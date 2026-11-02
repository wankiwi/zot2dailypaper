"""One isolated read-only capture. All console output is suppressed until sealed.

Only an explicitly approved transient data Secret supplies the private protocol.
No OpenAI/email/worker/cache credentials are accepted or needed.
"""
import contextlib
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import traceback
from time import monotonic

PUBLIC = Path(__file__).resolve().parent
PIN = '339d0185fff0f67e7726eeb162462652e8669a38'
EXPERIMENT = 'jina-offline-20261101'
ALLOWED_OVERRIDES = {'zotero', 'preprint_interests', 'source', 'abstracts', 'reranker', 'interest_profile', 'executor'}
DENIED_ENV = {'OPENAI_API_KEY', 'SENDER_PASSWORD', 'EMBEDDING_CACHE_KEY', 'ZOTERO_WORKER_D1_TOKEN'}
EXPORT_FORBIDDEN = {'corpus', 'config', 'vectors', 'history', 'private_input', 'runtime_overrides', 'paths'}


def validate_payload(payload):
    required = {'protocol', 'ranking_config', 'exclusions', 'freeze_receipt', 'runtime_overrides'}
    if set(payload) != required:
        raise ValueError('private_payload_keys')
    if set(payload['runtime_overrides']) - ALLOWED_OVERRIDES:
        raise ValueError('private_payload_unsupported_override')
    if any(k in payload['runtime_overrides'].get('zotero', {}) for k in ('api_key', 'user_id')):
        raise ValueError('credentials_must_use_existing_secrets')
    if 'api' in payload['runtime_overrides'].get('reranker', {}):
        raise ValueError('remote_embeddings_forbidden')
    if payload['protocol'].get('paid_calls_authorized') != 0:
        raise ValueError('paid_calls_not_authorized')
    if payload['protocol'].get('source_commit') != PIN:
        raise ValueError('wrong_source_pin')
    return payload


def check_export(value):
    if isinstance(value, dict):
        if set(value) & EXPORT_FORBIDDEN:
            raise ValueError('private_library_material_in_export')
        for child in value.values():
            check_export(child)
    elif isinstance(value, list):
        for child in value:
            check_export(child)


def git_head(root):
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def expanded_arxiv_config(config, categories):
    """Set the effective interest scope, which overrides the legacy source field."""
    from zot2dailypaper.preprint_interests import categories_for
    from omegaconf import OmegaConf
    base_categories = list(categories_for(config, 'arxiv'))
    extra = copy.deepcopy(config)
    if extra.get('preprint_interests') is None:
        extra.preprint_interests = OmegaConf.create({})
    if extra.preprint_interests.get('arxiv') is None:
        extra.preprint_interests.arxiv = OmegaConf.create({})
    extra.preprint_interests.arxiv.categories = list(categories)
    extra.source.arxiv.category = list(categories)
    effective = list(categories_for(extra, 'arxiv'))
    new = sorted(set(effective) - set(base_categories))
    if effective != list(categories) or not new or '*' in base_categories:
        raise ValueError('expanded_arxiv_scope_not_distinct')
    return extra, {'base_categories': base_categories, 'effective_categories': effective,
                   'new_categories': new, 'scope_verified_before_fetch': True}


