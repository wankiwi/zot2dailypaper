"""Public-metadata-only capture; no Zotero, embeddings, LLM or delivery clients.

This draft is local for review. Publication, data upload and one remote run require
separate authorization. Result artifacts contain only authenticated ciphertext.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from datetime import datetime, timezone

from omegaconf import OmegaConf
from seal import seal, canonical
from snapshot_policy import freeze_cutoff, frozen_source_dates, bind_arxiv_cutoff, capture_version_metadata, public_row
from http_diagnostics import collect, error_record

PUBLIC = Path(__file__).resolve().parent
ROOT = PUBLIC.parents[1]
EXPECTED_SOURCE_BLOBS = {
    "src/zot2dailypaper/__init__.py": "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
    "src/zot2dailypaper/abstracts.py": "c11a56a37bc8f42df620f8b8ce7d60480adb3ee0",
    "src/zot2dailypaper/http.py": "6e18542aa78d0fe102b552d59e4ac26a12fb32fe",
    "src/zot2dailypaper/identity.py": "8a32176dc8dac911cb55f3c7dbd104c8ad6185d2",
    "src/zot2dailypaper/journals.py": "16fd13ccf3929c2645fc014c583d337cd32551a0",
    "src/zot2dailypaper/metadata.py": "ce52a7104e87f21cbfffd39dd15e790fbdb2c8f2",
    "src/zot2dailypaper/preprint_interests.py": "5a646ffb652daa137986480c67ea23d64373f464",
    "src/zot2dailypaper/protocol.py": "d0bb2ffcf0ed85f0139f1d3ef7e045c82e34450e",
    "src/zot2dailypaper/retriever/__init__.py": "00f3926d1397a9d9d5dfc6f8879ec0b414456bfc",
    "src/zot2dailypaper/retriever/arxiv_retriever.py": "2397a8618320b7dadd68fb27f10736fee9e99b65",
    "src/zot2dailypaper/retriever/base.py": "13a00af1eafdd4a1fd9590d0e9ec7d0ebfc96e42",
    "src/zot2dailypaper/retriever/biorxiv_retriever.py": "9f0b244428560a98a5ee90bb82c4eb7c74591524",
    "src/zot2dailypaper/retriever/chemrxiv_retriever.py": "944c0ae878391c9a32347b7124629500136b3e81",
    "src/zot2dailypaper/retriever/journal_retriever.py": "c308fc22f688723bd5bb45b5d975a2fda0f69dd6",
    "src/zot2dailypaper/retriever/medrxiv_retriever.py": "7a976b38071cfacf50a4b2297219abd223b803cf",
    "src/zot2dailypaper/retriever/openalex_researchsquare.py": "ddc6a536d7ec7e62fc125cb61ccc3339b984bfc3",
    "src/zot2dailypaper/retriever/openreview_retriever.py": "5dce1790be0365ee1da60004ff2b1be92e8c64a6",
    "src/zot2dailypaper/retriever/researchsquare_retriever.py": "a40fac45f0a860d1d9b631d0f24208764068b450",
    "src/zot2dailypaper/scores.py": "8914482e004a67b980d858d2a704b12d3ee24a03",
    "src/zot2dailypaper/selection.py": "4f7446d47554a9df3f983f77634a9733c7ed42b7"
}
DENIED_ENV = {'ZOTERO_ID','ZOTERO_KEY','OPENAI_API_KEY','SILICONFLOW_API_KEY',
              'SMTP_PASSWORD','SENDER_PASSWORD','CUSTOM_CONFIG','JINA_FULLPOOL_INPUT','JINA_OFFLINE_INPUT'}
SECRET_KEYS = {'api_key','key','token','access_token','password','username','user_id',
               'email','mailto','sender','receiver','corpus','ranking_config','interest_profile','labels'}
SOURCES = {'journals','arxiv','biorxiv','medrxiv','researchsquare','openreview','chemrxiv'}


class CaptureCancelled(BaseException):
    pass


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_input(value):
    if set(value) != {'schema','source_config','date_policy','expansion_arxiv_categories'}:
        raise ValueError('source_input_keys')
    if value['schema'] != 'jina_public_source_capture_v1':
        raise ValueError('source_input_schema')
    config = value['source_config']
    if set(config) != {'source','executor','preprint_interests'}:
        raise ValueError('source_config_keys')
    if set(config['executor']) != {'source','debug'} or config['executor']['debug'] is not False:
        raise ValueError('source_executor_only')
    if not config['executor']['source'] or set(config['executor']['source']) - SOURCES:
        raise ValueError('unknown_sources')
    if set(config['source']) - SOURCES:
        raise ValueError('unknown_source_config')
    def check(item):
        if isinstance(item, dict):
            if set(item) & SECRET_KEYS:
                raise ValueError('private_or_credential_data_forbidden')
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
    check(value)
    from zot2dailypaper.preprint_interests import validate_interests
    validate_interests(OmegaConf.create(config))
    categories = value['expansion_arxiv_categories']
    if not isinstance(categories,list) or not categories or not all(isinstance(s,str) for s in categories):
        raise ValueError('expansion_categories_required')
    return value


def verify_sources(root=ROOT):
    for path, expected in EXPECTED_SOURCE_BLOBS.items():
        raw = (root/path).read_bytes()
        observed = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if observed != expected:
            raise ValueError('source_snapshot_mismatch')
    return len(EXPECTED_SOURCE_BLOBS)


def history_identities(path):
    # Preserve only public identity metadata. Include pending/uncertain delivery
    # conservatively: it could be shown before the private blind sheet is released.
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    if value.get('version') != 1 or not isinstance(value.get('records'),dict):
        raise ValueError('history_schema')
    rows=[]
    for key, record in sorted(value['records'].items()):
        paper=record['paper']
        rows.append({k:paper.get(k) for k in ('title','doi','related_dois','url')} | {'id':key})
    return rows,hashlib.sha256(raw).hexdigest()


def expanded_config(config, categories):
    result=copy.deepcopy(config)
    if result.get('preprint_interests') is None:
        result['preprint_interests']={}
    spec=result['preprint_interests'].setdefault('arxiv',{})
    spec['enabled']=True
    spec['categories']=list(categories)  # effective precedence, not source.category alone
    result['source']['arxiv']['category']=list(categories)
    result['executor']['source']=['arxiv']
    return result


def atomic_seal(path, value, public_key, input_hash):
    envelope=seal(value,public_key,'jina-public-sources-20261104',input_hash)
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temporary=tempfile.mkstemp(prefix='.sealed-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(canonical(envelope))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary,0o600)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def capture(value, private, checkpoint, history_path, now=None, factory=None, collector=collect):
    from zot2dailypaper.retriever import get_retriever_cls
    from zot2dailypaper.preprint_interests import enabled_sources
    value=validate_input(value)
    frozen=freeze_cutoff(value['date_policy'],now)
    cutoff=datetime.fromisoformat(frozen['asof_exclusive'])
    if factory is None:
        factory=get_retriever_cls
    cfgdict=copy.deepcopy(value['source_config'])
    # Catalog updates, downloaded metadata and local state are throwaway only.
    if 'journals' in cfgdict['source']:
        target=Path(private)/'journal_catalog.json'
        cached=ROOT/'data/journal_catalog.json'
        if cached.is_file():
            shutil.copyfile(cached,target)
        cfgdict['source']['journals']['catalog_cache']=str(target)
    result={'schema':'jina_public_source_snapshot_v1','status':'capturing',
            'query_cutoff':frozen['asof_exclusive'],'frozen_at':frozen['frozen_at'],
            'source_input_sha256':digest(value),'source_attempts':[],
            'source_days':{name:int(spec.get('window_days') or 7) for name,spec in cfgdict['source'].items()},
            'public_candidates':[],'delivery_identities':[],
            'delivery_history_complete':False,'blind_ready':False,'paid_calls':0,
            'zotero_network_access':False,'model_inference':False}
    checkpoint(result)
    try:
        identities,history_hash=history_identities(history_path)
        result.update(delivery_identities=identities,delivery_history_sha256=history_hash,
                      delivery_history_complete=True)
    except (OSError,ValueError,KeyError,TypeError) as error:
        result['history_error_type']=type(error).__name__
    scopes=[('common',cfgdict),('expanded',expanded_config(cfgdict,value['expansion_arxiv_categories']))]
    for scope, raw in scopes:
        cfg=OmegaConf.create(raw)
        for name in enabled_sources(cfg):
            result['active_phase']={'scope':scope,'source':name}
            checkpoint(result)
            retriever=factory(name)(cfg)
            capture_version_metadata(retriever)
            if name=='arxiv':
                bind_arxiv_cutoff(retriever,cutoff)
            with frozen_source_dates(cutoff):
                papers,audit=collector(retriever,name,scope)
            result['source_attempts'].append(audit)
            result['public_candidates'].extend(public_row(paper,scope) for paper in papers)
            checkpoint(result)
    attempts=result['source_attempts']
    complete=bool(attempts) and all(a['status'] in ('success','empty') and
                a.get('coverage_complete') is True for a in attempts)
    result['source_coverage_complete']=complete
    result['status']='public_sources_captured' if complete else 'source_preflight_failed_not_scored'
    result.pop('active_phase',None)
    result['captured_at']=datetime.now(timezone.utc).isoformat()
    checkpoint(result)
    return result


def main():
    os.umask(0o077)
    output=Path(os.environ['SEALED_OUTPUT']).resolve()
    key=(PUBLIC/'recipient.public.pem').read_bytes()
    latest={'status':'starting','paid_calls':0,'blind_ready':False}
    input_hash='unavailable'
    def checkpoint(value):
        nonlocal latest
        latest=value
        atomic_seal(output,value,key,input_hash)
    def abort(signum,frame):
        # Persist before unwinding SDK / thread-pool cleanup, which can block.
        latest['status']='capture_interrupted'
        latest['blind_ready']=False
        checkpoint(latest)
        raise CaptureCancelled()
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,abort)
    checkpoint(latest)
    try:
        if any(os.environ.get(name) for name in DENIED_ENV):
            raise ValueError('unnecessary_credential_or_profile_environment')
        verified_sources=verify_sources()
        value=json.loads(os.environ.pop('JINA_BLIND_SOURCE_INPUT'))
        input_hash=digest(value)
        with tempfile.TemporaryDirectory(prefix='jina-public-',dir=os.environ.get('RUNNER_TEMP')) as private:
            latest=capture(value,private,checkpoint,os.environ['HISTORY_FILE'])
        latest['capture_code_receipt']={'runner_commit':os.environ.get('GITHUB_SHA','local'),
            'verified_source_files':verified_sources,
            'source_blob_map_sha256':digest(EXPECTED_SOURCE_BLOBS),
            'capture_runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    except CaptureCancelled:
        pass
    except Exception as error:
        latest.update(status='capture_blocked_or_failed',error_type=type(error).__name__,
                      error=error_record(error),blind_ready=False,paid_calls=0)
    finally:
        checkpoint(latest)
        report={'status':latest['status'],'sealed_result_saved':True,
                'paid_calls':0,'blind_ready':False}
        try:
            os.write(3,(json.dumps(report)+'\n').encode())
        except OSError:
            pass
    return 0 if latest['status']=='public_sources_captured' else 2


if __name__=='__main__':
    raise SystemExit(main())
