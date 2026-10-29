"""Explicit manual held-out pilot. Public outputs are ciphertext and safe status."""
import base64
from collections import Counter
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from common import ROOT,PLAN_ID,PLAN_SHA,RUBRIC_SHA,PRODUCTION,N,Stop,dumps,digest,text_sha,canonical,eligible,select,requests_for,blind_rows,new_core,seal
import pilot_budget as budget
sys.path.insert(0,str(ROOT.parent))
import runner as previous_runner

def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(dumps(value)+'\n')

def read_remote():
    entry=previous_runner.github('GET')
    if not entry or 'sha' not in entry:raise Stop('shared_historical_ledger_missing')
    return json.loads(base64.b64decode(entry['content'])),entry['sha']

def put_remote(state,sha):
    previous_runner.github('PUT',{'message':'Record cumulative held-out pilot cost only','branch':previous_runner.BRANCH,'sha':sha,'content':base64.b64encode(dumps(state).encode()).decode()})

def load_baseline(private):
    """Use the production loader and actual LocalReranker. Never Executor.run."""
    from loguru import logger
    logger.remove()
    from omegaconf import OmegaConf
    from hydra import initialize_config_dir,compose
    from zot2dailypaper.executor import Executor
    from zot2dailypaper.interest_profile import interest_profile
    from zot2dailypaper.reranker.onnx_encoder import MODEL,REVISION
    base=Path(os.environ['BASELINE_ROOT']).resolve()
    if subprocess.check_output(['git','rev-parse','HEAD'],cwd=base,text=True).strip()!=PRODUCTION:raise Stop('production_snapshot_commit_mismatch')
    if not all(os.environ.get(name) for name in ('CUSTOM_CONFIG','ZOTERO_ID','ZOTERO_KEY')):raise Stop('current_private_config_or_zotero_credentials_missing')
    supplied=OmegaConf.create(os.environ['CUSTOM_CONFIG'])
    for name in ('interest_profile.keywords','interest_profile.keyword_weight','interest_profile.zotero_weight','zotero.ignore_path','reranker.strategy','reranker.missing_abstract_factor'):
        if OmegaConf.select(supplied,name) is None:raise Stop('explicit_current_baseline_settings_missing')
    spec=__import__('importlib.util',fromlist=['util']).spec_from_file_location('pilot_production_prepare',base/'scripts/prepare_workflow.py')
    loader=__import__('importlib.util',fromlist=['util']).module_from_spec(spec);spec.loader.exec_module(loader)
    env=dict(os.environ,GITHUB_EVENT_NAME='schedule',LLM_MODE='disabled')
    loader.prepare(base,env) # Fresh isolated checkout, not production configuration.
    with initialize_config_dir(config_dir=str(base/'config'),version_base=None):config=compose(config_name='runtime')
    config=OmegaConf.merge(config,{'llm':{'enabled':False},'output':{'email':{'enabled':False},'rss':{'enabled':False}},'state':{'enabled':False},'reranker':{'local':{'cache_dir':str(private/'vectors')}}})
    profile=interest_profile(config)
    if config.executor.reranker!='local' or config.reranker.strategy!='multi_interest_profile' or config.reranker.local.model!=MODEL or config.reranker.local.revision!=REVISION:raise Stop('current_jina_profile_baseline_required')
    if not profile.keywords or profile.keyword_weight<=0 or profile.zotero_weight<=0:raise Stop('nonempty_weighted_keyword_and_zotero_baseline_required')
    executor=Executor(config)
    corpus=executor.filter_corpus(executor.fetch_zotero_corpus())
    if not corpus:raise Stop('filtered_zotero_corpus_empty_no_keyword_only_substitute')
    settings={'keywords':list(profile.keywords),'weights':profile.effective_weights(True),
              'include_path':OmegaConf.to_container(config.zotero.include_path) if config.zotero.include_path is not None else None,
              'ignore_path':OmegaConf.to_container(config.zotero.ignore_path) if config.zotero.ignore_path is not None else None,
              'reranker':{name:OmegaConf.to_container(config.reranker[name],resolve=True) if OmegaConf.is_config(config.reranker[name]) else config.reranker[name] for name in ('strategy','multi_interest_profile','experiments','missing_abstract_factor','local')}}
    settings['reranker']['local'].pop('cache_dir',None)
    ordered=sorted(corpus,key=lambda p:p.added_date,reverse=True)
    # Memory-only hash input: no library entries/paths/abstracts are exported.
    snapshot=digest([[p.title,p.abstract,p.doi,p.added_date.isoformat(),p.paths] for p in ordered])
    source_settings={'sources':list(executor.retrievers),'source':OmegaConf.to_container(config.source,resolve=False),
                     'preprint_interests':OmegaConf.to_container(config.preprint_interests,resolve=True) if config.get('preprint_interests') else None}
    evidence={'production_commit':PRODUCTION,'profile_settings_sha256':digest(settings),'filtered_ordered_corpus_sha256':snapshot,
              'filtered_corpus_count':len(corpus),'keyword_count':len(profile.keywords),
              'effective_keyword_weight':profile.effective_weights(True)[0],'effective_zotero_weight':profile.effective_weights(True)[1],
              'source_settings_sha256':digest(source_settings),'strategy':config.reranker.strategy,
              'missing_abstract_factor':config.reranker.missing_abstract_factor,'model':MODEL,'revision':REVISION,
              'experiment_settings':OmegaConf.to_container(config.reranker.experiments,resolve=True)}
    return executor,corpus,profile,evidence

