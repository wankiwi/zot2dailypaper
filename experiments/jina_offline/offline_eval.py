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
                    (title == other or _similar_title(title, other))]
        if suspects:
            uncertain.append({'id': row['id'], 'reason': 'uncertain_title_family', 'possible_matches': suspects})
            continue
        seen.add(family)
        accepted.append(row)
    return accepted, excluded, uncertain


def _similar_title(left, right):
    # Both quick ratios are upper bounds: this avoids needless expensive scans
    # without changing the frozen .92 similarity decision.
    matcher = SequenceMatcher(None, left, right)
    return (matcher.real_quick_ratio() >= .92 and matcher.quick_ratio() >= .92
            and matcher.ratio() >= .92)




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
    pool_labels = [labels[x] for x in order if x in labels]
    ideal = sorted(pool_labels, reverse=True)[:k]
    idcg = sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(ideal))
    unknown = sum(x not in labels for x in order)
    high_ideal = sorted(pool_labels + [3]*unknown, reverse=True)[:k]
    high_idcg = sum((2**grade-1)/math.log2(i+2) for i, grade in enumerate(high_ideal))
    return {'status': 'complete_topk_judgments', 'precision': sum(labels[x] >= 2 for x in top) / max(1, len(top)),
            'dcg': dcg, 'pooled_ndcg': dcg / idcg if idcg else None,
            'complete_pool_ndcg': (dcg / idcg if idcg else None) if not unknown else None,
            'complete_pool_ndcg_bounds': [dcg/high_idcg if high_idcg else None, dcg/idcg if idcg else None],
            'unjudged_in_complete_pool': unknown, 'gain': '2**label-1',
            'must_read_ids': [x for x in top if labels[x] == 3],
            'denominator_note': 'P/DCG exact for this judged prefix; pooled IDCG uses only judged members of this order; full-pool NDCG unknown unless all pool members judged or bounds coincide; no whole-pool recall'}


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
    if not snapshot.get('query_cutoff') or not snapshot.get('source_days'):
        blockers.append('immutable_source_date_snapshot_missing')
    if digest(protocol['C']) != read_json(HERE / 'freeze_receipt.private.json')['frozen_C_sha256']:
        blockers.append('frozen_channels_or_fusion_changed')
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


def score_full_pool(rows, corpus, config, embeddings, protocol, pending=()):
    arms = {name: run_profile(rows, corpus, config, embeddings, use_titles)
            for name, use_titles in [('A', False), ('B', True)]}
    channels = channel_orders(rows, embeddings, protocol)
    arms['C'] = dict(fuse(arms['B']['order'], channels, protocol), rows=arms['B']['rows'])
    for name, arm in arms.items():
        arm['model_order'] = list(arm['order'])
        arm['stable_model_rank'] = {key: i for i, key in enumerate(arm['order'], 1)}
        arm['selection'] = selection(arm, rows, config, protocol['seed'], pending)
        selected = arm['selection']
        arm['recommendation_order'] = selected['main_order'] + selected['random']
        arm['main_rank'] = {key: i for i, key in enumerate(selected['main_order'], 1)}
        arm['random_position'] = {key: i for i, key in enumerate(selected['random'], 1)}
        arm['selection_disposition'] = {
            key: 'main_selected' if key in arm['main_rank'] else
                 'random_exploration' if key in arm['random_position'] else
                 'min_score_filtered' if key in selected['low_score'] else 'quota_not_selected'
            for key in arm['order']}
        scores = arm['rrf_scores'] if name == 'C' else {key: v['score'] for key, v in arm['rows'].items()}
        arm['unrounded_ranking_scores'] = scores
        groups = {}
        for key in arm['order']:
            groups.setdefault(scores[key], []).append(key)
        arm['tie_groups'] = [group for group in groups.values() if len(group) > 1]
        arm['tie_policy'] = 'B position then stable id' if name == 'C' else 'production stable input order'
    a_main = set(arms['A']['selection']['main_order'])
    for arm in arms.values():
        arm['A_displaced'] = sorted(a_main - set(arm['selection']['main_order']))
        arm['new_main_vs_A'] = sorted(set(arm['selection']['main_order']) - a_main)
    assignments = [reference_assignments(corpus, list(config.interest_profile.keywords), embeddings, titles)
                   for titles in (False, True)]
    transitions = {}
    for left, right in zip(*assignments):
        key = f"{left['direction_index']}->{right['direction_index']}"
        transitions[key] = transitions.get(key, 0) + 1
    return {'candidate_ids': [r['id'] for r in rows], 'candidate_snapshot_sha256': digest(rows),
            'arms': arms, 'channels': channels,
            'reference_assignment_audit': {'note': 'anchor argmax audit; profile min-affinity/support filtering remains in original scorer; no reference text/ids exported',
                'reference_count': len(corpus), 'direction_transitions': transitions,
                'changed_count': sum(a['direction_index'] != b['direction_index'] for a,b in zip(*assignments)),
                'assignment_sha256': [digest(value) for value in assignments]}}