def run_capture(private, payload):
    baseline = Path(os.environ['BASELINE_ROOT']).resolve()
    history_root = Path(os.environ['HISTORY_ROOT']).resolve()
    if git_head(baseline) != PIN:
        raise ValueError('wrong_baseline_checkout')
    for key in DENIED_ENV:
        if os.environ.get(key):
            raise ValueError('unnecessary_production_credential_present')
    if not all(os.environ.get(k) for k in ('ZOTERO_ID', 'ZOTERO_KEY')):
        raise ValueError('existing_zotero_secrets_missing')
    os.environ['JINA_OFFLINE_PRIVATE_DIR'] = str(private)
    os.environ['PRIVATE_EMBEDDING_CACHE_DIR'] = str(private / 'vectors')
    os.environ['HF_HOME'] = str(private / 'public_model')
    sys.path.insert(0, str(baseline / 'src'))
    import offline_eval as e
    from omegaconf import OmegaConf
    from hydra import initialize_config_dir, compose
    from loguru import logger
    from importlib.metadata import version
    from zot2dailypaper.executor import Executor
    from zot2dailypaper.abstracts import clean_abstract, recovery_shortlist, recover_abstracts, RecoveryContext
    from zot2dailypaper.identity import deduplicate, paper_dois, paper_doi, title_key, paper_id
    from zot2dailypaper.reranker.experiments import paper_text
    from zot2dailypaper.retriever import get_retriever_cls
    from zot2dailypaper.selection import is_cover_title, select_papers
    from zot2dailypaper.scores import minimum_score
    from zot2dailypaper.state import State
    logger.remove()
    for file, value in [('source_tree.snapshot.json', json.loads((PUBLIC / 'source_pin.json').read_text())),
                        ('runtime_config.frozen.private.json', payload['ranking_config']),
                        ('protocol.frozen.private.json', payload['protocol']),
                        ('prior49.exclusions.private.json', payload['exclusions']),
                        ('freeze_receipt.private.json', payload['freeze_receipt'])]:
        e.private_json(private / file, value)
    e.verify_source()
    loader = importlib.util.spec_from_file_location('isolated_prepare', baseline / 'scripts/prepare_workflow.py')
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    env = dict(os.environ, GITHUB_EVENT_NAME='schedule', LLM_MODE='disabled',
               CUSTOM_CONFIG=json.dumps(payload['runtime_overrides']), ZOTERO_WORKER_SYNC='disabled')
    module.prepare(baseline, env)  # Writes only the throwaway baseline checkout.
    with initialize_config_dir(config_dir=str(baseline / 'config'), version_base=None):
        cfg = compose(config_name='runtime')
    cfg = OmegaConf.merge(cfg, {'llm': {'enabled': False}, 'output': {'email': {'enabled': False}, 'rss': {'enabled': False}},
                               'state': {'enabled': False}, 'email': {'worker_sync': 'disabled'},
                               'reranker': {'local': {'cache_dir': str(private / 'vectors')}}})
    if cfg.executor.reranker != 'local' or cfg.reranker.strategy != 'multi_interest_profile':
        raise ValueError('actual_local_profile_required')
    if cfg.reranker.local.model != e.MODEL or cfg.reranker.local.revision != e.REVISION:
        raise ValueError('same_pinned_local_model_required')
    # Normalize only the private cache location in the ranking-only projection.
    projection = {'reranker': {k: OmegaConf.to_container(cfg.reranker[k], resolve=True)
                              if OmegaConf.is_config(cfg.reranker[k]) else cfg.reranker[k]
                              for k in payload['ranking_config']['reranker']},
                  'interest_profile': OmegaConf.to_container(cfg.interest_profile, resolve=True),
                  'executor': {k: OmegaConf.to_container(cfg.executor[k], resolve=True)
                               if OmegaConf.is_config(cfg.executor[k]) else cfg.executor[k]
                               for k in payload['ranking_config']['executor']}}
    projection['reranker']['local']['cache_dir'] = None
    if projection != payload['ranking_config']:
        raise ValueError('actual_runtime_ranking_projection_mismatch')
    ex = Executor(cfg)  # No Executor.run, _recommend, sync, delivery, or state save.
    original_corpus = ex.fetch_zotero_corpus()
    corpus = ex.filter_corpus(original_corpus)
    if not corpus:
        raise ValueError('actual_zotero_corpus_empty')
    history_file = history_root / 'recommendations.json'
    if not history_file.is_file():
        raise ValueError('read_only_history_snapshot_missing')
    state = State(history_file, enabled=True, retention_days=int(cfg.state.retention_days))
    events, attempts, raw = [], [], []
    for name, retriever in ex.retrievers.items():
        try:
            papers = retriever.retrieve_papers()
            attempts.append({'source': name, 'count': len(papers),
                             'failure_count': len(getattr(retriever, 'failures', [])),
                             'coverage_complete': None})
            raw.extend(papers)
        except Exception as error:
            attempts.append({'source': name, 'error_type': type(error).__name__, 'coverage_complete': False})
    candidates = []
    for p in raw:
        if is_cover_title(p.title):
            events.append({'id': paper_id(p), 'stage': 'cover', 'reason': 'publisher_cover'})
        else:
            p.abstract = clean_abstract(p.abstract)
            candidates.append(p)
    unique = deduplicate(candidates)
    unique_ids = {id(p) for p in unique}
    for p in candidates:
        if id(p) not in unique_ids:
            events.append({'id': paper_id(p), 'stage': 'identity', 'reason': 'duplicate_or_merged'})
    kept = []
    for p in unique:
        if cfg.executor.get('exclude_existing', True) and (paper_dois(p) & ex.library_dois or
                title_key(p.title) in (ex.library_titles_without_doi if paper_doi(p) else ex.library_titles)):
            events.append({'id': paper_id(p), 'stage': 'library', 'reason': 'already_in_zotero'})
        elif state.has(p):
            events.append({'id': paper_id(p), 'stage': 'history', 'reason': 'already_delivered'})
        else:
            kept.append(p)
    if not kept:
        raise ValueError('no_actual_common_candidates')
    before = {id(p): p.abstract for p in kept}
    original = ex.reranker.rerank(kept, corpus)
    recovery = cfg.abstracts
    if recovery.enabled and recovery.pre_rank_max_papers:
        chosen = recovery_shortlist(original, int(recovery.pre_rank_max_papers))
        context = RecoveryContext(deadline=monotonic() + int(recovery.pre_rank_seconds))
        recover_abstracts(chosen, recovery, context=context)
        for p in chosen:
            events.append({'id': paper_id(p), 'stage': 'recovery',
                           'reason': 'abstract_recovered' if p.abstract else 'abstract_still_missing'})
        original = ex.reranker.rerank(kept, corpus)
    # Capture a new real A snapshot; this does NOT recreate lost historic inputs.
    gold = {'order': [paper_id(p) for p in original],
            'rows': {paper_id(p): {f: getattr(p, f) for f in e.SCORE_FIELDS} for p in original}}
    pending_papers = state.pending('email')
    chosen = select_papers([p for p in original if p.score >= minimum_score(cfg.executor)],
                           dict(cfg.executor.quotas), pending_papers, random.Random(payload['protocol']['seed']))
    gold['selection'] = {g: [paper_id(p) for p in chosen if p.recommendation_group == g]
                         for g in ('journals', 'preprints', 'random')}
    gold['selection']['main_order'] = [paper_id(p) for p in chosen if p.recommendation_group != 'random']
    now = datetime.now(timezone.utc).isoformat()
    def row(p, pool):
        date = p.published.date().isoformat() if p.published else ''
        return {'id': paper_id(p), 'doi': p.doi, 'related_dois': p.related_dois,
                'source': p.source, 'title': p.title, 'url': p.url, 'journal': p.journal,
                'abstract': p.abstract, 'abstract_before_recovery': before.get(id(p), p.abstract),
                'abstract_complete': bool(p.abstract.strip()) and not p.abstract.rstrip().endswith(('...', '…')),
                'abstract_provenance': {'source': p.abstract_source or p.source,
                                        'url': p.abstract_source_url or p.url}, 'retrieved_at': now,
                'publication_kind': p.publication_kind,
                'first_public_date': '' if p.related_dois else date,
                'date_basis': 'source published date; related versions require independent earliest-date evidence',
                'pool': pool}
    rows = [row(p, 'common') for p in kept]
    extra_cfg, expanded_scope = expanded_arxiv_config(
        cfg, payload['protocol']['source_plan']['expanded']['arxiv_categories'])
    extra = get_retriever_cls('arxiv')(extra_cfg)
    try:
        expanded = extra.retrieve_papers()
        attempts.append({'source': 'expanded_arxiv', 'count': len(expanded),
                         'coverage_complete': None, **expanded_scope})
        known = {paper_id(p) for p in kept}
        rows += [row(p, 'expanded') for p in deduplicate(expanded)
                 if paper_id(p) not in known and not state.has(p)
                 and not (paper_dois(p) & ex.library_dois)
                 and title_key(p.title) not in (ex.library_titles_without_doi if paper_doi(p) else ex.library_titles)]
    except Exception as error:
        attempts.append({'source': 'expanded_arxiv', 'error_type': type(error).__name__,
                         'coverage_complete': False, **expanded_scope})
    # Encode complete source texts; raw references stay only in process/private temp.
    refs = [{'id': f'R{i:06}', 'title': p.title, 'abstract': p.abstract, 'doi': p.doi,
             'added_date': p.added_date.isoformat(), 'paths': p.paths} for i, p in enumerate(corpus)]
    text = [paper_text(e.as_corpus(p), mode) for p in refs for mode in ('abstract', 'title_abstract')]
    text += [paper_text(e.as_paper(p), mode) for p in rows for mode in ('abstract', 'title_abstract')]
    text += list(cfg.interest_profile.keywords)
    text += [c['task_description'] for c in payload['protocol']['C']['channels']]
    texts = list(dict.fromkeys(text))
    vectors = ex.reranker._encode_texts(texts)
    encoder = ex.reranker._encoder
    resolved_revision = getattr(encoder[0].auto_model.config, '_commit_hash', None)
    if resolved_revision != e.REVISION:
        raise ValueError('resolved_model_revision_mismatch')
    model_snapshot = private / 'public_model' / 'hub' / ('models--' + e.MODEL.replace('/', '--')) / 'snapshots' / e.REVISION
    artifact_hashes = {}
    for file in sorted(model_snapshot.rglob('*')):
        if file.is_file():
            if not file.resolve().is_relative_to(private):
                raise ValueError('model_artifact_outside_private_job_directory')
            h = hashlib.sha256()
            with file.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(chunk)
            artifact_hashes[str(file.relative_to(model_snapshot))] = h.hexdigest()
    if not artifact_hashes:
        raise ValueError('public_model_artifact_hashes_missing')
    identity = {'model': cfg.reranker.local.model, 'revision': cfg.reranker.local.revision,
                'resolved_revision': resolved_revision, 'artifact_files_sha256': artifact_hashes,
                'dimension': int(vectors.shape[1]), 'backend': ex.reranker._backend,
                'dtype': str(next(encoder.parameters()).dtype), 'device': str(encoder.device),
                'threads': __import__('torch').get_num_threads(),
                'encode_kwargs': OmegaConf.to_container(cfg.reranker.local.encode_kwargs),
                'prompts': encoder.prompts, 'max_seq_length': encoder.max_seq_length,
                'cache_namespace': ex.reranker._cache_namespace,
                'libraries': {k: version(k) for k in ('numpy', 'torch', 'transformers', 'sentence-transformers', 'tokenizers')}}
    pack = {'model_identity': identity,
            'vectors': {hashlib.sha256(t.encode()).hexdigest(): v.tolist() for t, v in zip(texts, vectors)}}
    pack['vectors_sha256'] = e.digest(pack['vectors'])
    pending = [dict(row(p, 'pending'), recommendation_group=p.recommendation_group) for p in pending_papers]
    snapshot = {'source_commit': PIN, 'captured_at': now, 'config': projection,
                'model_identity': identity, 'corpus': refs, 'candidates': rows,
                'candidate_events': events, 'source_attempts': attempts,
                'history': {'sha256': hashlib.sha256(history_file.read_bytes()).hexdigest(),
                            'commit': git_head(history_root)}, 'pending': pending,
                'expected_A': gold, 'trace_completeness': 'item filtering traced; internal upstream retrieval coverage remains unknown'}
    result = e.evaluate(snapshot, pack, payload['protocol'], payload['exclusions'])
    result['capture_counts'] = {'zotero_before_filter': len(original_corpus), 'zotero_after_filter': len(corpus),
                                'raw_candidates': len(raw), 'common_after_filters': len(kept)}
    result['source_configuration_sha256'] = e.digest(OmegaConf.to_container(cfg.source, resolve=False))
    # Candidate public text is allowed only in the sealed result; no raw Zotero
    # reference text, titles, DOI list, collection paths, vectors or state export.
    result['public_candidates'] = rows
    result['baseline_scope'] = 'new captured real baseline; historical 41248417743 full inputs unavailable'
    check_export(result)
    return result