def retrieve_and_rank(executor,corpus,profile,plan,development,report=None):
    from zot2dailypaper.abstracts import clean_abstract
    from zot2dailypaper.identity import deduplicate,paper_dois,paper_doi,title_key
    from zot2dailypaper.selection import is_cover_title
    from zot2dailypaper.reranker.experiments import paper_text
    import numpy as np
    candidates=[];source_counts={};failures={}
    for source,retriever in executor.retrievers.items():
        try:
            values=retriever.retrieve_papers();candidates.extend(values);source_counts[source]=len(values)
            failures[source]=len(getattr(retriever,'failures',[]))
        except Exception as error:
            failures[source]={'error_class':type(error).__name__,'http_status':getattr(error,'code',None) or getattr(getattr(error,'response',None),'status_code',None)};source_counts[source]=0
    covers=sum(is_cover_title(p.title) for p in candidates)
    papers=deduplicate([p for p in candidates if not is_cover_title(p.title)])
    before=len(papers)
    if executor.config.executor.get('exclude_existing',True):
        papers=[p for p in papers if not (paper_dois(p)&executor.library_dois) and title_key(p.title) not in (executor.library_titles_without_doi if paper_doi(p) else executor.library_titles)]
    rows=[]
    for p in papers:
        p.abstract=clean_abstract(p.abstract)
        row={'canonical_id':canonical(p.doi or p.url),'aliases':list(paper_dois(p))+[p.url],
             'title':p.title,'abstract':p.abstract,'link':'https://doi.org/'+p.doi if p.doi else p.url,
             'source':p.source,'group':'journals' if p.source=='journals' else 'preprints','paper':p,
             'abstract_provenance':{'source':p.abstract_source or p.source,'url':p.abstract_source_url or p.url}}
        if row['canonical_id']:rows.append(row)
    rows,excluded=eligible(rows,plan,development)
    report=report if report is not None else {}
    report.update(retrieved_source_counts=source_counts,source_failures=failures,cover_exclusions=covers,
                  library_existing_exclusions=before-len(papers),eligible_count=len(rows),excluded_count=len(excluded),
                  exclusions=excluded,eligible_source_counts=dict(Counter(p['source'] for p in rows)))
    if len(rows)<N:raise Stop('fewer_than_24_eligible_full_abstracts')
    # Rerank every eligible candidate with the current production algorithm.
    executor.reranker.rerank([p['paper'] for p in rows],corpus)
    text=[paper_text(p['paper'],executor.config.reranker.experiments.text_mode) for p in rows]
    similarities=executor.reranker.get_rank_similarity(text,list(profile.keywords),0,'document')
    executor.reranker.validate_similarity(similarities,(len(rows),len(profile.keywords)))
    for i,p in enumerate(rows):
        p['baseline_score']=float(p['paper'].score);p['direction']=int(np.argmax(similarities[i]))
        p['keyword_cosine']=float(similarities[i,p['direction']])
    chosen,sampling=select(rows)
    pool_hash=digest([{'id':p['canonical_id'],'abstract_sha256':text_sha(p['abstract']),'source':p['source']} for p in rows])
    chosen=sorted(chosen,key=lambda p:p['canonical_id'])
    for i,p in enumerate(chosen,1):p['id']=f'H{i:03d}'
    evidence={'pool_sha256':pool_hash,'selection':sampling,'candidate_ids':[p['canonical_id'] for p in chosen],
              'freeze_time_utc':datetime.now(timezone.utc).isoformat(),
              'history_policy':'Fresh source pool before delivery-history and quotas; no production history read/write.',
              'scored_snapshot_sha256':digest([{k:p[k] for k in ('id','title','abstract')} for p in chosen])}
    report.update(evidence)
    return chosen,report