def validation_classification(rows, prior, asof, source_days, requirements=None):
    from snapshot_policy import date_eligibility
    accepted, excluded, uncertain = partition_families(rows, prior)
    status = {r['id']: r['reason'] for r in [*excluded, *uncertain]}
    for row in accepted:
        valid, reason = date_eligibility(row, asof, source_days)
        if valid and (requirements or {}).get('require_complete_abstract') and (
                not row.get('abstract', '').strip() or not row.get('abstract_complete', False)):
            valid, reason = False, 'complete_abstract_unavailable'
        status[row['id']] = 'eligible' if valid else reason
    return status


def plan_actual_heads(pool, rows, eligibility, protocol, scope):
    """True heads of one prequalified validation pool; never score-conditional refill."""
    cfg = protocol['blind_pool']
    reasons = {}
    for name, arm in pool['arms'].items():
        for key in arm['model_order'][:cfg['head_k']]:
            reasons.setdefault(key, []).append('actual_model_head:' + name)
        for key in arm['selection']['main_order'][:cfg['head_k']]:
            reasons.setdefault(key, []).append('actual_main_head:' + name)
        for key in arm['selection']['random']:
            reasons.setdefault(key, []).append('actual_random:' + name)
    blocked = [{'id': key, 'reason': eligibility[key]} for key in reasons if eligibility[key] != 'eligible']
    by_id = {r['id']: r for r in rows}
    info_gaps = [key for key in reasons if not by_id[key].get('abstract', '').strip()
                 or not by_id[key].get('abstract_complete', False)]
    required_main = {key for key, why in reasons.items() if any(x.startswith('actual_main_head:') for x in why)}
    required_model = {key for key, why in reasons.items() if any(x.startswith('actual_model_head:') for x in why)}
    rng = random.Random(protocol['seed'] + (1 if scope.endswith('expanded') else 0))
    strata = {}
    for row in sorted(rows, key=lambda r: r['id']):
        if row['id'] not in reasons and eligibility[row['id']] == 'eligible':
            strata.setdefault((row['source'], row.get('publication_kind') or ''), []).append(row['id'])
    for group in strata.values():
        rng.shuffle(group)
    keys = sorted(strata)
    for _ in range(cfg['stratified_random_n']):
        if not keys:
            break
        group = keys[_ % len(keys)]
        key = strata[group].pop()
        reasons[key] = ['source_stratified_random']
        if not strata[group]:
            keys.remove(group)
    order = sorted(key for key in reasons if eligibility[key] == 'eligible')
    rng.shuffle(order)
    mapping = {f'R{i:03}': key for i, key in enumerate(order, 1)}
    raw_model_unjudged_heads = {
        name: [key for key in arm['model_order'][:cfg['head_k']] if key not in mapping.values()]
        for name, arm in pool['arms'].items()}
    first = set()
    for arm in pool['arms'].values():
        first.update(arm['model_order'][:10])
        first.update(arm['selection']['main_order'][:10])
    stage1 = [blind for blind,key in mapping.items() if key in first]
    stage2 = [blind for blind,key in mapping.items() if key not in first]
    return {'scope': scope, 'blind_id_to_candidate_id': mapping, 'sampling_reasons': reasons,
            'display_stages': {'1':stage1, '2':stage2},
            'stage_policy':'precomputed model/main top10 union first, remaining frozen top20/random pool second; preserve full blind order/ids; never label-adaptive',
            'stage1_theoretical_maximum':60, 'stage1_when_heads_equal_maximum':30,
            'stage1_metric_scope':'fully judged top10 only; pooled NDCG denominator remains limited; no top20 point metrics before complete judgments',

            'required_main_ids': sorted(required_main), 'required_model_ids': sorted(required_model),
            'blocked_prior_or_unknown_heads': blocked,
            'head_information_gaps': info_gaps, 'model_heads_not_in_blind_pool': raw_model_unjudged_heads,
            'theoretical_maximum': 6*cfg['head_k'] + 3*5 + cfg['stratified_random_n'],
            'when_model_and_main_heads_equal_maximum': 3*cfg['head_k'] + 3*5 + cfg['stratified_random_n'],
            'blind_count': len(mapping), 'silent_truncation': False,
            'ready_for_independent_main_metrics': not blocked and not info_gaps,
            'metric_scope': 'raw model and actual quota main prefixes reported separately; all their mandatory head judgments retained'}


