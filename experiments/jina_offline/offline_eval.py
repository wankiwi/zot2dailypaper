"""Private, file-only A/B/C experiment. Never instantiate Executor or an API client.

Run with the project's existing Python environment. Real evaluations require a
captured private input snapshot and matching local Jina embeddings; missing inputs
are blockers, never replaced by fixtures. See README.md for the capture contract.
"""
from __future__ import annotations

import argparse
import ast
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher
from types import FunctionType

ROOT = Path(os.environ.get('BASELINE_ROOT', Path(__file__).resolve().parents[2])).resolve()
sys.path.insert(0, str(ROOT / 'src'))
import numpy as np
from omegaconf import OmegaConf
from zot2dailypaper.identity import canonical_doi, title_key, paper_id
from zot2dailypaper.protocol import Paper, CorpusPaper
from zot2dailypaper.reranker.base import BaseReranker
from zot2dailypaper.reranker.experiments import paper_text
from zot2dailypaper.reranker.strategies import strategy_settings
from zot2dailypaper.selection import select_papers, is_cover_title
from zot2dailypaper.scores import minimum_score

HERE = Path(os.environ.get('JINA_OFFLINE_PRIVATE_DIR', Path(__file__).resolve().parent)).resolve()
PIN = '339d0185fff0f67e7726eeb162462652e8669a38'
MODEL = 'jinaai/jina-embeddings-v5-text-nano-retrieval'
REVISION = 'ac5d898c8d382b17167c33e5c8af644a3519b47d'
SCORE_FIELDS = ('score', 'raw_score', 'keyword_score', 'zotero_score',
                'scoring_basis', 'missing_abstract_factor', 'ranking_strategy',
                'matched_interest', 'interest_direction_support',
                'interest_direction_reliability', 'interest_keyword_weight',
                'interest_zotero_weight')
CRITICAL = ('src/zot2dailypaper/reranker/base.py',
            'src/zot2dailypaper/reranker/strategies.py',
            'src/zot2dailypaper/reranker/experiments.py',
            'src/zot2dailypaper/reranker/local.py',
            'src/zot2dailypaper/reranker/onnx_encoder.py',
            'src/zot2dailypaper/reranker/embedding_cache.py',
            'src/zot2dailypaper/interest_profile.py',
            'src/zot2dailypaper/identity.py', 'src/zot2dailypaper/protocol.py',
            'src/zot2dailypaper/scores.py', 'src/zot2dailypaper/selection.py',
            'src/zot2dailypaper/preprint_interests.py',
            'src/zot2dailypaper/retriever/arxiv_retriever.py')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        f.write('\n')
    path.chmod(0o600)


def read_json(path):
    return json.loads(Path(path).read_text())


def verify_source(root=ROOT):
    tree = read_json(HERE / 'source_tree.snapshot.json')
    blobs = {p['path']: p['sha'] for p in tree['tree'] if p['type'] == 'blob'}
    checks = {}
    for name in CRITICAL:
        raw = (root / name).read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        checks[name] = {'git_blob': actual, 'expected': blobs[name], 'matches': actual == blobs[name],
                        'sha256': hashlib.sha256(raw).hexdigest()}
    if tree['sha'] != PIN or not all(v['matches'] for v in checks.values()):
        raise ValueError('scoring_source_pin_mismatch')
    return checks


def normalize_id(value):
    value = str(value or '').strip()
    doi = canonical_doi(value)
    if doi and doi.startswith('10.48550/arxiv.'):
        return 'arxiv:' + re.sub(r'v\d+$', '', doi.removeprefix('10.48550/arxiv.'))
    if doi:
        return 'doi:' + doi
    m = re.search(r'(?:arxiv:|arxiv\.org/(?:abs|pdf|html)/)?(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$', value, re.I)
    if m:
        return 'arxiv:' + m[1]
    return value.casefold().rstrip('/')


def identifiers(row):
    fields = [row.get(k) for k in ('id', 'canonical_id', 'doi', 'url', 'link')]
    fields += list(row.get('related_dois', [])) + list(row.get('identifiers', []))
    return {normalize_id(v) for v in fields if v}


