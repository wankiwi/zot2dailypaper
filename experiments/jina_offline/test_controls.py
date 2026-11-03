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
        return {'protocol': {'paid_calls_authorized': 0, 'source_commit': a.PIN, 'schema': 'jina_fullpool_protocol_v1', 'date_policy': {}},
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
        self.assertEqual(set(config['jobs']), {'controls', 'isolated-jina'})
        job = config['jobs']['isolated-jina']
        self.assertEqual(job['timeout-minutes'], '45')
        self.assertIn('experiment/jina-timeout-diagnostics-20261103', job['if'])
        exports = [s['with'] for s in job['steps'] if s.get('uses') == 'actions/upload-artifact@v4']
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]['path'], 'sealed/result.sealed.json')
        self.assertEqual(exports[0]['retention-days'], '1')
        credential_env = [s['env'] for s in job['steps'] if 'env' in s][0]
        self.assertFalse(set(credential_env) & a.DENIED_ENV)
        self.assertNotIn('CUSTOM_CONFIG', credential_env)
        self.assertIn('JINA_FULLPOOL_INPUT', credential_env)
        self.assertNotIn('JINA_OFFLINE_INPUT', path.read_text())
        self.assertEqual(config['on']['workflow_dispatch']['inputs']['mode']['default'], 'tests')
        self.assertFalse(any('env' in step and set(step['env']) & {'ZOTERO_KEY','JINA_FULLPOOL_INPUT'} for step in config['jobs']['controls']['steps']))

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
            env = {'RUNNER_TEMP': directory, 'SEALED_OUTPUT': str(out), 'JINA_FULLPOOL_INPUT': '{}'}
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

    def test_export_guard_failure_still_cleans_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'out/result.sealed.json'
            env={'RUNNER_TEMP':directory,'SEALED_OUTPUT':str(out),'JINA_FULLPOOL_INPUT':'{}'}
            with patch.dict(os.environ,env),patch.object(a,'check_export',side_effect=ValueError('private_library_material_in_export')):
                with self.assertRaisesRegex(ValueError,'private_library_material'):
                    a.main()
            self.assertFalse(list(Path(directory).glob('jina-offline-*')))
            self.assertFalse(out.exists())

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


class SourceDiagnostics(unittest.TestCase):
    url = 'https://export.arxiv.org/api/query?search_query=private+synthetic+term&start=0&max_results=100&api_key=synthetic-secret'

    def response(self, status, content=b'', headers=None):
        import requests
        r = requests.Response()
        r.status_code, r._content, r._content_consumed = status, content, True
        r.headers.update(headers or {})
        r.url = self.url
        return r

    def run_transport(self, statuses):
        import http_diagnostics as d
        from zot2dailypaper.retriever import arxiv_retriever as m
        audit = d.Audit('arxiv','common')
        values = [self.response(status) for status in statuses]
        with patch('requests.Session.send', side_effect=values) as sender, patch.object(d,'sleep'), patch.object(m,'sleep'), d.audited_arxiv(audit):
            with m.ArxivSession() as session:
                result = session.get(self.url)
        return result, audit, sender.call_count

    def test_arxiv_403_no_retry_and_no_secret_or_query_text(self):
        r, audit, count = self.run_transport([403])
        self.assertEqual((r.status_code,count),(403,1))
        self.assertEqual(audit.events[0]['transport_attempt'],1)
        self.assertEqual(audit.events[0]['http_status'],403)
        encoded=json.dumps(audit.finish(error=RuntimeError('synthetic-secret response body')))
        for text in ('synthetic-secret','private+synthetic','response body','api_key'):
            self.assertNotIn(text,encoded)
        self.assertEqual(audit.events[0]['domain'],'export.arxiv.org')
        self.assertEqual(audit.events[0]['path'],'/api/query')

    def test_arxiv_429_no_blind_retry(self):
        r, audit, count = self.run_transport([429])
        self.assertEqual(count,1)
        self.assertEqual(r.status_code,429)

    def test_arxiv_5xx_original_four_attempt_bound(self):
        r, audit, count = self.run_transport([503]*4)
        self.assertEqual(count,4)
        self.assertEqual([x['transport_attempt'] for x in audit.events],[1,2,3,4])
        self.assertEqual(r.status_code,503)

    def empty_feed(self):
        return b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>0</o:totalResults><o:startIndex>0</o:startIndex><o:itemsPerPage>100</o:itemsPerPage></feed>'

    def test_arxiv_transient_then_valid_empty(self):
        import http_diagnostics as d
        from zot2dailypaper.retriever import arxiv_retriever as m
        audit=d.Audit('arxiv','common')
        with patch('requests.Session.send',side_effect=[self.response(503),self.response(200,self.empty_feed())]) as sender, patch.object(d,'sleep'),patch.object(m,'sleep'),d.audited_arxiv(audit):
            with m.ArxivSession() as client:
                r=client.get(self.url)
        self.assertEqual(sender.call_count,2)
        self.assertEqual(audit.events[-1]['phase'],'page_validation')
        self.assertEqual(audit.events[-1]['expected_total'],0)
        self.assertEqual(audit.events[-1]['page_entries'],0)

    def test_invalid_http200_stops_with_validation_error(self):
        import http_diagnostics as d
        from zot2dailypaper.retriever import arxiv_retriever as m
        audit=d.Audit('arxiv','common')
        with patch('requests.Session.send',return_value=self.response(200,b'<html>private body</html>')),patch.object(d,'sleep'),d.audited_arxiv(audit):
            with m.ArxivSession() as client, self.assertRaises(m.ArxivFeedError):
                client.get(self.url)
        self.assertEqual(audit.events[-1]['status'],'failed')
        self.assertNotIn('private body',json.dumps(audit.finish(error=ValueError('private body'))))

    def test_source_empty_distinct_from_failed(self):
        import http_diagnostics as d
        empty=SimpleNamespace(retrieve_papers=lambda: [])
        self.assertEqual(d.collect(empty,'arxiv','common')[1]['status'],'empty')
        import arxiv
        def fail(): raise arxiv.HTTPError(self.url,0,403)
        _, record=d.collect(SimpleNamespace(retrieve_papers=fail),'arxiv','common')
        self.assertEqual(record['status'],'failed')
        self.assertIsNone(record['count'])
        self.assertEqual(record['error']['http_status'],403)
        self.assertFalse(record['coverage_complete'])

    def test_completed_first_page_then_failure_keeps_page_offset(self):
        import http_diagnostics as d
        from zot2dailypaper.retriever import arxiv_retriever as m
        import arxiv
        entry='''<entry><id>https://arxiv.org/abs/2611.00001v1</id><title>Synthetic record</title><summary>Synthetic evidence</summary><published>2026-11-01T00:00:00Z</published><updated>2026-11-01T00:00:00Z</updated><author><name>Synthetic Author</name></author><a:primary_category term="cs.LG"/><category term="cs.LG"/><link href="https://arxiv.org/abs/2611.00001v1"/></entry>'''
        feed=('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/" xmlns:a="http://arxiv.org/schemas/atom"><o:totalResults>2</o:totalResults><o:startIndex>0</o:startIndex><o:itemsPerPage>1</o:itemsPerPage>'+entry+'</feed>').encode()
        def retrieve():
            with m.ArxivSession() as client:
                client.get(self.url)
                url=self.url.replace('start=0','start=100')
                response=client.get(url)
                raise arxiv.HTTPError(url,0,response.status_code)
        with patch('requests.Session.send',side_effect=[self.response(200,feed),self.response(403)]),patch.object(d,'sleep'):
            _,record=d.collect(SimpleNamespace(retrieve_papers=retrieve),'arxiv','common')
        self.assertEqual(record['status'],'failed')
        self.assertEqual(record['error']['page_start'],100)
        self.assertEqual(record['events'][1]['page_entries'],1)
        self.assertEqual(record['events'][-1]['page_start'],100)
        self.assertIsNone(record['count'])

    def test_redirect_is_observed_and_never_followed(self):
        import http_diagnostics as d
        from zot2dailypaper.retriever import arxiv_retriever as m
        audit=d.Audit('arxiv','common')
        with patch('requests.Session.send',return_value=self.response(302,headers={'Location':'https://blocked.example/private'})) as sender,patch.object(d,'sleep'),d.audited_arxiv(audit):
            with m.ArxivSession() as client:
                self.assertEqual(client.get(self.url).status_code,302)
        self.assertEqual(sender.call_count,1)
        self.assertEqual(audit.events[0]['redirect_domain'],'blocked.example')
        self.assertNotIn('/private',json.dumps(audit.events))

    def test_retry_after_bounded_and_unknown_fields_private(self):
        import http_diagnostics as d
        for bad in ('inf','nan','86401','private body',None): self.assertIsNone(d.safe_retry_after(bad))
        self.assertEqual(d.safe_retry_after('30'),30)
        shape=d.request_shape('https://user:synthetic-secret@api.crossref.org/works?query=private-word&api_key=secret&offset=20')
        self.assertEqual(shape['page_offset'],20)
        self.assertEqual(shape['path'],'/works')
        self.assertNotIn('secret',json.dumps(shape))
        self.assertNotIn('private-word',json.dumps(shape))