def evaluate(snapshot, pack, protocol, prior):
    checks = verify_source()
    gate = preflight(snapshot, pack, protocol)
    if digest(prior) != protocol['excluded_studies_sha256']:
        gate['blockers'].append('frozen_exclusion_history_hash_mismatch')
    if gate['blockers']:
        return dict(gate, status='blocked_before_inference')
    cfg = OmegaConf.create(snapshot['config'])
    embeddings = Embeddings(pack, snapshot['model_identity'])
    common = [r for r in snapshot['candidates'] if r.get('pool', 'common') == 'common']
    expansion = [r for r in snapshot['candidates'] if r.get('pool') == 'expanded']
    asof = datetime.fromisoformat(snapshot['query_cutoff'].replace('Z', '+00:00'))
    eligibility = validation_classification(snapshot['candidates'], prior, asof, snapshot['source_days'], protocol.get('validation'))
    validation_common = [r for r in common if eligibility[r['id']] == 'eligible']
    validation_expansion = [r for r in expansion if eligibility[r['id']] == 'eligible']
    # Production audit is retained intact. It is never called the independent
    # validation pool; its A record must match the unchanged full-pool scorer.
    production_pool = score_full_pool(common, snapshot['corpus'], cfg, embeddings, protocol, snapshot.get('pending', []))
    full_A = production_pool['arms']['A']
    reproduction = replay_check(full_A, snapshot.get('expected_A'))
    membership = replay_selection(full_A, snapshot.get('expected_A'), common, cfg, snapshot.get('pending', []))
    if reproduction['status'] == 'failed' or membership['status'] == 'failed':
        return {'status': 'blocked_reproduction_mismatch', 'reproduction': reproduction,
                'production_selection_replay': membership, 'paid_calls': 0}
    if not snapshot.get('synthetic_fixture_only', False) and (reproduction['status'] != 'verified' or membership['status'] != 'verified'):
        return {'status': 'blocked_unverified_production_baseline', 'paid_calls': 0,
                'reproduction': reproduction, 'production_selection_replay': membership}
    # Eligibility is determined without scores or labels, before ANY scoring or
    # quota/random selection of the independent validation pool.
    primary_pool = score_full_pool(validation_common, snapshot['corpus'], cfg, embeddings, protocol, snapshot.get('pending', [])) if validation_common else None
    primary_plan = plan_actual_heads(primary_pool, validation_common, eligibility, protocol, 'independent_common') if primary_pool else None
    source_failures = [x['source'] for x in snapshot['source_attempts']
                       if x.get('scope') == 'common' and x.get('status') in ('failed', 'partial')]
    if primary_plan:
        primary_plan['source_failures'] = source_failures
        primary_plan['ready_for_independent_main_metrics'] &= not source_failures
    expanded_pool = expanded_plan = expanded_production = None
    expansion_attempts = [x for x in snapshot['source_attempts'] if x.get('scope') == 'expanded']
    expansion_failed = any(x.get('status') in ('failed', 'partial', 'skipped_access_refusal') for x in expansion_attempts)
    if expansion and not expansion_failed:
        expanded_production = score_full_pool(common+expansion, snapshot['corpus'], cfg, embeddings, protocol, snapshot.get('pending', []))
        validation_union = validation_common + validation_expansion
        if validation_union:
            expanded_pool = score_full_pool(validation_union, snapshot['corpus'], cfg, embeddings, protocol, snapshot.get('pending', []))
            expanded_plan = plan_actual_heads(expanded_pool, validation_union, eligibility, protocol, 'independent_expanded')
            expanded_plan['ready_for_independent_main_metrics'] &= not source_failures
    excluded_counts = {}
    for key, reason in eligibility.items():
        if reason != 'eligible':
            excluded_counts[reason] = excluded_counts.get(reason, 0)+1
    return {'status': 'two_pools_captured' if primary_pool and not source_failures else 'no_ready_independent_pool',
            'schema': 'jina_two_pool_v2', 'paid_calls': 0,
            'synthetic_fixture_only': snapshot.get('synthetic_fixture_only', False),
            'source_checks': checks, 'manifest': input_manifest(snapshot, pack, protocol),
            'query_cutoff': snapshot['query_cutoff'], 'source_days': snapshot['source_days'],
            'model_identity': snapshot['model_identity'],
            'reproduction': reproduction, 'production_selection_replay': membership,
            'production_full_pool_audit': production_pool,
            'validation_common_full_pool': primary_pool,
            'expanded_production_full_pool_audit': expanded_production,
            'validation_expanded_full_pool_controls': expanded_pool,
            'pool_separation': {'production_common_count':len(common),
                'independent_common_count':len(validation_common),
                'production_expansion_count':len(expansion),
                'independent_expansion_count':len(validation_expansion),
                'excluded_by_reason':excluded_counts,
                'validation_membership_sha256':digest([r['id'] for r in validation_common]),
                'eligibility_applied_before_validation_scoring':True,
                'claim':'true heads of the unexposed eligible validation pool; not unfiltered production heads',
                'pending_quotas':'same captured pending reservations reused for all arms; pending documents not relabeled'},
            'validation_eligibility': eligibility, 'primary_blind_plan': primary_plan,
            'expanded_blind_plan': expanded_plan,
            'C_expansion_status': 'failed_source' if expansion_failed else 'captured' if expansion else 'empty',
            'input_audit': audit_inputs(snapshot, full_A, full_A['selection']),
            'blind_id_to_candidate_id': primary_plan['blind_id_to_candidate_id'] if primary_plan else {},
            'new_date_subset_cropped_before_scoring': False,
            'unjudged_policy': 'unknown; never 0; pooled NDCG is not complete-pool NDCG'}


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