def partition_families(rows, prior):
    """Explicit identity edges are transitive; title similarity only quarantines.

    Do not infer a DOI family from references, shared topic, or a generic title.
    Exact/fuzzy title matches without explicit identity remain uncertain.
    """
    all_rows = [*prior, *rows]
    parent = list(range(len(all_rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner = {}
    for i, row in enumerate(all_rows):
        for key in identifiers(row):
            if key in owner:
                parent[find(i)] = find(owner[key])
            owner[key] = i
    old = {find(i) for i in range(len(prior))}
    accepted, excluded, uncertain = [], [], []
    known_titles = [(title_key(x.get('title', '')), i) for i, x in enumerate(all_rows)]
    seen = set()
    for j, row in enumerate(rows, len(prior)):
        family = find(j)
        if family in old:
            excluded.append({'id': row['id'], 'reason': 'previously_exposed_family'})
            continue
        if family in seen:
            excluded.append({'id': row['id'], 'reason': 'duplicate_family'})
            continue
        title = title_key(row.get('title', ''))
        suspects = [all_rows[i]['id'] for other, i in known_titles[:j]
                    if title and other and find(i) != family and
                    (title == other or SequenceMatcher(None, title, other).ratio() >= .92)]
        if suspects:
            uncertain.append({'id': row['id'], 'reason': 'uncertain_title_family', 'possible_matches': suspects})
            continue
        seen.add(family)
        accepted.append(row)
    return accepted, excluded, uncertain


def fresh_validation(rows, prior, protocol):
    eligible, excluded, uncertain = partition_families(rows, prior)
    start, end = protocol['validation']['start_inclusive'], protocol['validation']['end_exclusive']
    ready = []
    for row in eligible:
        date = row.get('first_public_date', '')
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) or not start <= date < end:
            excluded.append({'id': row['id'], 'reason': 'outside_new_date_or_unknown_original_date'})
        elif not row.get('abstract', '').strip() or not row.get('abstract_complete', False):
            excluded.append({'id': row['id'], 'reason': 'full_abstract_unavailable'})
        elif not row.get('abstract_provenance'):
            excluded.append({'id': row['id'], 'reason': 'abstract_provenance_missing'})
        elif is_cover_title(row.get('title', '')):
            excluded.append({'id': row['id'], 'reason': 'publisher_cover'})
        else:
            ready.append(row)
    return ready, excluded, uncertain


class Embeddings:
    def __init__(self, pack, expected_identity):
        if pack.get('model_identity') != expected_identity:
            raise ValueError('embedding_runtime_identity_mismatch')
        if expected_identity.get('model') != MODEL or expected_identity.get('revision') != REVISION:
            raise ValueError('embedding_model_pin_mismatch')
        if expected_identity.get('dimension') != 768:
            raise ValueError('embedding_dimension_mismatch')
        if digest(pack['vectors']) != pack.get('vectors_sha256'):
            raise ValueError('embedding_payload_hash_mismatch')
        self.identity = copy.deepcopy(expected_identity)
        self.vectors = pack['vectors']

    def encode(self, texts):
        vectors = []
        for text in texts:
            key = hashlib.sha256(text.encode()).hexdigest()
            if key not in self.vectors:
                raise ValueError('missing_exact_text_embedding:' + key)
            v = np.asarray(self.vectors[key], dtype=np.float32)
            if v.shape != (self.identity['dimension'],) or not np.isfinite(v).all() or np.linalg.norm(v) == 0:
                raise ValueError('invalid_embedding')
            vectors.append(v)
        return np.stack(vectors) if vectors else np.empty((0, self.identity['dimension']), dtype=np.float32)


def _title_abstract(paper, mode):
    # Only this function changes between A and B. In particular, config.experiments
    # stays default, and strategy_settings continues to enforce the profile guard.
    return paper_text(paper, 'title_abstract')


_b_globals = dict(BaseReranker.rerank.__globals__, paper_text=_title_abstract)
_b_rerank = FunctionType(BaseReranker.rerank.__code__, _b_globals, 'rerank',
                         BaseReranker.rerank.__defaults__, BaseReranker.rerank.__closure__)


class ProfileFromEmbeddings(BaseReranker):
    def __init__(self, config, embeddings):
        super().__init__(config)
        self.embeddings = embeddings

    def get_similarity_score(self, left, right):
        # Same float32 normalization and matmul as production LocalReranker._similarity.
        a, b = self.embeddings.encode(left), self.embeddings.encode(right)
        a = a / np.linalg.norm(a, axis=1, keepdims=True)
        b = b / np.linalg.norm(b, axis=1, keepdims=True)
        return a @ b.T


class TitleAbstractProfile(ProfileFromEmbeddings):
    rerank = _b_rerank


def as_paper(row):
    paper = Paper(source=row['source'], title=row['title'], abstract=row.get('abstract', ''),
                 authors=[], url=row.get('url', row.get('link', '')),
                 doi=row.get('doi'), related_dois=row.get('related_dois', []),
                 publication_kind=row.get('publication_kind'), journal=row.get('journal'))
    paper._offline_id = row['id']
    return paper


def as_corpus(row):
    return CorpusPaper(title=row['title'], abstract=row['abstract'], doi=row.get('doi'),
                       added_date=datetime.fromisoformat(row['added_date'].replace('Z', '+00:00')),
                       paths=list(row.get('paths', [])))


def run_profile(rows, corpus, config, embeddings, title_abstract=False):
    cls = TitleAbstractProfile if title_abstract else ProfileFromEmbeddings
    ranker = cls(config, embeddings)
    papers = [as_paper(row) for row in rows]
    by_object = {id(p): row['id'] for row, p in zip(rows, papers)}
    ranked = ranker.rerank(papers, [as_corpus(x) for x in corpus])
    records = {by_object[id(p)]: {key: getattr(p, key) for key in SCORE_FIELDS} for p in ranked}
    return {'order': [by_object[id(p)] for p in ranked], 'rows': records,
            'direction_support': getattr(ranker, 'direction_profile_support', [])}


def reference_assignments(corpus, keywords, embeddings, title_abstract=False):
    ordered = sorted(corpus, key=lambda x: as_corpus(x).added_date, reverse=True)
    mode = 'title_abstract' if title_abstract else 'abstract'
    docs = embeddings.encode([paper_text(as_corpus(x), mode) for x in ordered])
    anchors = embeddings.encode(keywords)
    docs /= np.linalg.norm(docs, axis=1, keepdims=True)
    anchors /= np.linalg.norm(anchors, axis=1, keepdims=True)
    affinity = docs @ anchors.T
    winners = affinity.argmax(axis=1)
    return [{'id': row['id'], 'direction_index': int(w), 'affinity': float(affinity[i, w])}
            for i, (row, w) in enumerate(zip(ordered, winners))]


def channel_orders(rows, embeddings, protocol):
    docs = embeddings.encode([paper_text(as_paper(x), 'title_abstract') for x in rows])
    docs /= np.linalg.norm(docs, axis=1, keepdims=True)
    channels = {}
    for channel in protocol['C']['channels']:
        anchor = embeddings.encode([channel['task_description']])
        anchor /= np.linalg.norm(anchor, axis=1, keepdims=True)
        scores = (docs @ anchor.T).ravel()
        order = sorted(range(len(rows)), key=lambda i: (-float(scores[i]), rows[i]['id']))
        channels[channel['id']] = {'order': [rows[i]['id'] for i in order],
                                    'scores': {r['id']: float(s) for r, s in zip(rows, scores)}}
    return channels


def fuse(base_order, channels, protocol):
    cfg = protocol['C']
    orders = {'B': base_order, **{k: v['order'] for k, v in channels.items()}}
    totals = dict.fromkeys(base_order, 0.0)
    contributions = {key: {} for key in base_order}
    for name, order in orders.items():
        for rank, key in enumerate(order[:cfg['lane_depth']], 1):
            value = cfg['weights'][name] / (cfg['rrf_k'] + rank)
            totals[key] += value
            contributions[key][name] = value
    positions = {key: i for i, key in enumerate(base_order)}
    order = sorted(base_order, key=lambda key: (-totals[key], positions[key], key))
    return {'order': order, 'rrf_scores': totals, 'contributions': contributions}


def selection(result, rows, config, seed, pending=(), rng=None):
    by_id = {row['id']: row for row in rows}
    objects = []
    ids = {}
    minimum = minimum_score(config.executor)
    low = []
    for key in result['order']:
        # C uses B eligibility scores, never reinterpret RRF as production relevance.
        if result['rows'][key]['score'] < minimum:
            low.append(key)
            continue
        p = as_paper(by_id[key])
        ids[id(p)] = key
        objects.append(p)
    pending_papers = [as_paper(p) for p in pending]
    for p, row in zip(pending_papers, pending):
        p.recommendation_group = row.get('recommendation_group')
    chosen = select_papers(objects, dict(config.executor.quotas), pending_papers, rng or random.Random(seed))
    selected = {group: [ids[id(p)] for p in chosen if p.recommendation_group == group]
                for group in ('journals', 'preprints', 'random')}
    selected['main_order'] = [ids[id(p)] for p in chosen if p.recommendation_group != 'random']
    selected['low_score'] = low
    selected['quota_evicted'] = [ids[id(p)] for p in objects if id(p) not in {id(x) for x in chosen}]
    return selected


def blind_pool(results, rows, protocol):
    cfg = protocol['blind_pool']
    reasons = {}
    for name, result in results.items():
        for key in result['selection']['main_order'][:cfg['head_k']]:
            reasons.setdefault(key, []).append('head:' + name)
    ids = {row['id'] for row in rows}
    positions = {name: {key: i for i, key in enumerate(r['order'])} for name, r in results.items()}
    def disagreement(key):
        ranks = [p.get(key, len(p)) for p in positions.values()]
        return max(ranks) - min(ranks)
    differing = sorted(ids - reasons.keys(), key=lambda key: (-disagreement(key), key))
    for key in differing[:cfg['disagreement_n']]:
        reasons[key] = ['disagreement']
    rng = random.Random(protocol['seed'])
    by_stratum = {}
    for row in sorted(rows, key=lambda x: x['id']):
        if row['id'] not in reasons:
            # Source strata are observational metadata, not expected relevance.
            s = (row.get('pool', 'common'), row['source'], row.get('publication_kind', 'preprint'))
            by_stratum.setdefault(s, []).append(row['id'])
    for pool in by_stratum.values():
        rng.shuffle(pool)
    strata = sorted(by_stratum)
    picked = 0
    while picked < cfg['stratified_random_n'] and strata:
        for s in list(strata):
            if picked == cfg['stratified_random_n']:
                break
            reasons[by_stratum[s].pop()] = ['stratified_random']
            picked += 1
            if not by_stratum[s]:
                strata.remove(s)
    order = sorted(reasons)
    rng.shuffle(order)
    mapping = {f'N{i:03}': key for i, key in enumerate(order, 1)}
    return mapping, reasons


def judged_metrics(order, labels, k):
    # Incomplete judgments block point metrics; omitted papers are never zero.
    top = order[:k]
    missing = [x for x in top if x not in labels]
    if missing:
        return {'status': 'incomplete_topk_judgments', 'missing': missing,
                'judged_fraction': (len(top) - len(missing)) / max(1, len(top))}
    if any(type(x) is not int or x not in range(4) for x in labels.values()):
        raise ValueError('labels_must_be_integers_0_to_3')
    dcg = sum((2 ** labels[x] - 1) / math.log2(i + 2) for i, x in enumerate(top))
    ideal = sorted(labels.values(), reverse=True)[:k]
    idcg = sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(ideal))
    return {'status': 'judged_pool_only', 'precision': sum(labels[x] >= 2 for x in top) / max(1, len(top)),
            'ndcg': dcg / idcg if idcg else None, 'gain': '2**label-1',
            'must_read_ids': [x for x in top if labels[x] == 3],
            'denominator_note': 'IDCG over judged blind pool; whole-pool recall unavailable'}


def replay_check(actual, expected):
    if not expected:
        return {'status': 'not_verified', 'reason': 'full_production_gold_record_missing'}
    errors = []
    if actual['order'] != expected['order']:
        errors.append('full_order_mismatch')
    for key in actual['order']:
        if key not in expected.get('rows', {}):
            errors.append('missing_gold_row:' + key)
            continue
        for field in SCORE_FIELDS:
            a, b = actual['rows'][key][field], expected['rows'][key].get(field)
            equal = math.isclose(a, b, rel_tol=0, abs_tol=1e-6) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else a == b
            if not equal:
                errors.append(key + ':' + field)
    return {'status': 'verified' if not errors else 'failed', 'errors': errors,
            'compared_rows': len(actual['order']), 'tolerance_abs': 1e-6}


def replay_selection(actual, expected, rows, config, pending=()):
    """Replay captured membership, not the unrecorded SystemRandom entropy stream."""
    if not expected or 'selection' not in expected:
        return {'status': 'not_verified', 'reason': 'captured_production_selection_missing'}
    gold = expected['selection']

    class RecordedDraw:
        def sample(self, remaining, k):
            lookup = {p._offline_id: p for p in remaining}
            keys = gold.get('random', [])
            if len(keys) != k or len(set(keys)) != k or any(x not in lookup for x in keys):
                raise ValueError('captured_random_draw_not_eligible')
            return [lookup[x] for x in keys]

    try:
        replayed = selection(actual, rows, config, 0, pending, RecordedDraw())
    except ValueError as exc:
        return {'status': 'failed', 'reason': str(exc)}
    errors = [group for group in ('journals', 'preprints', 'random', 'main_order')
              if replayed[group] != gold.get(group)]
    return {'status': 'failed' if errors else 'verified', 'mismatched_fields': errors,
            'random_note': 'recorded draw eligibility/order verified; no claim to reproduce SystemRandom entropy'}


def audit_inputs(snapshot, ranked, selected):
    records = {r['id']: {'id': r['id'], 'pool': r.get('pool', 'common'),
                         'source': r['source'], 'abstract_missing_after_recovery': not bool(r.get('abstract', '').strip()),
                         'before_recovery_known': 'abstract_before_recovery' in r,
                         'abstract_recovered': (not bool(r.get('abstract_before_recovery', '').strip()) and
                                                bool(r.get('abstract', '').strip())) if 'abstract_before_recovery' in r else None,
                         'trace': []} for r in snapshot['candidates']}
    event_counts = {}
    for event in snapshot.get('candidate_events', []):
        reason = event.get('reason', 'unspecified')
        event_counts[reason] = event_counts.get(reason, 0) + 1
        key = event.get('id')
        if key:
            records.setdefault(key, {'id': key, 'trace': []})['trace'].append(event)
    for rank, key in enumerate(ranked['order'], 1):
        row = records[key]
        row['rank_after_recovery'] = rank
        row['score_after_recovery'] = ranked['rows'][key]['score']
        row['direction_after_recovery'] = ranked['rows'][key]['matched_interest']
        row['selection_disposition'] = ('low_score' if key in selected['low_score'] else
                                       'main_selected' if key in selected['main_order'] else
                                       'random_exploration' if key in selected['random'] else
                                       'quota_evicted')
    return {'event_counts': event_counts, 'papers': records,
            'source_attempts': snapshot.get('source_attempts', []),
            'unobserved_papers': 'unknown; unavailable source coverage is never a negative label',
            'trace_completeness': snapshot.get('trace_completeness', 'unknown')}


def input_manifest(snapshot, pack, protocol):
    return {'schema': 'jina_abc_input_manifest_v1', 'source_commit': PIN,
            'snapshot_sha256': digest(snapshot), 'config_sha256': digest(snapshot['config']),
            'model_identity_sha256': digest(snapshot['model_identity']),
            'corpus_sha256': digest(snapshot['corpus']),
            'candidate_order_sha256': digest([x['id'] for x in snapshot['candidates']]),
            'candidates_before_recovery_sha256': digest([{'id': row['id'], 'abstract': row.get('abstract_before_recovery')}
                                                        for row in snapshot['candidates']]),
            'before_recovery_text_complete': all('abstract_before_recovery' in r for r in snapshot['candidates']),
            'candidates_after_recovery_sha256': digest(snapshot['candidates']),
            'vectors_sha256': pack['vectors_sha256'], 'protocol_sha256': digest(protocol),
            'history_sha256': digest(snapshot.get('history', {})),
            'events_sha256': digest(snapshot.get('candidate_events', [])),
            'rows': {row['id']: {'title_sha256': hashlib.sha256(row['title'].encode()).hexdigest(),
                                 'abstract_sha256': hashlib.sha256(row.get('abstract', '').encode()).hexdigest(),
                                 'pool': row.get('pool', 'common')}
                     for row in snapshot['candidates']}}


def preflight(snapshot, pack, protocol):
    blockers = []
    if digest(protocol) != read_json(HERE / 'freeze_receipt.private.json')['protocol_sha256']:
        blockers.append('frozen_protocol_hash_mismatch')
    if snapshot.get('source_commit') != PIN:
        blockers.append('source_commit_mismatch')
    for key in ('config', 'model_identity', 'corpus', 'candidates', 'candidate_events', 'history'):
        if key not in snapshot:
            blockers.append('missing_snapshot_field:' + key)
    if not snapshot.get('corpus'):
        blockers.append('actual_filtered_zotero_corpus_missing')
    if not snapshot.get('candidates'):
        blockers.append('captured_candidate_pool_missing')
    if not pack:
        blockers.append('matching_local_embeddings_missing')
    if not blockers:
        cfg = OmegaConf.create(snapshot['config'])
        strategy, _ = strategy_settings(cfg)
        if strategy != 'multi_interest_profile':
            blockers.append('wrong_baseline_strategy')
        expected = read_json(HERE / 'runtime_config.frozen.private.json')
        for field in ('interest_profile', 'reranker', 'executor'):
            if OmegaConf.to_container(cfg[field], resolve=True) != expected[field]:
                blockers.append('runtime_ranking_config_mismatch:' + field)
        if len({r['id'] for r in snapshot['candidates']}) != len(snapshot['candidates']):
            blockers.append('duplicate_candidate_ids')
        if len({r['id'] for r in snapshot['corpus']}) != len(snapshot['corpus']):
            blockers.append('duplicate_corpus_ids')
        for row in snapshot['candidates']:
            if any(k not in row for k in ('source', 'title', 'abstract', 'url', 'abstract_before_recovery', 'retrieved_at')):
                blockers.append('incomplete_candidate_snapshot:' + row['id'])
        identity = snapshot['model_identity']
        for key in ('model', 'revision', 'dimension', 'backend', 'dtype', 'device', 'threads',
                    'encode_kwargs', 'prompts', 'max_seq_length', 'libraries'):
            if key not in identity:
                blockers.append('incomplete_model_identity:' + key)
        if any(not r.get('abstract', '').strip() for r in snapshot['corpus']):
            blockers.append('empty_reference_abstract')
        if not snapshot.get('source_attempts'):
            blockers.append('source_request_coverage_trace_missing')
    return {'status': 'ready' if not blockers else 'blocked_before_inference', 'blockers': blockers,
            'paid_calls': 0, 'production_writes': 0}


def evaluate(snapshot, pack, protocol, prior):
    source = verify_source()
    gate = preflight(snapshot, pack, protocol)
    if digest(prior) != protocol['excluded_studies_sha256']:
        gate['blockers'].append('frozen_exclusion_history_hash_mismatch')
    if gate['blockers']:
        gate['status'] = 'blocked_before_inference'
        return gate
    cfg = OmegaConf.create(snapshot['config'])
    embeddings = Embeddings(pack, snapshot['model_identity'])
    # Full replay precedes new-date exclusions and retains the original input order.
    common_all = [x for x in snapshot['candidates'] if x.get('pool', 'common') == 'common']
    full_A = run_profile(common_all, snapshot['corpus'], cfg, embeddings)
    reproduction = replay_check(full_A, snapshot.get('expected_A'))
    membership = replay_selection(full_A, snapshot.get('expected_A'), common_all, cfg, snapshot.get('pending', []))
    if reproduction['status'] == 'failed' or membership['status'] == 'failed':
        return {'status': 'blocked_reproduction_mismatch', 'reproduction': reproduction,
                'production_selection_replay': membership, 'paid_calls': 0}
    if (not snapshot.get('synthetic_fixture_only', False) and
            (reproduction['status'] != 'verified' or membership['status'] != 'verified')):
        return {'status': 'blocked_unverified_production_baseline', 'paid_calls': 0,
                'reproduction': reproduction, 'production_selection_replay': membership,
                'full_A_replay': full_A,
                'reason': 'capture unchanged production full score/order and selection records before creating a real blind pack'}
    ready, excluded, uncertain = fresh_validation(snapshot['candidates'], prior, protocol)
    common = [x for x in ready if x.get('pool', 'common') == 'common']
    expansion = [x for x in ready if x.get('pool') == 'expanded']
    if not common:
        return {'status': 'blocked_no_new_complete_common_candidates', 'excluded': excluded,
                'uncertain_families': uncertain, 'reproduction': reproduction, 'paid_calls': 0}
    results = {name: run_profile(common, snapshot['corpus'], cfg, embeddings, b)
               for name, b in [('A', False), ('B', True)]}
    fixed_channels = channel_orders(common, embeddings, protocol)
    results['C_fixed'] = dict(fuse(results['B']['order'], fixed_channels, protocol), rows=results['B']['rows'])
    expanded_controls = None
    if expansion:
        union = common + expansion
        a_plus = run_profile(union, snapshot['corpus'], cfg, embeddings)
        b_plus = run_profile(union, snapshot['corpus'], cfg, embeddings, True)
        expanded_channels = channel_orders(union, embeddings, protocol)
        results['C_expanded'] = dict(fuse(b_plus['order'], expanded_channels, protocol), rows=b_plus['rows'])
        expanded_controls = {'A_same_expanded_pool': a_plus, 'B_same_expanded_pool': b_plus,
                             'channels': expanded_channels}
    else:
        union = common
    for name, result in results.items():
        result['selection'] = selection(result, union if name == 'C_expanded' else common, cfg, protocol['seed'])
    a_main = set(results['A']['selection']['main_order'])
    for name, result in results.items():
        result['A_displaced'] = sorted(a_main - set(result['selection']['main_order']))
        result['new_main_vs_A'] = sorted(set(result['selection']['main_order']) - a_main)
    mapping, reasons = blind_pool(results, union, protocol)
    full_selection = selection(full_A, common_all, cfg, protocol['seed'], snapshot.get('pending', []))
    return {'status': 'offline_comparison_ready_for_labels', 'paid_calls': 0,
            'synthetic_fixture_only': snapshot.get('synthetic_fixture_only', False),
            'source_checks': source, 'manifest': input_manifest(snapshot, pack, protocol),
            'full_A_replay': full_A, 'reproduction': reproduction,
            'production_selection_replay': membership, 'results': results,
            'A_reference_assignments': reference_assignments(snapshot['corpus'], cfg.interest_profile.keywords, embeddings),
            'B_reference_assignments': reference_assignments(snapshot['corpus'], cfg.interest_profile.keywords, embeddings, True),
            'fixed_pool_channels': fixed_channels, 'expanded_pool_controls': expanded_controls,
            'blind_id_to_candidate_id': mapping, 'private_sampling_reasons': reasons,
            'excluded': excluded, 'uncertain_families': uncertain,
            'input_audit': audit_inputs(snapshot, full_A, full_selection),
            'C_expansion_status': 'available' if expansion else 'not_measured_no_expanded_inputs'}


def write_blind_csv(path, evaluation, snapshot):
    by_id = {r['id']: r for r in snapshot['candidates']}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['blind_id', 'title', 'source', 'journal_or_venue', 'link',
                         'original_abstract', 'abstract_status', 'abstract_source',
                         'abstract_source_url', 'relevance_0_to_3', 'optional_reason'])
        for blind_id, key in evaluation['blind_id_to_candidate_id'].items():
            row = by_id[key]
            abstract = row.get('abstract', '')
            status = ('missing' if not abstract.strip() else
                      'available' if row.get('abstract_complete', False) else 'incomplete')
            provenance = row.get('abstract_provenance') or {}
            if isinstance(provenance, str):
                provenance = {'source': provenance}
            # Captured source text, never a generated recommendation explanation.
            writer.writerow([blind_id, row['title'], row.get('source', ''),
                             row.get('journal') or row.get('publication_venue', ''),
                             row.get('url', row.get('link', '')), abstract, status,
                             provenance.get('source', ''), provenance.get('url', ''), '', ''])
    Path(path).chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--vectors', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    protocol = read_json(HERE / 'protocol.frozen.private.json')
    if not args.snapshot or not args.vectors:
        result = {'status': 'blocked_before_inference', 'paid_calls': 0,
                  'blockers': [k for k, v in [('actual_private_snapshot', args.snapshot),
                                            ('matching_local_embeddings', args.vectors)] if not v],
                  'source_checks': verify_source(), 'protocol_sha256': digest(protocol)}
    else:
        snapshot, pack = read_json(args.snapshot), read_json(args.vectors)
        result = evaluate(snapshot, pack, protocol, read_json(HERE / 'prior49.exclusions.private.json'))
        if result['status'] == 'offline_comparison_ready_for_labels':
            args.out.mkdir(parents=True, exist_ok=True)
            write_blind_csv(args.out / 'blind_evaluation.private.csv', result, snapshot)
    private_json(args.out / 'result.private.json', result)
    # Public console summary contains no profile, corpus membership, scores or labels.
    print(json.dumps({'status': result['status'], 'paid_calls': 0,
                      'blockers': result.get('blockers', []), 'output_saved_locally': True}))


if __name__ == '__main__':
    main()
