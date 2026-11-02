"""Synthetic checks only; never read a real profile, library or human labels."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np
from omegaconf import OmegaConf
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

import actions_runner as a
import offline_eval as e
from seal import seal, canonical


class Guards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        cls.public = cls.private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def payload(self):
        return {'protocol': {'paid_calls_authorized': 0, 'source_commit': a.PIN},
                'ranking_config': {}, 'exclusions': [], 'freeze_receipt': {}, 'runtime_overrides': {}}

    def decrypt(self, envelope):
        header = {k: v for k, v in envelope.items() if k not in ('wrapped_key', 'nonce', 'ciphertext')}
        aad = canonical(header)
        key = self.private_key.decrypt(base64.b64decode(envelope['wrapped_key']),
                                       padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=aad))
        return json.loads(AESGCM(key).decrypt(base64.b64decode(envelope['nonce']), base64.b64decode(envelope['ciphertext']), aad))

    def test_existing_encryption_format_roundtrip(self):
        value = {'synthetic_fixture_only': True, 'scores': {'x': 12.3}}
        envelope = seal(value, self.public, 'fixture', '0' * 64)
        self.assertEqual(self.decrypt(envelope), value)
        self.assertNotIn('scores', envelope)

    def test_ciphertext_and_context_tamper_fail_closed(self):
        envelope = seal({'test': 1}, self.public, 'fixture', '0' * 64)
        data = bytearray(base64.b64decode(envelope['ciphertext']))
        data[0] ^= 1
        envelope['ciphertext'] = base64.b64encode(data).decode()
        with self.assertRaises(InvalidTag):
            self.decrypt(envelope)

    def test_corpus_and_vectors_never_export(self):
        for field in ('corpus', 'vectors', 'config', 'history', 'paths'):
            with self.assertRaisesRegex(ValueError, 'private_library_material'):
                a.check_export({'result': [{field: ['synthetic']} ]})
        a.check_export({'corpus_sha256': 'not actual library text', 'public_candidates': []})

    def test_paid_or_remote_embedding_input_rejected(self):
        p = self.payload()
        p['protocol']['paid_calls_authorized'] = 1
        with self.assertRaisesRegex(ValueError, 'paid_calls'):
            a.validate_payload(p)
        p = self.payload()
        p['runtime_overrides']['reranker'] = {'api': {}}
        with self.assertRaisesRegex(ValueError, 'remote_embeddings'):
            a.validate_payload(p)

    def test_private_payload_cannot_contain_api_credentials_or_delivery_overrides(self):
        p = self.payload()
        p['runtime_overrides']['zotero'] = {'api_key': 'synthetic-marker'}
        with self.assertRaisesRegex(ValueError, 'credentials_must_use_existing'):
            a.validate_payload(p)
        p = self.payload()
        p['runtime_overrides']['email'] = {'enabled': True}
        with self.assertRaisesRegex(ValueError, 'unsupported_override'):
            a.validate_payload(p)

    def test_workflow_only_dispatch_readonly_and_ciphertext_artifact(self):
        import yaml
        path = Path(__file__).resolve().parents[2] / '.github/workflows/test.yml'
        config = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(config['on']), {'workflow_dispatch'})
        self.assertEqual(config['permissions'], {'contents': 'read'})
        self.assertEqual(set(config['jobs']), {'isolated-jina'})
        job = config['jobs']['isolated-jina']
        self.assertEqual(job['timeout-minutes'], '45')
        self.assertIn('experiment/jina-input-audit-20261101', job['if'])
        exports = [s['with'] for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4']
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]['path'], 'sealed/result.sealed.json')
        self.assertEqual(exports[0]['retention-days'], '1')
        credential_env = [s['env'] for s in job['steps'] if 'env' in s][0]
        self.assertFalse(set(credential_env) & a.DENIED_ENV)
        self.assertNotIn('CUSTOM_CONFIG', credential_env)

    def test_no_production_delivery_or_save_entrypoints(self):
        text = (Path(__file__).parent / 'actions_runner.py').read_text()
        for expression in ('ex.run(', 'ex._recommend(', 'state.save(', 'state.add(', 'state.mark(', 'send_email('):
            self.assertNotIn(expression, text)
        self.assertIn('shutil.rmtree(private', text)

    def test_missing_payload_seals_failure_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'out/result.sealed.json'
            public_file = Path(directory) / 'recipient.public.pem'
            public_file.write_bytes(self.public)
            env = {'RUNNER_TEMP': directory, 'SEALED_OUTPUT': str(out), 'JINA_OFFLINE_INPUT': '{}'}
            with patch.dict(os.environ, env), patch.object(a, 'PUBLIC', Path(directory)):
                a.main()
            result = self.decrypt(json.loads(out.read_text()))
            self.assertEqual(result['paid_calls'], 0)
            self.assertEqual(result['error_code'], 'private_payload_keys')
            self.assertFalse(list(Path(directory).glob('jina-offline-*')))

    def test_continuous_scoring_is_same_profile_method_for_A_and_B(self):
        from zot2dailypaper.reranker.base import BaseReranker
        self.assertIs(e.TitleAbstractProfile.rerank.__code__, BaseReranker.rerank.__code__)
        self.assertIs(e.TitleAbstractProfile.rerank.__globals__['strategy_settings'],
                      BaseReranker.rerank.__globals__['strategy_settings'])

    def test_missing_labels_not_zero(self):
        result = e.judged_metrics(['x', 'y'], {'x': 3}, 2)
        self.assertEqual(result['status'], 'incomplete_topk_judgments')
        self.assertNotIn('ndcg', result)

    def test_family_version_alias_and_title_uncertainty(self):
        prior = [{'id': 'prior', 'title': 'synthetic prior record', 'identifiers': ['arxiv:2610.12345v1']}]
        rows = [{'id': 'new', 'title': 'synthetic version', 'url': 'https://arxiv.org/abs/2610.12345v2'},
                {'id': 'uncertain', 'title': 'synthetic prior record', 'doi': '10.9999/unlinked'}]
        ready, excluded, uncertain = e.partition_families(rows, prior)
        self.assertEqual(ready, [])
        self.assertEqual(excluded[0]['reason'], 'previously_exposed_family')
        self.assertEqual(uncertain[0]['id'], 'uncertain')

    def production_source_config(self):
        # Public production profile, including its actual categories priority.
        return OmegaConf.merge(OmegaConf.load(e.ROOT / 'config/base.yaml'),
                               OmegaConf.load(e.ROOT / 'config/interests.yaml'))

    def test_actual_interest_priority_and_expanded_effective_scope(self):
        from zot2dailypaper.preprint_interests import categories_for
        config = self.production_source_config()
        baseline = list(categories_for(config, 'arxiv'))
        self.assertEqual(len(baseline), 6)
        before = OmegaConf.to_container(config, resolve=False)
        desired = ['cs.LG', 'math.NA', 'physics.data-an']
        ineffective = copy.deepcopy(config)
        ineffective.source.arxiv.category = desired
        self.assertEqual(list(categories_for(ineffective, 'arxiv')), baseline)
        expanded, audit = a.expanded_arxiv_config(config, desired)
        self.assertEqual(list(categories_for(expanded, 'arxiv')), desired)
        self.assertEqual(audit['new_categories'], ['math.NA', 'physics.data-an'])
        self.assertTrue(audit['scope_verified_before_fetch'])
        self.assertEqual(OmegaConf.to_container(config, resolve=False), before)
        with self.assertRaisesRegex(ValueError, 'scope_not_distinct'):
            a.expanded_arxiv_config(config, baseline)

    def test_real_arxiv_retriever_query_reaches_new_categories_without_network(self):
        from zot2dailypaper.retriever.arxiv_retriever import ArxivRetriever
        config, audit = a.expanded_arxiv_config(self.production_source_config(),
                                               ['cs.LG', 'math.NA', 'physics.data-an'])
        config.source.arxiv.window_days = 7
        config.executor.debug = False
        retriever = ArxivRetriever(config)
        captured = []
        def results(client, search):
            captured.append(search.query)
            return [SimpleNamespace(primary_category=c) for c in ['math.NA', 'physics.data-an', 'cs.LG', 'cs.AI']]
        with patch('zot2dailypaper.retriever.arxiv_retriever.api_client'), patch.object(retriever, '_api_results', side_effect=results):
            rows = retriever._retrieve_raw_papers()
        self.assertEqual({r.primary_category for r in rows}, {'math.NA', 'physics.data-an', 'cs.LG'})
        self.assertIn('cat:math.NA', captured[0])
        self.assertIn('cat:physics.data-an', captured[0])
        self.assertNotIn('cat:physics.chem-ph', captured[0])

    def test_blind_csv_keeps_original_abstract_source_and_missing_flags(self):
        snapshot = {'candidates': [
            {'id': 'paper-a', 'title': 'Synthetic A', 'source': 'journals', 'journal': 'Synthetic Journal',
             'url': 'https://example.test/a', 'abstract': 'Original, synthetic text.\nSecond line.',
             'abstract_complete': True, 'abstract_provenance': {'source': 'fixture-metadata', 'url': 'https://example.test/a/source'}},
            {'id': 'paper-b', 'title': 'Synthetic B', 'source': 'arxiv', 'url': 'https://example.test/b',
             'abstract': '', 'abstract_complete': False},
            {'id': 'paper-c', 'title': 'Synthetic C', 'source': 'arxiv', 'url': 'https://example.test/c',
             'abstract': 'Truncated synthetic...', 'abstract_complete': False},
        ]}
        result = {'blind_id_to_candidate_id': {'N001': 'paper-a', 'N002': 'paper-b', 'N003': 'paper-c'}}
        import csv
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'blind.csv'
            e.write_blind_csv(path, result, snapshot)
            with path.open() as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]['original_abstract'], snapshot['candidates'][0]['abstract'])
        self.assertEqual(rows[0]['abstract_source_url'], 'https://example.test/a/source')
        self.assertEqual(rows[0]['journal_or_venue'], 'Synthetic Journal')
        self.assertEqual([r['abstract_status'] for r in rows], ['available', 'missing', 'incomplete'])
        self.assertFalse(set(rows[0]) & {'score', 'rank', 'algorithm', 'sampling_path', 'recommendation_reason'})
        self.assertTrue(all(r['relevance_0_to_3'] == '' for r in rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