def write_staged_blind_csv(directory, evaluation, snapshot):
    """Presentation only: fixed partition, original blind IDs, no label input."""
    plan = evaluation.get('primary_blind_plan') or {}
    if not plan.get('ready_for_independent_main_metrics'):
        raise ValueError('independent_blind_plan_not_ready')
    mapping = evaluation['blind_id_to_candidate_id']
    stages = plan['display_stages']
    first, second = stages['1'], stages['2']
    if (len(set(first+second)) != len(first+second) or set(first+second) != set(mapping)
            or any([key for key in mapping if key in set(stage)] != stage for stage in (first,second))):
        raise ValueError('fixed_display_partition_mismatch')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = []
    for number, stage in [('1',first),('2',second)]:
        subsets = [(f'phase{number}.private.csv',stage)]
        subsets += [(f'phase{number}_batch{i//5+1:02}.private.csv',stage[i:i+5]) for i in range(0,len(stage),5)]
        for filename, ids in subsets:
            value = {'blind_id_to_candidate_id':{key:mapping[key] for key in ids}}
            file = directory / filename
            write_blind_csv(file,value,snapshot)
            files.append(file)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--vectors', type=Path)
    parser.add_argument('--export-evaluation', type=Path,
                        help='offline presentation export from an already decrypted sealed result; no vectors/API needed')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.export_evaluation:
        if args.snapshot or args.vectors:
            parser.error('export mode does not accept inference inputs')
        result = read_json(args.export_evaluation)
        files = write_staged_blind_csv(args.out,result,{'candidates':result['public_candidates']})
        print(json.dumps({'status':'fixed_stages_exported','paid_calls':0,'files_written':len(files)}))
        return
    protocol = read_json(HERE / 'protocol.frozen.private.json')
    if not args.snapshot or not args.vectors:
        result = {'status': 'blocked_before_inference', 'paid_calls': 0,
                  'blockers': [k for k, v in [('actual_private_snapshot', args.snapshot),
                                            ('matching_local_embeddings', args.vectors)] if not v],
                  'source_checks': verify_source(), 'protocol_sha256': digest(protocol)}
    else:
        snapshot, pack = read_json(args.snapshot), read_json(args.vectors)
        result = evaluate(snapshot, pack, protocol, read_json(HERE / 'excluded_families.private.json'))
        if (result.get('primary_blind_plan') or {}).get('ready_for_independent_main_metrics'):
            args.out.mkdir(parents=True, exist_ok=True)
            write_staged_blind_csv(args.out, result, snapshot)
    private_json(args.out / 'result.private.json', result)
    # Public console summary contains no profile, corpus membership, scores or labels.
    print(json.dumps({'status': result['status'], 'paid_calls': 0,
                      'blockers': result.get('blockers', []), 'output_saved_locally': True}))


if __name__ == '__main__':
    main()