class FrozenDates(unittest.TestCase):
    def test_execution_date_can_advance_without_protocol_edit(self):
        from datetime import datetime,timezone
        from snapshot_policy import freeze_cutoff
        policy={'mode':'completed_utc_day_end','minimum_asof_exclusive':'2026-11-02T00:00:00Z'}
        a=freeze_cutoff(policy,datetime(2026,11,3,2,tzinfo=timezone.utc))
        b=freeze_cutoff(policy,datetime(2026,11,4,23,tzinfo=timezone.utc))
        self.assertTrue(a['asof_exclusive'].startswith('2026-11-03T00'))
        self.assertTrue(b['asof_exclusive'].startswith('2026-11-04T00'))
        with self.assertRaisesRegex(ValueError,'no_new_completed'): freeze_cutoff(policy,datetime(2026,11,2,23,tzinfo=timezone.utc))

    def test_freezing_retriever_dates_does_not_freeze_http_retry_clock(self):
        from datetime import datetime,timezone
        from snapshot_policy import frozen_source_dates
        from zot2dailypaper.retriever import arxiv_retriever as ar,biorxiv_retriever as bio
        before_ar,before_bio=ar.datetime,bio.datetime
        asof=datetime(2026,11,3,tzinfo=timezone.utc)
        with frozen_source_dates(asof):
            self.assertIs(ar.datetime,before_ar)
            self.assertEqual(bio.datetime.now(timezone.utc),asof)
        self.assertIs(bio.datetime,before_bio)

    def test_frozen_arxiv_query_same_categories_and_seven_day_scope(self):
        from datetime import datetime,timezone
        from snapshot_policy import bind_arxiv_cutoff
        from zot2dailypaper.retriever.arxiv_retriever import ArxivRetriever
        cfg=Guards().production_source_config()
        cfg.source.arxiv.window_days=7
        cfg.executor.debug=False
        retriever=ArxivRetriever(cfg)
        captured=[]
        def results(client, search): captured.append(search.query); return []
        bind_arxiv_cutoff(retriever,datetime(2026,11,3,tzinfo=timezone.utc))
        with patch('zot2dailypaper.retriever.arxiv_retriever.api_client'),patch.object(retriever,'_api_results',side_effect=results):
            retriever._retrieve_raw_papers()
        self.assertIn('submittedDate:[202610270000 TO 202611030000]',captured[0])
        for category in retriever.categories: self.assertIn('cat:'+category,captured[0])

    def test_updated_preprint_original_date_is_not_guessed(self):
        from snapshot_policy import capture_version_metadata,public_row
        from zot2dailypaper.protocol import Paper
        from datetime import datetime,timezone
        paper=Paper(source='biorxiv',title='Synthetic revision',authors=[],abstract='Evidence',url='https://example.test/pv2',published=datetime(2026,11,1,tzinfo=timezone.utc))
        retriever=SimpleNamespace(convert_to_paper=lambda raw:paper)
        capture_version_metadata(retriever)
        row=public_row(retriever.convert_to_paper({'version':'2'}),'common')
        self.assertEqual(row['original_date_status'],'unknown_revised_record')
        self.assertEqual(row['first_public_date'],'')
        self.assertEqual(row['source_version'],2)

    def test_source_window_preserved_without_single_day_crop(self):
        from snapshot_policy import date_eligibility
        from datetime import datetime,timezone
        asof=datetime(2026,11,3,tzinfo=timezone.utc)
        row={'source':'journals','first_public_date':'2026-10-29','original_date_status':'source_original_published'}
        self.assertTrue(date_eligibility(row,asof,{'journals':7})[0])
        row['record_updated_at']='2026-11-04T00:00:00+00:00'
        self.assertEqual(date_eligibility(row,asof,{'journals':7})[1],'record_updated_after_cutoff')


class FullPoolControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from datetime import datetime,timezone
        cls.cfg=OmegaConf.merge(OmegaConf.load(e.ROOT/'config/base.yaml'),{'interest_profile':{'keywords':['synthetic axis red','synthetic axis blue'],'keyword_weight':.4,'zotero_weight':.6},'executor':{'quotas':{'journals':25,'preprints':20,'random':5},'min_score':-10}})
        cls.protocol={'seed':20261102,'blind_pool':{'head_k':20,'stratified_random_n':6},'C':{'channels':[{'id':'methods','task_description':'Synthetic transferable numerical method'},{'id':'phenomena','task_description':'Synthetic measured microscopic observation'}],'weights':{'B':2.,'methods':1.,'phenomena':1.},'rrf_k':60,'lane_depth':40}}
        cls.rows=[{'id':f'x{i:03}','title':'Synthetic '+hashlib.sha256(str(i).encode()).hexdigest()[:32],'abstract':f'Synthetic full evidence {i:03}','source':'journals' if i%3 else 'arxiv','url':f'https://example.test/{i}','doi':f'10.9999/fixture{i}','pool':'common','first_public_date':'2026-11-02','original_date_status':'source_original_published','abstract_complete':True,'abstract_provenance':{'source':'fixture'},'abstract_before_recovery':f'Synthetic full evidence {i:03}','retrieved_at':'2026-11-03T00:00:00Z'} for i in range(120)]
        cls.corpus=[{'id':f'R{i:03}','title':f'Synthetic reference {i}','abstract':f'Synthetic reference evidence {i}','added_date':f'2026-10-{i+1:02}T00:00:00Z','paths':['Synthetic'],'doi':f'10.9999/reference{i}'} for i in range(12)]
        texts=[e.paper_text(e.as_paper(r),mode) for r in cls.rows for mode in ('abstract','title_abstract')]
        texts += [e.paper_text(e.as_corpus(r),mode) for r in cls.corpus for mode in ('abstract','title_abstract')]
        texts += list(cls.cfg.interest_profile.keywords)+[v['task_description'] for v in cls.protocol['C']['channels']]
        rng=np.random.default_rng(43)
        vectors={hashlib.sha256(t.encode()).hexdigest():rng.uniform(.05,1.,768).astype(np.float32).tolist() for t in dict.fromkeys(texts)}
        cls.identity={'model':e.MODEL,'revision':e.REVISION,'dimension':768,'backend':'synthetic','dtype':'float32','device':'cpu','threads':1,'encode_kwargs':{},'prompts':{},'max_seq_length':8192,'libraries':{'fixture':'1'}}
        cls.pack={'model_identity':cls.identity,'vectors':vectors,'vectors_sha256':e.digest(vectors)}
        cls.emb=e.Embeddings(cls.pack,cls.identity)
        cls.pool=e.score_full_pool(cls.rows,cls.corpus,cls.cfg,cls.emb,cls.protocol)

    def test_actual_local_reranker_all_120_scores_directions_and_order(self):
        from zot2dailypaper.reranker.local import LocalReranker
        ranker=LocalReranker(self.cfg)
        objects=[e.as_paper(x) for x in self.rows]
        with patch.object(ranker,'_encode_texts',side_effect=self.emb.encode):
            ranked=ranker.rerank(objects,[e.as_corpus(x) for x in self.corpus])
        gold={'order':[p._offline_id for p in ranked],'rows':{p._offline_id:{key:getattr(p,key) for key in e.SCORE_FIELDS} for p in ranked}}
        self.assertEqual(e.replay_check(self.pool['arms']['A'],gold)['status'],'verified')
        self.assertEqual(len(gold['order']),120)

    def test_three_arms_persist_every_candidate_with_stable_ranks_and_views(self):
        ids={r['id'] for r in self.rows}
        for arm in self.pool['arms'].values():
            self.assertEqual(set(arm['model_order']),ids)
            self.assertEqual(set(arm['unrounded_ranking_scores']),ids)
            self.assertEqual(set(arm['rows']),ids)
            self.assertEqual(set(arm['selection_disposition']),ids)
            self.assertEqual(len(arm['stable_model_rank']),120)
            self.assertEqual(len(arm['selection']['main_order']),45)
            self.assertEqual(len(arm['selection']['random']),5)
        self.assertIn('reference_assignment_audit',self.pool)
        self.assertEqual(sum(self.pool['reference_assignment_audit']['direction_transitions'].values()),12)

    def test_quota_main_heads_union_random_and_strata_no_silent_truncation(self):
        eligibility={r['id']:'eligible' for r in self.rows}
        plan=e.plan_actual_heads(self.pool,self.rows,eligibility,self.protocol,'common')
        sampled=set(plan['blind_id_to_candidate_id'].values())
        for arm in self.pool['arms'].values():
            self.assertTrue(set(arm['model_order'][:20])<=sampled)
            self.assertTrue(set(arm['selection']['main_order'][:20])<=sampled)
            self.assertTrue(set(arm['selection']['random'])<=sampled)
        self.assertLessEqual(len(sampled),81)
        self.assertEqual(plan['theoretical_maximum'],141)
        self.assertEqual(plan['when_model_and_main_heads_equal_maximum'],81)
        self.assertTrue(plan['ready_for_independent_main_metrics'])
        self.assertFalse(plan['silent_truncation'])
        self.assertEqual(plan,e.plan_actual_heads(self.pool,self.rows,eligibility,self.protocol,'common'))

    def test_exposed_actual_head_not_replaced_by_lower_rank(self):
        eligibility={r['id']:'eligible' for r in self.rows}
        blocked=self.pool['arms']['A']['selection']['main_order'][0]
        eligibility[blocked]='previously_exposed_family'
        plan=e.plan_actual_heads(self.pool,self.rows,eligibility,self.protocol,'common')
        self.assertFalse(plan['ready_for_independent_main_metrics'])
        self.assertNotIn(blocked,plan['blind_id_to_candidate_id'].values())
        self.assertIn({'id':blocked,'reason':'previously_exposed_family'},plan['blocked_prior_or_unknown_heads'])
        self.assertIn(blocked,plan['required_main_ids'])

    def test_full_pool_evaluate_reproduction_and_expanded_same_pool_controls(self):
        original={**self.rows[0],'id':'expanded-a','doi':'10.9999/extra','title':'An unrelated numerical primitive','pool':'expanded','url':'https://example.test/expanded-a'}
        pack=copy.deepcopy(self.pack)
        text=e.paper_text(e.as_paper(original),'title_abstract')
        pack['vectors'][hashlib.sha256(text.encode()).hexdigest()]=np.random.default_rng(98).uniform(.05,1.,768).astype(np.float32).tolist()
        pack['vectors_sha256']=e.digest(pack['vectors'])
        protocol={**self.protocol,'excluded_studies_sha256':e.digest([])}
        gold=copy.deepcopy(self.pool['arms']['A'])
        snapshot={'source_commit':e.PIN,'config':OmegaConf.to_container(self.cfg),'model_identity':self.identity,'corpus':self.corpus,'candidates':self.rows+[original],'candidate_events':[],'history':{'sha256':'synthetic'},'query_cutoff':'2026-11-03T00:00:00Z','source_days':{'arxiv':7,'journals':7},'source_attempts':[{'source':'arxiv','scope':'common','status':'success'},{'source':'arxiv','scope':'expanded','status':'success'}],'expected_A':gold,'synthetic_fixture_only':True}
        with tempfile.TemporaryDirectory() as directory,patch.object(e,'HERE',Path(directory)),patch.object(e,'verify_source',return_value={'fixture':'no claim of production code verification'}):
            e.private_json(Path(directory)/'freeze_receipt.private.json',{'protocol_sha256':e.digest(protocol),'frozen_C_sha256':e.digest(protocol['C'])})
            e.private_json(Path(directory)/'runtime_config.frozen.private.json',OmegaConf.to_container(self.cfg))
            result=e.evaluate(snapshot,pack,protocol,[])
        self.assertEqual(result['reproduction']['status'],'verified')
        self.assertEqual(result['production_selection_replay']['status'],'verified')
        self.assertEqual(len(result['production_full_pool_audit']['candidate_ids']),120)
        self.assertEqual(len(result['validation_common_full_pool']['candidate_ids']),120)
        expanded=result['validation_expanded_full_pool_controls']
        self.assertEqual(len(expanded['candidate_ids']),121)
        self.assertTrue(all(len(arm['order'])==121 for arm in expanded['arms'].values()))
        self.assertFalse(result['new_date_subset_cropped_before_scoring'])
        a.check_export(result)

    def test_unknown_tail_does_not_become_zero_or_exact_full_ndcg(self):
        result=e.judged_metrics(['a','b','c'],{'a':2,'b':1},2)
        self.assertEqual(result['precision'],.5)
        self.assertIsNone(result['complete_pool_ndcg'])
        self.assertEqual(result['unjudged_in_complete_pool'],1)
        self.assertIn('pooled_ndcg',result)
        self.assertLessEqual(*result['complete_pool_ndcg_bounds'])
        self.assertNotIn('ndcg',result)
        incomplete=e.judged_metrics(['a','b'],{'a':3},2)
        self.assertEqual(incomplete['status'],'incomplete_topk_judgments')
        self.assertNotIn('precision',incomplete)

    def test_complete_judgments_allow_exact_ndcg(self):
        result=e.judged_metrics(['a','b'],{'a':2,'b':1},2)
        self.assertEqual(result['complete_pool_ndcg'],1.)
        self.assertEqual(result['complete_pool_ndcg_bounds'],[1.,1.])