class CallLedger:
    def __init__(self,client,private,index):
        self.client=client;self.index=index
        self.inner=client.Ledger(private/f'call_{index:02d}.receipt.json')
    def reserve(self,method,request):
        state,sha=read_remote()
        proposed=budget.start(state,os.environ['GITHUB_RUN_ID'],self.index,digest(request))
        amount=self.inner.reserve(method,request)
        put_remote(proposed,sha) # CAS must confirm before send.
        return amount
    def finish(self,method,fields):
        receipt=self.inner.finish(method,fields)
        state,sha=read_remote()
        updated=budget.finish_call(state,os.environ['GITHUB_RUN_ID'],self.index,receipt)
        put_remote(updated,sha)
        return receipt

def execute(private,result):
    previous_runner.assert_job(os.environ)
    state,_=read_remote();budget.ready(state)
    plan=json.loads((ROOT/'protocol.public.json').read_text())
    rubric=json.loads((ROOT.parent/'rubric.public.json').read_text())
    if digest(plan)!=PLAN_SHA or digest(rubric)!=RUBRIC_SHA:raise Stop('frozen_protocol_or_rubric_changed')
    client=new_core()
    price=json.loads((ROOT.parent/'pricing.public.json').read_text())
    if price['version']!=client.PRICE_VERSION or price['verified_on']!=datetime.now(timezone.utc).date().isoformat():raise Stop('current_day_verified_pricing_required')
    client.credential(os.environ) # Fail early if the official key is absent/unverified; no request.
    result['phase']='baseline_and_sources'
    executor,corpus,profile,baseline=load_baseline(private)
    result['baseline']=baseline
    development=json.loads((ROOT.parent/'papers.public.json').read_text())
    result['pool']={}
    rows,pool=retrieve_and_rank(executor,corpus,profile,plan,development,result['pool'])
    result['pool']=pool
    result['phase']='frozen_before_decisions'
    blind,mapping=blind_rows(rows)
    result['blind_rows']=blind;result['blind_to_scored_id']=mapping
    result['candidates']=[{k:p[k] for k in ('id','canonical_id','title','link','source','group','abstract_provenance')}|{'abstract_sha256':text_sha(p['abstract']),'abstract_bytes':len(p['abstract'].encode())} for p in rows]
    result['baseline_scores']={p['id']:p['baseline_score'] for p in rows}
    result['baseline_order']=sorted(result['baseline_scores'],key=lambda key:(-result['baseline_scores'][key],key))
    batches=requests_for(rows,rubric)
    plans=[{'request_sha256':digest(request),'reserved_nanos':client.reserve_nanos('decisions',request)} for request in batches]
    # Frozen inputs, private profile hashes and both batch order/bounds exist before paid claim.
    write(private/'pre_model_snapshot.json',result)
    if price['verified_on']!=datetime.now(timezone.utc).date().isoformat():raise Stop('pricing_date_changed_before_paid_boundary')
    state,sha=read_remote()
    claimed=budget.reserve(state,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'],plans)
    put_remote(claimed,sha)
    result['phase']='decisions';result['status']='in_progress';result['request_plans']=plans;result['model_receipts']=[];scores={};success=False
    try:
        for index,request in enumerate(batches):
            actual_client=new_core();actual_client.EXPECTED_IDS=frozenset(q['name'] for q in request['questions'])
            def send(endpoint,payload,key):
                result['api_requests_started']=result.get('api_requests_started',0)+1
                return actual_client.transport(endpoint,payload,key)
            response_dir=private/f'response_{index:02d}'
            receipt=actual_client.run_one('decisions',request,CallLedger(actual_client,private,index),client.credential(os.environ),send=send,output=response_dir)
            result['model_receipts'].append(receipt)
            response_path=response_dir/'decisions.sanitized-response.json'
            if response_path.exists():result.setdefault('sanitized_model_responses',[]).append(json.loads(response_path.read_text()))
            if receipt.get('status')!='completed':raise Stop('model_failure_no_retry')
            scores.update(receipt['scores'])
        if set(scores)!={p['id'] for p in rows}:raise Stop('incomplete_pilot_scores')
        result['decisions_scores']=scores;result['decisions_order']=sorted(scores,key=lambda key:(-scores[key],key))
        success=True;result['phase']='completed';result['status']='completed'
    finally:
        state,sha=read_remote()
        closed=budget.close(state,os.environ['GITHUB_RUN_ID'],success)
        put_remote(closed,sha);result['cumulative_budget']=closed['summary']
    return result

def main():
    temp=Path(os.environ['RUNNER_TEMP']);private=temp/'heldout-private';public=temp/'heldout-public'
    private.mkdir(mode=0o700,exist_ok=True);public.mkdir(mode=0o700,exist_ok=True)
    result={'experiment':PLAN_ID,'protocol_sha256':PLAN_SHA,'rubric_sha256':RUBRIC_SHA,
            'run_id':os.environ.get('GITHUB_RUN_ID'),'source_commit':os.environ.get('GITHUB_SHA'),
            'status':'blocked_before_model','phase':'preflight','human_labels_received':False}
    public_status={'experiment':PLAN_ID,'status':'blocked','api_requests_this_run':0,'historical_estimated_usd':'0.000437000'}
    recipient=(ROOT/'recipient.public.pem').read_bytes()
    # Validate the recipient before any data access or model request.
    seal({'synthetic_preflight':True},recipient)
    try:
        execute(private,result)
    except Exception as error:
        if result['phase']=='decisions':result['status']='failed_after_reservation'
        result['error_class']=type(error).__name__
        if isinstance(error,(Stop,budget.previous.BudgetStop)) or (type(error).__name__=='Blocked' and type(error).__module__.startswith('heldout_client_')):result['reason']=str(error)
    finally:
        try:
            state,_=read_remote()
            result['cumulative_budget']=budget.summary(state) if state.get('schema')==3 else state['summary']
            public_status['cumulative_budget']=result['cumulative_budget']
        except Exception:public_status['remote_accounting_readable']=False
        # No raw abstracts, corpus, credentials or raw logs are part of this export.
        write(public/'result.sealed.json',seal(result,recipient))
        calls=result.get('model_receipts',[])
        public_status.update(status=result['status'],phase=result['phase'],api_requests_this_run=result.get('api_requests_started',0),
                             baseline_ready='baseline' in result,sample_count=len(result.get('candidates',[])),
                             error_class=result.get('error_class'),reason=result.get('reason'))
        write(public/'status.public.json',public_status)
        # stdout is redirected to a private temp log by the workflow as an extra guard.
        print(dumps(public_status))

if __name__=='__main__':main()