def main():
    output = Path(os.environ['SEALED_OUTPUT']).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(prefix='jina-offline-', dir=os.environ['RUNNER_TEMP']))
    result = {'status': 'blocked_before_inference', 'paid_calls': 0}
    protocol_hash = 'unavailable'
    try:
        # Untrusted exception messages and all library progress stay off public logs.
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            payload = validate_payload(json.loads(os.environ.pop('JINA_OFFLINE_INPUT', '{}')))
            from seal import canonical
            protocol_hash = hashlib.sha256(canonical(payload['protocol'])).hexdigest()
            result = run_capture(private, payload)
    except Exception as error:
        result = {'status': 'blocked_or_failed', 'paid_calls': 0, 'error_type': type(error).__name__}
        safe_codes = {'private_payload_keys', 'private_payload_unsupported_override',
                      'credentials_must_use_existing_secrets', 'remote_embeddings_forbidden',
                      'paid_calls_not_authorized', 'wrong_source_pin', 'wrong_baseline_checkout',
                      'unnecessary_production_credential_present', 'existing_zotero_secrets_missing',
                      'actual_local_profile_required', 'same_pinned_local_model_required',
                      'resolved_model_revision_mismatch', 'model_artifact_outside_private_job_directory',
                      'public_model_artifact_hashes_missing',
                      'expanded_arxiv_scope_not_distinct',
                      'actual_runtime_ranking_projection_mismatch', 'actual_zotero_corpus_empty',
                      'read_only_history_snapshot_missing', 'no_actual_common_candidates'}
        if type(error) is ValueError and str(error) in safe_codes:
            result['error_code'] = str(error)
        result['error_sites'] = [{'file': Path(x.filename).name, 'function': x.name, 'line': x.lineno}
                                 for x in traceback.extract_tb(error.__traceback__)[-4:]]
    finally:
        from seal import seal
        check_export(result)
        envelope = seal(result, (PUBLIC / 'recipient.public.pem').read_bytes(), EXPERIMENT, protocol_hash)
        output.write_text(json.dumps(envelope, separators=(',', ':')))
        shutil.rmtree(private, ignore_errors=True)
    print(json.dumps({'status': result['status'], 'paid_calls': 0, 'sealed_result_saved': True}))


if __name__ == '__main__':
    main()