class CompletenessAndPrivacy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FullPoolControls.setUpClass()

    def test_partial_non_arxiv_source_is_not_empty_success(self):
        import http_diagnostics as d
        retriever=SimpleNamespace(failures=[])
        def retrieve(): retriever.failures.append('synthetic partial journal'); return []
        retriever.retrieve_papers=retrieve
        _,record=d.collect(retriever,'journals','common')
        self.assertEqual(record['status'],'partial')
        self.assertFalse(record['coverage_complete'])
        self.assertEqual(record['reported_subsource_failures'],1)

    def test_conversion_error_swallowed_by_base_retriever_still_marks_partial(self):
        import http_diagnostics as d
        from snapshot_policy import capture_version_metadata
        def convert(raw): raise ValueError('private raw record')
        retriever=SimpleNamespace(convert_to_paper=convert)
        capture_version_metadata(retriever)
        def retrieve():
            try: retriever.convert_to_paper({})
            except ValueError: pass
            return []
        retriever.retrieve_papers=retrieve
        _,record=d.collect(retriever,'journals','common')
        self.assertEqual(record['status'],'partial')
        self.assertEqual(record['reported_conversion_failures'],1)
        self.assertNotIn('private raw record',json.dumps(record))

    def test_common_failure_stops_before_encoding_or_expanded_retry(self):
        text=(Path(__file__).parent/'actions_runner.py').read_text()
        start=text.index("if any(x['status'] in ('failed', 'partial')")
        end=text.index('    candidates = []',start)
        guard=text[start:end]
        self.assertIn('source_preflight_failed_not_scored',guard)
        self.assertIn('return {',guard)
        self.assertLess(end,text.index('ex.reranker.rerank'))
        self.assertLess(end,text.index("collect(extra, 'arxiv', 'expanded')"))

    def test_missing_source_snapshot_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(e,'HERE',Path(directory)):
            protocol={'C':{'fixture':'unchanged'}}
            e.private_json(Path(directory)/'freeze_receipt.private.json',{'protocol_sha256':e.digest(protocol),'frozen_C_sha256':e.digest(protocol['C'])})
            result=e.preflight({'source_commit':e.PIN},None,protocol)
        self.assertIn('immutable_source_date_snapshot_missing',result['blockers'])
        self.assertIn('actual_filtered_zotero_corpus_missing',result['blockers'])

    def test_no_channel_retuning_after_freeze(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(e,'HERE',Path(directory)):
            protocol={'C':{'fixture':'modified'}}
            e.private_json(Path(directory)/'freeze_receipt.private.json',{'protocol_sha256':e.digest(protocol),'frozen_C_sha256':e.digest({'fixture':'frozen'})})
            result=e.preflight({'source_commit':e.PIN},None,protocol)
        self.assertIn('frozen_channels_or_fusion_changed',result['blockers'])

    def test_main_and_model_heads_both_judged_when_pending_consumes_quota(self):
        fixture=FullPoolControls
        pending=[dict(fixture.rows[i],recommendation_group='journals') for i in range(25)]
        pool=e.score_full_pool(fixture.rows,fixture.corpus,fixture.cfg,fixture.emb,fixture.protocol,pending)
        self.assertNotEqual(pool['arms']['A']['model_order'][:20],pool['arms']['A']['selection']['main_order'][:20])
        eligibility={r['id']:'eligible' for r in fixture.rows}
        plan=e.plan_actual_heads(pool,fixture.rows,eligibility,fixture.protocol,'common')
        sampled=set(plan['blind_id_to_candidate_id'].values())
        for arm in pool['arms'].values():
            self.assertTrue(set(arm['model_order'][:20])<=sampled)
            self.assertTrue(set(arm['selection']['main_order'][:20])<=sampled)
        self.assertLessEqual(plan['blind_count'],141)
        self.assertTrue(all(not x for x in plan['model_heads_not_in_blind_pool'].values()))

    def test_missing_actual_head_abstract_is_information_gap_not_negative(self):
        fixture=FullPoolControls
        rows=copy.deepcopy(fixture.rows)
        key=fixture.pool['arms']['A']['model_order'][0]
        next(r for r in rows if r['id']==key)['abstract']=''
        plan=e.plan_actual_heads(fixture.pool,rows,{r['id']:'eligible' for r in rows},fixture.protocol,'common')
        self.assertIn(key,plan['head_information_gaps'])
        self.assertFalse(plan['ready_for_independent_main_metrics'])
        self.assertIn(key,plan['blind_id_to_candidate_id'].values())

    def test_all_reference_identifiers_stay_out_of_export(self):
        fixture=FullPoolControls
        audit=fixture.pool['reference_assignment_audit']
        text=json.dumps(audit)
        for reference in fixture.corpus:
            self.assertNotIn(reference['title'],text)
            self.assertNotIn(reference['doi'],text)
        self.assertNotIn('paths',audit)
        a.check_export(fixture.pool)


class ExclusionAndSourcePins(unittest.TestCase):
    def test_all_prior_families_and_publication_aliases_are_checked(self):
        prior=[{'id':f'old{i:02}','title':f'Synthetic old family {i}', 'identifiers':[f'arxiv:2610.{10000+i}v1',f'10.9999/family{i}']} for i in range(76)]
        rows=[{'id':'new-version','title':'A linked updated preprint','url':'https://arxiv.org/abs/2610.10075v4'},
              {'id':'published-version','title':'An exact linked published report','doi':'10.9999/family0'}]
        accepted,excluded,uncertain=e.partition_families(rows,prior)
        self.assertEqual(accepted,[])
        self.assertEqual(len(excluded),2)
        self.assertTrue(all(r['reason']=='previously_exposed_family' for r in excluded))
        self.assertEqual(uncertain,[])

    def test_local_scoring_core_blob_matches_the_frozen_public_pin(self):
        pin=json.loads((Path(__file__).parent/'source_pin.json').read_text())
        for row in pin['tree']:
            # protocol.py's separate paid-summary method can differ in this local
            # checkout. It is not executed by these synthetic ranking checks;
            # a real capture verifies ALL pinned files, including protocol.py.
            if row['path'].endswith('/protocol.py'): continue
            data=(e.ROOT/row['path']).read_bytes()
            actual=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            self.assertEqual(actual,row['sha'],row['path'])


class ObservedSourceFailures(unittest.TestCase):
    def response(self,status):
        import requests
        r=requests.Response();r.status_code=status;r._content=b'';r._content_consumed=True;r.url='https://api.example.test/list'
        return r

    def collect(self,urls,statuses,source='other'):
        import requests,http_diagnostics as d
        def retrieve():
            for url in urls:
                r=requests.get(url)
                try:r.raise_for_status()
                except requests.HTTPError:pass
            return []
        retriever=SimpleNamespace(retrieve_papers=retrieve)
        with patch('requests.adapters.HTTPAdapter.send',side_effect=[self.response(s) for s in statuses]) as sender:
            papers,record=d.collect(retriever,source,'common')
        return papers,record,sender.call_count

    def test_swallowed_403_is_failed_unknown_not_valid_zero(self):
        _,record,calls=self.collect(['https://api.example.test/list?page=0'],[403])
        self.assertEqual(calls,1)
        self.assertEqual(record['status'],'failed')
        self.assertIsNone(record['count'])
        self.assertEqual(record['unrecovered_observed_failures'][0]['http_status'],403)

    def test_swallowed_503_or_transport_error_is_failed_not_empty(self):
        _,record,calls=self.collect(['https://api.example.test/list?page=0'],[503])
        self.assertEqual(record['status'],'failed')
        self.assertEqual(calls,1)
        import requests,http_diagnostics as d
        def retrieve():
            try:requests.get('https://api.example.test/list?page=0')
            except requests.Timeout:pass
            return []
        with patch('requests.adapters.HTTPAdapter.send',side_effect=requests.Timeout('private detail')):
            _,record=d.collect(SimpleNamespace(retrieve_papers=retrieve),'other','common')
        self.assertEqual(record['status'],'failed')
        self.assertNotIn('private detail',json.dumps(record))

    def test_same_get_transient_recovery_is_not_overstrict_failure(self):
        _,record,calls=self.collect(['https://api.example.test/list?page=0']*2,[503,200])
        self.assertEqual(calls,2) # fake SDK's existing sequence; collector adds zero calls
        self.assertEqual(record['status'],'empty')
        self.assertEqual(record['unrecovered_observed_failures'],[])
        self.assertEqual(len(record['recovered_observed_failures']),1)

    def test_unrelated_successful_page_does_not_clear_failure(self):
        _,record,_=self.collect(['https://api.example.test/list?page=1','https://api.example.test/list?page=2'],[503,200])
        self.assertEqual(record['status'],'failed')
        self.assertEqual(len(record['unrecovered_observed_failures']),1)

    def test_unrelated_successful_endpoint_does_not_clear_swallowed_refusal(self):
        _,record,_=self.collect(['https://api.example.test/list','https://other.example.test/health'],[403,200])
        self.assertEqual(record['status'],'failed')
        self.assertEqual(record['recovered_observed_failures'],[])

    def test_prior_call_failure_and_stale_sdk_failure_not_charged_to_next_call(self):
        import http_diagnostics as d
        self.collect(['https://api.example.test/list'],[403])
        retriever=SimpleNamespace(retrieve_papers=lambda:[],failures=['historic unrelated record'])
        _,record=d.collect(retriever,'other','common')
        self.assertEqual(record['status'],'empty')
        self.assertEqual(record['reported_subsource_failures'],0)
        self.assertEqual(record['events'],[])

    def test_partial_return_with_unrecovered_page_failure_is_partial(self):
        import requests,http_diagnostics as d
        def retrieve():
            requests.get('https://api.example.test/list?page=2')
            return [SimpleNamespace(title='Synthetic partial result')]
        with patch('requests.adapters.HTTPAdapter.send',return_value=self.response(503)):
            papers,record=d.collect(SimpleNamespace(retrieve_papers=retrieve),'other','common')
        self.assertEqual(len(papers),1)
        self.assertEqual(record['status'],'partial')
        self.assertFalse(record['coverage_complete'])

    def test_existing_journal_adapter_allowed_fallback_recovers_404(self):
        import requests,http_diagnostics as d
        from zot2dailypaper.retriever.journal_retriever import JournalRetriever
        retriever=JournalRetriever(Guards().production_source_config())
        def journal(*args):
            a=requests.get('https://api.crossref.org/works?filter=fixture')
            b=requests.get('https://publisher.example.test/feed')
            return None,[],False # original adapter contract: valid documented fallback
        retriever._journal=journal
        retriever.retrieve_papers=lambda:(retriever._journal(None,None,None) and [])
        with patch('requests.adapters.HTTPAdapter.send',side_effect=[self.response(404),self.response(200)]):
            _,record=d.collect(retriever,'journals','common')
        self.assertEqual(record['status'],'empty')
        self.assertEqual(len(record['recovered_observed_failures']),1)
        self.assertEqual(record['unrecovered_observed_failures'],[])
        self.assertEqual(record['events'][0]['operation_id'],record['events'][1]['operation_id'])

    def test_journal_adapter_unrecovered_refusal_stays_failed(self):
        import requests,http_diagnostics as d
        from zot2dailypaper.retriever.journal_retriever import JournalRetriever
        retriever=JournalRetriever(Guards().production_source_config())
        def journal(*args):
            requests.get('https://api.crossref.org/works?filter=fixture')
            requests.get('https://publisher.example.test/feed')
            return None,[],True # original policy:403+RSS is still incomplete
        retriever._journal=journal
        retriever.retrieve_papers=lambda:(retriever._journal(None,None,None) and [])
        with patch('requests.adapters.HTTPAdapter.send',side_effect=[self.response(403),self.response(200)]) as sender:
            _,record=d.collect(retriever,'journals','common')
        self.assertEqual(sender.call_count,2) # one per distinct existing endpoint, no retry
        self.assertEqual(record['status'],'failed')
        self.assertEqual(len(record['unrecovered_observed_failures']),1)

    def test_distinct_journal_operations_cannot_rescue_each_other(self):
        import requests,http_diagnostics as d
        from zot2dailypaper.retriever.journal_retriever import JournalRetriever
        retriever=JournalRetriever(Guards().production_source_config())
        def journal(i,*args):
            requests.get('https://api.crossref.org/works?filter='+str(i))
            return None,[],bool(i==1)
        retriever._journal=journal
        def retrieve():retriever._journal(1,None,None);retriever._journal(2,None,None);return []
        retriever.retrieve_papers=retrieve
        with patch('requests.adapters.HTTPAdapter.send',side_effect=[self.response(403),self.response(200)]):
            _,record=d.collect(retriever,'journals','common')
        self.assertEqual(record['status'],'failed')
        self.assertNotEqual(record['events'][0]['operation_id'],record['events'][1]['operation_id'])


class DualPoolAndDisplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls): FullPoolControls.setUpClass()

    def evaluate(self,rows,prior):
        fixture=FullPoolControls
        protocol={**fixture.protocol,'excluded_studies_sha256':e.digest(prior),'validation':{'require_complete_abstract':True}}
        snapshot={'source_commit':e.PIN,'config':OmegaConf.to_container(fixture.cfg),'model_identity':fixture.identity,'corpus':fixture.corpus,'candidates':rows,'candidate_events':[],'history':{'sha256':'synthetic'},'query_cutoff':'2026-11-03T00:00:00Z','source_days':{'arxiv':7,'journals':7},'source_attempts':[{'source':'arxiv','scope':'common','status':'success'}],'expected_A':copy.deepcopy(fixture.pool['arms']['A']),'synthetic_fixture_only':True}
        calls=[];original=e.score_full_pool
        def scoring(rows,*args,**kwargs):calls.append([r['id'] for r in rows]);return original(rows,*args,**kwargs)
        with tempfile.TemporaryDirectory() as directory,patch.object(e,'HERE',Path(directory)),patch.object(e,'verify_source',return_value={'fixture':'synthetic only'}),patch.object(e,'score_full_pool',side_effect=scoring):
            e.private_json(Path(directory)/'freeze_receipt.private.json',{'protocol_sha256':e.digest(protocol),'frozen_C_sha256':e.digest(protocol['C'])})
            e.private_json(Path(directory)/'runtime_config.frozen.private.json',OmegaConf.to_container(fixture.cfg))
            result=e.evaluate(snapshot,fixture.pack,protocol,prior)
        return result,calls

    def test_old_high_rank_and_uncertain_date_excluded_before_validation_scores(self):
        fixture=FullPoolControls;rows=copy.deepcopy(fixture.rows)
        order=fixture.pool['arms']['A']['model_order'];old,unknown=order[:2]
        old_row=next(r for r in rows if r['id']==old)
        prior=[{'id':'previous-family','title':'Prior exposed version','identifiers':[old_row['doi']]}]
        next(r for r in rows if r['id']==unknown)['original_date_status']='unknown_revised_record'
        result,calls=self.evaluate(rows,prior)
        self.assertEqual(result['reproduction']['status'],'verified')
        self.assertEqual(len(result['production_full_pool_audit']['candidate_ids']),120)
        self.assertEqual(len(result['validation_common_full_pool']['candidate_ids']),118)
        self.assertEqual(len(calls[0]),120)
        self.assertEqual(len(calls[1]),118)
        for key in (old,unknown):self.assertIn(key,calls[0]);self.assertNotIn(key,calls[1])
        for arm in result['validation_common_full_pool']['arms'].values():
            self.assertNotIn(old,arm['rows']);self.assertNotIn(unknown,arm['order'])
            self.assertFalse({old,unknown}&set(arm['selection']['main_order']+arm['selection']['random']))
        self.assertTrue(result['primary_blind_plan']['ready_for_independent_main_metrics'])
        self.assertEqual(result['primary_blind_plan']['blocked_prior_or_unknown_heads'],[])
        self.assertIn('not unfiltered production heads',result['pool_separation']['claim'])
        a.check_export(result)

    def test_phase1_exact_top10_union_phase2_fixed_complement(self):
        fixture=FullPoolControls
        plan=e.plan_actual_heads(fixture.pool,fixture.rows,{r['id']:'eligible' for r in fixture.rows},fixture.protocol,'independent_common')
        mapping=plan['blind_id_to_candidate_id'];first=plan['display_stages']['1'];second=plan['display_stages']['2']
        expected=set()
        for arm in fixture.pool['arms'].values():expected.update(arm['model_order'][:10]);expected.update(arm['selection']['main_order'][:10])
        self.assertEqual({mapping[k] for k in first},expected)
        self.assertEqual(set(first)&set(second),set())
        self.assertEqual(set(first+second),set(mapping))
        self.assertEqual([key for key in mapping if key in set(first)],first)
        self.assertEqual([key for key in mapping if key in set(second)],second)
        self.assertLessEqual(len(first),60)

    def test_staged_csv_preserves_ids_batches_and_never_exports_algorithm_hints(self):
        import csv
        fixture=FullPoolControls
        plan=e.plan_actual_heads(fixture.pool,fixture.rows,{r['id']:'eligible' for r in fixture.rows},fixture.protocol,'independent_common')
        result={'primary_blind_plan':plan,'blind_id_to_candidate_id':plan['blind_id_to_candidate_id']}
        with tempfile.TemporaryDirectory() as directory:
            files=e.write_staged_blind_csv(Path(directory)/'a',result,{'candidates':fixture.rows})
            for stage in ('1','2'):
                path=Path(directory)/'a'/f'phase{stage}.private.csv'
                with path.open() as f:records=list(csv.DictReader(f))
                self.assertEqual([r['blind_id'] for r in records],plan['display_stages'][stage])
                self.assertTrue(all(not r['relevance_0_to_3'] and not r['optional_reason'] for r in records))
                self.assertFalse(set(records[0])&{'score','rank','algorithm','sampling_path','sampling_reasons','recommendation_reason'})
                batch_ids=[]
                for file in sorted((Path(directory)/'a').glob(f'phase{stage}_batch*.private.csv')):
                    with file.open() as f:batch=list(csv.DictReader(f))
                    self.assertLessEqual(len(batch),5);batch_ids += [r['blind_id'] for r in batch]
                self.assertEqual(batch_ids,plan['display_stages'][stage])
            labelled=copy.deepcopy(result);labelled['synthetic_user_labels']={key:0 for key in result['blind_id_to_candidate_id']}
            again=e.write_staged_blind_csv(Path(directory)/'b',labelled,{'candidates':fixture.rows})
            self.assertEqual([p.read_bytes() for p in files],[p.read_bytes() for p in again])

    def test_export_cli_needs_no_private_protocol_embeddings_or_source_calls(self):
        fixture=FullPoolControls
        plan=e.plan_actual_heads(fixture.pool,fixture.rows,{r['id']:'eligible' for r in fixture.rows},fixture.protocol,'independent_common')
        result={'primary_blind_plan':plan,'blind_id_to_candidate_id':plan['blind_id_to_candidate_id'],'public_candidates':fixture.rows}
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'decrypted.private.json';e.private_json(file,result)
            out=Path(directory)/'blind'
            with patch.object(sys,'argv',['offline_eval.py','--export-evaluation',str(file),'--out',str(out)]),patch.object(e,'verify_source',side_effect=AssertionError('no inference')):
                e.main()
            self.assertTrue((out/'phase1.private.csv').exists());self.assertTrue((out/'phase2.private.csv').exists())

    def test_invalid_stage_overlap_or_reordering_rejected(self):
        fixture=FullPoolControls
        plan=e.plan_actual_heads(fixture.pool,fixture.rows,{r['id']:'eligible' for r in fixture.rows},fixture.protocol,'independent_common')
        result={'primary_blind_plan':copy.deepcopy(plan),'blind_id_to_candidate_id':plan['blind_id_to_candidate_id']}
        result['primary_blind_plan']['display_stages']['2'].append(plan['display_stages']['1'][0])
        with tempfile.TemporaryDirectory() as directory,self.assertRaisesRegex(ValueError,'fixed_display_partition'):
            e.write_staged_blind_csv(Path(directory),result,{'candidates':fixture.rows})


class CaptureLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Guards.setUpClass()

    def context(self):
        from contextlib import ExitStack
        stack=ExitStack();directory=Path(stack.enter_context(tempfile.TemporaryDirectory()))
        (directory/'recipient.public.pem').write_bytes(Guards.public)
        stack.enter_context(patch.object(a,'PUBLIC',directory))
        stack.enter_context(patch.dict(os.environ,{'JINA_PROGRESS_FD':''}))
        return stack,directory

    def decrypt(self,path):return Guards().decrypt(json.loads(path.read_text()))

    def test_encrypted_seed_exists_before_any_private_work(self):
        stack,directory=self.context()
        with stack:
            out=directory/'result.sealed.json';cp=a.CaptureProgress(out,interval=0)
            value=self.decrypt(out)
            self.assertEqual(value['status'],'incomplete_checkpoint')
            self.assertEqual(value['last_phase'],'startup')
            self.assertFalse(value['baseline_verified']);self.assertFalse(value['blind_ready'])
            self.assertEqual(value['counts'],{})
            self.assertNotIn('last_phase',json.loads(out.read_text()))
            cp.finish({'status':'blocked_before_inference','paid_calls':0})

    def test_counts_and_source_detail_encrypted_public_progress_has_no_private_data(self):
        import io
        stack,directory=self.context()
        with stack:
            out=directory/'result.sealed.json';cp=a.CaptureProgress(out,interval=0)
            stream=io.StringIO();cp.stream=stream
            cp.protocol_hash='a'*64
            attempts=[{'source':'arxiv','status':'failed','error':{'http_status':403,'query_sha256':'b'*64}}]
            cp.record(phase='retrieve_common',source='arxiv',attempts=attempts,zotero_before_filter=895,retrieved_common=222)
            body=self.decrypt(out)
            self.assertEqual(body['counts']['zotero_before_filter'],895)
            self.assertEqual(body['source_attempts'],attempts)
            public=json.loads(stream.getvalue().splitlines()[-1])
            self.assertEqual(set(public),{'phase','source','event','elapsed_seconds','sealed_checkpoint_saved'})
            for key in ('counts','query_sha256','source_attempts','http_status'):self.assertNotIn(key,public)
            cp.stream=None;stream.close();cp.finish({'status':'blocked_or_failed','paid_calls':0})

    def test_atomic_replace_failure_preserves_previous_decryptable_checkpoint(self):
        stack,directory=self.context()
        with stack:
            out=directory/'result.sealed.json';cp=a.CaptureProgress(out,interval=0)
            previous=out.read_bytes()
            with patch.object(a.os,'replace',side_effect=OSError('synthetic I/O')):
                with self.assertRaises(OSError):cp.record(phase='zotero')
            self.assertEqual(out.read_bytes(),previous)
            self.assertEqual(self.decrypt(out)['last_phase'],'startup')
            self.assertFalse(list(directory.glob('.checkpoint-*')))
            cp.finish({'status':'blocked_or_failed','paid_calls':0})

    def test_reject_untrusted_public_phase_or_counter_before_overwriting(self):
        stack,directory=self.context()
        with stack:
            out=directory/'result.sealed.json';cp=a.CaptureProgress(out,interval=0)
            previous=out.read_bytes()
            for args in ({'phase':'synthetic-private-query'},{'phase':'zotero','private_notes':123},{'phase':'zotero','zotero_before_filter':-1}):
                with self.assertRaisesRegex(ValueError,'unsafe_progress'):cp.record(**args)
            self.assertEqual(previous,out.read_bytes())
            cp.finish({'status':'blocked_or_failed','paid_calls':0})

    def test_keyboard_interrupt_preserves_phase_and_non_success_status(self):
        stack,directory=self.context()
        with stack:
            out=directory/'result.sealed.json'
            def fake_capture(private,payload,progress):
                progress.record(phase='initial_rerank',common_after_filters=120)
                raise KeyboardInterrupt('synthetic-sensitive-message')
            env={'RUNNER_TEMP':str(directory),'SEALED_OUTPUT':str(out),'JINA_FULLPOOL_INPUT':'{}'}
            with patch.dict(os.environ,env),patch.object(a,'validate_payload',return_value={'protocol':{}}),patch.object(a,'run_capture',side_effect=fake_capture):
                with self.assertRaises(SystemExit) as error:a.main()
            self.assertEqual(error.exception.code,130)
            value=self.decrypt(out)
            self.assertEqual(value['status'],'capture_cancelled')
            self.assertEqual(value['last_phase'],'initial_rerank')
            self.assertFalse(value['baseline_verified']);self.assertFalse(value['blind_ready'])
            self.assertNotIn('synthetic-sensitive-message',json.dumps(value))
            self.assertFalse(list(directory.glob('jina-offline-*')))

    def child(self,directory):
        import subprocess
        module_dir=str(Path(a.__file__).parent)
        code=f'''import sys,time,os
from pathlib import Path
sys.path.insert(0,{module_dir!r})
import actions_runner as a
a.PUBLIC=Path({str(directory)!r})
a.validate_payload=lambda x:{{'protocol':{{}}}}
def fake(private,payload,progress):
 progress.record(phase='initial_rerank',common_after_filters=120)
 time.sleep(60)
a.run_capture=fake
a.main()
'''
        env=dict(os.environ,RUNNER_TEMP=str(directory),SEALED_OUTPUT=str(directory/'result.sealed.json'),JINA_FULLPOOL_INPUT='{}',JINA_PROGRESS_FD='')
        return subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    def wait_phase(self,process,out,phase='initial_rerank'):
        import time
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if out.exists() and self.decrypt(out).get('last_phase')==phase:return
            if process.poll() is not None:break
            time.sleep(.02)
        process.kill();process.communicate(timeout=3)
        self.fail('synthetic worker did not reach checkpoint')

    def test_cancellation_escapes_nested_sdk_and_collect_exception_handlers(self):
        import http_diagnostics as d
        caught=[]
        def nested_sdk():
            try:
                try:raise a.CaptureCancelled()
                except Exception:
                    caught.append('inner_sdk')
                    return []
            except Exception:
                caught.append('outer_sdk')
                return []
        with self.assertRaises(a.CaptureCancelled):
            d.collect(SimpleNamespace(retrieve_papers=nested_sdk),'other','common')
        self.assertEqual(caught,[])

    def check_signal_at_nested_retrieval_boundary(self,signum,shell_entry=False):
        import shlex,subprocess
        stack,directory=self.context()
        with stack:
            module_dir=str(Path(a.__file__).parent)
            code=f'''import os,sys,time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,{module_dir!r})
import actions_runner as a
import http_diagnostics as d
a.PUBLIC=Path({str(directory)!r})
(a.PUBLIC/'entry_pid').write_text(str(os.getpid()))
a.validate_payload=lambda x:{{'protocol':{{}}}}
def fake(private,payload,progress):
 def sdk():
  try:
   try:
    progress.record(phase='retrieve_common',source='arxiv')
    time.sleep(60)
   except Exception:
    (a.PUBLIC/'inner_handler_ran').touch()
    return []
  except Exception:
   (a.PUBLIC/'outer_handler_ran').touch()
   return []
 d.collect(SimpleNamespace(retrieve_papers=sdk),'other','common')
 (a.PUBLIC/'capture_continued').touch()
 return {{'status':'synthetic_finished','paid_calls':0}}
a.run_capture=fake
a.main()
'''
            out=directory/'result.sealed.json'
            env=dict(os.environ,RUNNER_TEMP=str(directory),SEALED_OUTPUT=str(out),JINA_FULLPOOL_INPUT='{}',JINA_PROGRESS_FD='3' if shell_entry else '')
            command=[sys.executable,'-c',code]
            if shell_entry:
                script=directory/'capture-step.sh'
                script.write_text('exec '+shlex.quote(sys.executable)+' -c '+shlex.quote(code)+' 3>&1 > /dev/null 2>&1\n')
                command=['/bin/bash','--noprofile','--norc','-e','-o','pipefail',str(script)]
            process=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                self.wait_phase(process,out,'retrieve_common')
                self.assertEqual(int((directory/'entry_pid').read_text()),process.pid)
                process.send_signal(signum)
                stdout,stderr=process.communicate(timeout=5)
                self.assertEqual(process.returncode,130,(stdout,stderr))
                value=self.decrypt(out)
                self.assertEqual(value['status'],'capture_cancelled')
                self.assertEqual(value['error_type'],'CaptureCancelled')
                self.assertEqual(value['last_phase'],'retrieve_common')
                self.assertFalse(value['baseline_verified']);self.assertFalse(value['blind_ready'])
                for name in ('inner_handler_ran','outer_handler_ran','capture_continued'):
                    self.assertFalse((directory/name).exists(),name)
                self.assertFalse(list(directory.glob('jina-offline-*')))
            finally:
                if process.poll() is None:
                    process.kill();process.communicate(timeout=3)

    def test_sigterm_escapes_nested_sdk_and_retrieval_boundary(self):
        import signal
        self.check_signal_at_nested_retrieval_boundary(signal.SIGTERM)

    def test_sigint_escapes_nested_sdk_and_retrieval_boundary(self):
        import signal
        self.check_signal_at_nested_retrieval_boundary(signal.SIGINT)

    def test_actions_bash_entry_sigint_reaches_python_and_preserves_checkpoint(self):
        import signal
        self.check_signal_at_nested_retrieval_boundary(signal.SIGINT,shell_entry=True)

    def test_actions_bash_entry_sigterm_reaches_python_and_preserves_checkpoint(self):
        import signal
        self.check_signal_at_nested_retrieval_boundary(signal.SIGTERM,shell_entry=True)

    def test_sigterm_writes_cancelled_checkpoint_and_cleans_private_temp(self):
        import signal
        stack,directory=self.context()
        with stack:
            process=self.child(directory);out=directory/'result.sealed.json';self.wait_phase(process,out)
            process.send_signal(signal.SIGTERM);stdout,stderr=process.communicate(timeout=5)
            self.assertEqual(process.returncode,130)
            value=self.decrypt(out);self.assertEqual(value['status'],'capture_cancelled')
            self.assertEqual(value['last_phase'],'initial_rerank')
            self.assertFalse(value['blind_ready'])
            self.assertFalse(list(directory.glob('jina-offline-*')))

    def test_sigkill_leaves_valid_partial_ciphertext_without_fabricating_completion(self):
        stack,directory=self.context()
        with stack:
            process=self.child(directory);out=directory/'result.sealed.json';self.wait_phase(process,out)
            process.kill();process.communicate(timeout=5)
            value=self.decrypt(out)
            self.assertEqual(value['status'],'incomplete_checkpoint')
            self.assertEqual(value['last_phase'],'initial_rerank')
            self.assertEqual(value['counts']['common_after_filters'],120)
            self.assertFalse(value['baseline_verified']);self.assertFalse(value['blind_ready'])

    def test_heartbeat_encoder_wrapper_is_observational_and_finish_stops_thread(self):
        import time
        stack,directory=self.context()
        with stack:
            out=directory/'result.sealed.json';cp=a.CaptureProgress(out,interval=.01)
            marker=object();calls=[]
            def encode(texts,**kwargs):calls.append((texts,kwargs));time.sleep(.05);return marker
            ranker=SimpleNamespace(_encode_texts=encode)
            cp.trace_encoder(ranker);cp.record(phase='initial_rerank')
            texts=['synthetic private reference text']
            self.assertIs(ranker._encode_texts(texts,prompt_name='document'),marker)
            self.assertEqual(calls,[(texts,{'prompt_name':'document'})])
            body=self.decrypt(out)
            self.assertTrue(any(event['event']=='heartbeat' for event in body['phase_events']))
            self.assertFalse(body['counts']['encoder_present_at_entry'])
            self.assertNotIn(texts[0],json.dumps(body))
            cp.finish({'status':'synthetic_finished','paid_calls':0})
            self.assertFalse(cp.thread.is_alive())
            previous=out.read_bytes();time.sleep(.03);self.assertEqual(out.read_bytes(),previous)
            self.assertEqual(self.decrypt(out)['status'],'synthetic_finished')

    def test_dedicated_fd3_does_not_tee_raw_stdout_or_stderr(self):
        import subprocess,shlex
        stack,directory=self.context()
        with stack:
            module_dir=str(Path(a.__file__).parent)
            code=f'''import sys,os
from pathlib import Path
sys.path.insert(0,{module_dir!r})
import actions_runner as a
a.PUBLIC=Path({str(directory)!r})
cp=a.CaptureProgress(Path({str(directory/'result.sealed.json')!r}),interval=0)
print('synthetic-sensitive-stdout')
os.write(2,b'synthetic-sensitive-stderr')
cp.record(phase='zotero',zotero_before_filter=895)
cp.finish({{'status':'synthetic_finished','paid_calls':0}})
'''
            command=shlex.quote(sys.executable)+' -c '+shlex.quote(code)+' 3>&1 > /dev/null 2>&1'
            result=subprocess.run(command,shell=True,executable='/bin/bash',env=dict(os.environ,JINA_PROGRESS_FD='3'),capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0)
            for line in result.stdout.decode().splitlines():
                value=json.loads(line)
                self.assertLessEqual(set(value),{'phase','source','event','elapsed_seconds','sealed_checkpoint_saved'})
            self.assertNotIn(b'synthetic-sensitive',result.stdout+result.stderr)
            self.assertNotIn(b'zotero_before_filter',result.stdout)
            self.assertEqual(self.decrypt(directory/'result.sealed.json')['status'],'synthetic_finished')

    def test_step_time_budgets_leave_upload_and_cleanup_inside_45_minutes(self):
        import yaml
        path=Path(a.__file__).parents[2]/'.github/workflows/test.yml'
        config=yaml.load(path.read_text(),Loader=yaml.BaseLoader);job=config['jobs']['isolated-jina']
        self.assertEqual(job['timeout-minutes'],'45')
        steps=job['steps'];self.assertEqual(sum(int(step['timeout-minutes']) for step in steps),44)
        capture=next(s for s in steps if s.get('name')=='Capture and compare privately')
        upload=next(s for s in steps if s.get('name')=='Retain ciphertext only')
        self.assertEqual(capture['timeout-minutes'],'32')
        self.assertEqual(capture['env']['JINA_PROGRESS_FD'],'3')
        self.assertTrue(capture['run'].startswith('exec .venv/bin/python '))
        self.assertIn('3>&1 > /dev/null 2>&1',capture['run'])
        self.assertEqual(upload['if'],'${{ always() }}')
        self.assertNotIn('cancelled',upload['if'])
        self.assertEqual(upload['with']['path'],'sealed/result.sealed.json')
        self.assertEqual(upload['with']['retention-days'],'1')


if __name__ == '__main__':
    unittest.main(verbosity=2)
