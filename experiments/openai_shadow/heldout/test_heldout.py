"""Synthetic tests only: crypto, sampling, payload privacy and shared accounting."""
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import common as c
import pilot_budget as b

def history():
    old=b.previous
    claim={'schema':1,'plan':'openai-shadow-20261029-v1','run_id':old.V1_RUN,'source_sha':old.V1_COMMIT,'status':'reserved_outcome_unknown','currency':'USD','limit_usd':'1.000000000','reserved_usd':'1.000000000'}
    state=old.reconcile(claim,'7fe07670fbcb6650a4835add634ec0edd931af73',old.verified_receipt())
    state=old.reserve(state,'40863920037','cc3fb9d98d1efe8d512831aad0c70853a5e9e2ff','89a083dbd6e256e14ec264f31aa447f0e087db908594646ab0c12a06d8fac0b7',30_000_000)
    return old.finish(state,'40863920037',{'status':'completed','usage_complete':True,'input_tokens':2634,'cached_input_tokens':0,'cache_write_tokens':0,'output_tokens':0,'reasoning_tokens':0,'measured_cost_nanos':263400})

def synthetic_rows(n=72):
    return [{'id':f'H{i+1:03d}','canonical_id':f'10.1234/unique{i:03d}','title':f'UniqueTitle{i} UniqueTopic{i}',
             'abstract':('Synthetic complete abstract describing public scientific evidence '+str(i)+' ')*10,
             'source':'journals' if i%3==0 else 'arxiv' if i%3==1 else 'chemrxiv','group':'journals' if i%3==0 else 'preprints',
             'direction':i%6,'keyword_cosine':.2+i/200,'baseline_score':float((i*7)%n),'link':f'https://doi.org/10.1234/unique{i:03d}',
             'abstract_provenance':{'source':'synthetic_fixture_only'}} for i in range(n)]

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        cls.private=cls.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        cls.public=cls.key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    def test_encryption_roundtrip_and_no_plaintext(self):
        data={'private_rankings':'PRIVATE_RANKING_SENTINEL','mapping':['H002','H001'],'note':'PRIVATE_NOTE_SENTINEL'}
        envelope=c.seal(data,self.public)
        self.assertEqual(c.unseal(envelope,self.private),data)
        self.assertNotIn('PRIVATE_',json.dumps(envelope));self.assertNotIn('BEGIN PRIVATE KEY',json.dumps(envelope))
        self.assertNotEqual(c.seal(data,self.public)['ciphertext'],envelope['ciphertext'])
    def test_tampered_ciphertext_and_wrong_recipient_fail(self):
        envelope=c.seal({'test':1},self.public)
        altered=copy.deepcopy(envelope);raw=bytearray(base64.b64decode(altered['ciphertext']));raw[0]^=1
        altered['ciphertext']=base64.b64encode(raw).decode()
        with self.assertRaises(Exception):c.unseal(altered,self.private)
        altered=copy.deepcopy(envelope);altered['protocol_sha256']='0'*64
        with self.assertRaises(c.Stop):c.unseal(altered,self.private)
        other=rsa.generate_private_key(public_exponent=65537,key_size=3072).private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        with self.assertRaises(c.Stop):c.unseal(envelope,other)
    def test_frozen_protocol_and_rubric(self):
        self.assertEqual(c.digest(json.loads((c.ROOT/'protocol.public.json').read_text())),c.PLAN_SHA)
        self.assertEqual(c.digest(json.loads((c.ROOT.parent/'rubric.public.json').read_text())),c.RUBRIC_SHA)
    def test_sampling_reproducible_balanced_and_without_duplicates(self):
        rows=synthetic_rows();a,meta=c.select(rows);z,_=c.select(list(reversed(rows)))
        self.assertEqual([p['id'] for p in a],[p['id'] for p in z]);self.assertEqual(len({p['id'] for p in a}),24)
        self.assertEqual(len(meta['strata']),24);self.assertGreaterEqual(len(meta['source_counts']),2)
    def test_insufficient_or_single_group_pool_blocks(self):
        with self.assertRaises(c.Stop):c.select(synthetic_rows(23))
        with self.assertRaises(c.Stop):c.select([dict(p,group='journals') for p in synthetic_rows()])
    def test_versions_development_duplicates_and_missingness_excluded(self):
        plan=json.loads((c.ROOT/'protocol.public.json').read_text());rows=synthetic_rows(5)
        rows[0]['canonical_id']='https://doi.org/10.26434/chemrxiv.15002639/v8'
        rows[1]['canonical_id']='https://arxiv.org/abs/2610.37581v3'
        rows[2]['abstract']='';rows[3]['abstract']='truncated '*30+'…'
        kept,excluded=c.eligible(rows,plan,[])
        self.assertEqual(len(kept),1);self.assertEqual(len(excluded),4)
        rows=synthetic_rows(2);rows[1]['title']=rows[0]['title']
        self.assertEqual(len(c.eligible(rows,plan,[])[0]),1)
    def test_payload_has_only_public_text_and_frozen_rubric(self):
        rows=synthetic_rows(24)
        for p in rows:p.update(label='EXPECTED_LABEL_SENTINEL',corpus='ZOTERO_PRIVATE_SENTINEL')
        batches=c.requests_for(rows,json.loads((c.ROOT.parent/'rubric.public.json').read_text()))
        serialized=c.dumps(batches)
        self.assertNotIn('EXPECTED_LABEL_SENTINEL',serialized);self.assertNotIn('ZOTERO_PRIVATE_SENTINEL',serialized)
        self.assertEqual(sum(len(x['questions']) for x in batches),24)
        self.assertTrue(all(len(x['questions'])<=4 for x in batches))
    def test_blind_table_hides_scores_mapping_and_strata(self):
        rows=synthetic_rows(24);blind,mapping=c.blind_rows(rows)
        self.assertEqual(len(mapping),24);self.assertEqual(set(mapping.values()),{p['id'] for p in rows})
        for row in blind:
            self.assertEqual(set(row),{'id','title','link','abstract_excerpt','relevance','reason'})
            self.assertLessEqual(len(row['abstract_excerpt'].split()),26)
            self.assertEqual(row['relevance'],'')
    def test_prior_ledger_preserved_and_both_costs_counted(self):
        prior=history();state=b.reserve(prior,'123','c'*40,[{'request_sha256':'d'*64,'reserved_nanos':1_000_000}])
        self.assertEqual(state['previous_ledger'],prior);self.assertEqual(state['summary']['known_cost_total_usd'],'0.000437000')
        with self.assertRaises(c.Stop):b.reserve(state,'124','e'*40,[{'request_sha256':'f'*64,'reserved_nanos':1}])
        with self.assertRaises(c.Stop):b.reserve(prior,'123','c'*40,[{'request_sha256':'d'*64,'reserved_nanos':b.CAP-b.HISTORY+1}])
    def test_unknown_cost_keeps_reservation_and_never_retries(self):
        state=b.reserve(history(),'123','c'*40,[{'request_sha256':'d'*64,'reserved_nanos':1_000_000},{'request_sha256':'e'*64,'reserved_nanos':2_000_000}])
        state=b.start(state,'123',0,'d'*64)
        state=b.finish_call(state,'123',0,{'status':'transport_failure','usage_complete':False})
        with self.assertRaises(c.Stop):b.start(state,'123',1,'e'*64)
        state=b.close(state,'123',False)
        self.assertEqual(state['summary']['unknown_or_pending_reserved_usd'],'0.001000000')
        self.assertEqual(state['pilot']['status'],'usage_unknown')
        with self.assertRaises(c.Stop):b.ready(state)
    def test_measured_cost_and_public_accounting_redaction(self):
        state=b.reserve(history(),'123','c'*40,[{'request_sha256':'d'*64,'reserved_nanos':1_000_000}]);state=b.start(state,'123',0,'d'*64)
        state=b.finish_call(state,'123',0,{'status':'completed','usage_complete':True,'measured_cost_nanos':100_000,'input_tokens':1000,'scores':{'PRIVATE_PAPER':2.5},'corpus':'PRIVATE_CORPUS'})
        state=b.close(state,'123',True)
        self.assertEqual(state['summary']['known_cost_total_usd'],'0.000537000')
        self.assertNotIn('PRIVATE_',c.dumps(state['pilot']))
        with self.assertRaises(c.Stop):b.ready(state)
    def test_history_tampering_wrong_owner_and_early_success_block(self):
        bad=history();bad['extension']['measured_cost_nanos']=0
        with self.assertRaises(c.Stop):b.ready(bad)
        state=b.reserve(history(),'123','c'*40,[{'request_sha256':'d'*64,'reserved_nanos':1_000_000}])
        with self.assertRaises(c.Stop):b.start(state,'124',0,'d'*64)
        with self.assertRaises(c.Stop):b.close(state,'123',True)
    def test_ordinal_metrics_refuse_missing_labels(self):
        scores={f'B{i:03d}':float(24-i) for i in range(24)};labels={key:3 if i<10 else 0 for i,key in enumerate(scores)}
        self.assertEqual(c.metrics(scores,labels)['precision_at_10'],1)
        self.assertEqual(c.metrics(scores,labels)['ndcg_at_10'],1)
        labels.pop(next(iter(labels)))
        with self.assertRaises(c.Stop):c.metrics(scores,labels)
    def test_remote_cas_happens_before_transport_and_failed_cas_sends_nothing(self):
        spec=importlib.util.spec_from_file_location('heldout_live_test',c.ROOT/'runner.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
        client=c.new_core();doc=synthetic_rows(24)
        request=c.requests_for(doc,json.loads((c.ROOT.parent/'rubric.public.json').read_text()))[0]
        client.EXPECTED_IDS=frozenset(q['name'] for q in request['questions'])
        state=b.reserve(history(),'123','c'*40,[{'request_sha256':c.digest(request),'reserved_nanos':client.reserve_nanos('decisions',request)}]);events=[]
        remote={'state':state,'sha':'a'*40}
        def put(new,sha):self.assertEqual(sha,remote['sha']);remote['state']=copy.deepcopy(new);events.append('remote_'+new['pilot']['calls'][0]['status'])
        r.read_remote=lambda:(copy.deepcopy(remote['state']),remote['sha']);r.put_remote=put
        old=os.environ.get('GITHUB_RUN_ID');os.environ['GITHUB_RUN_ID']='123'
        def send(*args):
            events.append('model');self.assertEqual(remote['state']['pilot']['calls'][0]['status'],'pending')
            return {'model':client.MODEL,'usage':{'input_tokens':1000,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens':0,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':1000},'answers':[{'type':'score','name':key,'score':1.5,'confidence':.5,'probabilities':[{'value':i,'label':str(i),'probability':.25} for i in range(4)]} for key in sorted(client.EXPECTED_IDS)]}
        try:
            with tempfile.TemporaryDirectory() as temp:
                receipt=client.run_one('decisions',request,r.CallLedger(client,Path(temp),0),'synthetic_key',send=send)
                self.assertEqual(receipt['status'],'completed');self.assertEqual(events,['remote_pending','model','remote_complete'])
            remote['state']=state;events.clear()
            def denied(*args):raise OSError('synthetic CAS failure')
            r.put_remote=denied
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(OSError):client.run_one('decisions',request,r.CallLedger(client,Path(temp),0),'synthetic_key',send=send)
            self.assertEqual(events,[])
        finally:
            if old is None:os.environ.pop('GITHUB_RUN_ID',None)
            else:os.environ['GITHUB_RUN_ID']=old
    def test_full_synthetic_pilot_and_private_export(self):
        spec=importlib.util.spec_from_file_location('heldout_e2e_test',c.ROOT/'runner.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
        rows=synthetic_rows(24);remote={'state':history(),'sha':'a'*40};events=[]
        def read():return copy.deepcopy(remote['state']),remote['sha']
        def put(state,sha):
            self.assertEqual(sha,remote['sha']);remote['state']=copy.deepcopy(state);events.append('ledger_'+state['pilot']['status'])
        def factory():
            client=c.new_core()
            def transport(endpoint,request,key):
                self.assertEqual(endpoint,'https://api.openai.com/v1/decisions')
                self.assertEqual(remote['state']['pilot']['status'],'reserved');events.append('model')
                self.assertNotIn('ZOTERO_CORPUS_SENTINEL',request['input'])
                self.assertNotIn('baseline_score',request['input'])
                return {'model':client.MODEL,'usage':{'input_tokens':1000,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens':0,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':1000},'answers':[{'type':'score','name':q['name'],'score':1.5,'confidence':.5,'probabilities':[{'value':i,'label':str(i),'probability':.25} for i in range(4)]} for q in request['questions']]}
            client.transport=transport;return client
        env={'GITHUB_REPOSITORY':'wankiwi/zot2dailypaper','GITHUB_REF':'refs/heads/experiment/openai-shadow-20261029','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1','GITHUB_WORKFLOW':'Test','GITHUB_RUN_ID':'123','GITHUB_SHA':'c'*40,'OPENAI_API_KEY':'synthetic_key','OPENAI_API_KEY_ISSUER':'https://api.openai.com','OPENAI_API_BASE':'https://api.openai.com/v1','OPENAI_BASE_URL':''}
        with tempfile.TemporaryDirectory() as temp,mock.patch.dict(os.environ,env),mock.patch.object(r,'read_remote',read),mock.patch.object(r,'put_remote',put),mock.patch.object(r,'new_core',factory),mock.patch.object(r,'load_baseline',return_value=(object(),['ZOTERO_CORPUS_SENTINEL'],object(),{'profile_settings_sha256':'f'*64})),mock.patch.object(r,'retrieve_and_rank',return_value=(rows,{'eligible_count':72,'candidate_ids':[p['canonical_id'] for p in rows]})):
            result={'status':'blocked_before_model'};r.execute(Path(temp),result)
            self.assertEqual(result['status'],'completed');self.assertEqual(result['api_requests_started'],6)
            self.assertEqual(len(result['decisions_scores']),24)
            self.assertEqual(result['cumulative_budget']['known_cost_total_usd'],'0.001037000')
            self.assertEqual(result['cumulative_budget']['unknown_or_pending_reserved_usd'],'0.000000000')
            self.assertEqual(remote['state']['previous_ledger'],history());self.assertEqual(remote['state']['pilot']['status'],'completed')
            self.assertLess(events.index('ledger_reserved'),events.index('model'))
            self.assertNotIn('ZOTERO_CORPUS_SENTINEL',c.dumps(result))
            self.assertNotIn(rows[0]['abstract'],c.dumps(result))
            self.assertNotIn('synthetic_key',c.dumps(result))
            self.assertNotIn('scores',c.dumps(remote['state']['pilot']))
            encrypted=c.seal(result,self.public)
            ds=importlib.util.spec_from_file_location('heldout_decrypt_test',c.ROOT/'decrypt.py');decoder=importlib.util.module_from_spec(ds);ds.loader.exec_module(decoder)
            summary=decoder.write_deliverables(encrypted,self.private,Path(temp)/'deliver')
            self.assertEqual(summary['rows'],24)
            import csv
            with Path(summary['blind_pack']).open(encoding='utf-8-sig') as f:table=list(csv.reader(f))
            self.assertEqual(len(table),25)
            self.assertFalse(any('baseline' in cell or 'decisions' in cell for row in table for cell in row))
            with self.assertRaises(c.Stop):b.ready(remote['state'])
    def test_source_gate_failure_never_claims_or_calls(self):
        spec=importlib.util.spec_from_file_location('heldout_gate_test',c.ROOT/'runner.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
        env={'GITHUB_REPOSITORY':'wankiwi/zot2dailypaper','GITHUB_REF':'refs/heads/experiment/openai-shadow-20261029','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1','GITHUB_WORKFLOW':'Test','GITHUB_RUN_ID':'123','GITHUB_SHA':'c'*40,'OPENAI_API_KEY':'synthetic_key','OPENAI_API_KEY_ISSUER':'https://api.openai.com','OPENAI_API_BASE':'https://api.openai.com/v1','OPENAI_BASE_URL':''}
        with tempfile.TemporaryDirectory() as temp,mock.patch.dict(os.environ,env),mock.patch.object(r,'read_remote',return_value=(history(),'a'*40)),mock.patch.object(r,'put_remote') as put,mock.patch.object(r,'load_baseline',side_effect=c.Stop('synthetic_preflight_failure')):
            with self.assertRaisesRegex(c.Stop,'synthetic_preflight_failure'):r.execute(Path(temp),{})
            put.assert_not_called()
    @unittest.skipUnless(importlib.util.find_spec('omegaconf'),'production dependency test runs in the workflow environment')
    def test_production_config_loader_preserves_private_runtime_and_real_path_filter(self):
        from datetime import datetime,timezone
        import shutil
        from zot2dailypaper.executor import Executor
        from zot2dailypaper.protocol import CorpusPaper
        spec=importlib.util.spec_from_file_location('heldout_baseline_test',c.ROOT/'runner.py');r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
        actual_root=next(p for p in [Path.cwd(),*Path(__file__).resolve().parents] if (p/'scripts/prepare_workflow.py').is_file() and (p/'config').is_dir())
        # The public test uses synthetic directions/paths/weights, never real preferences.
        custom='interest_profile:\n  keywords: [synthetic_alpha, synthetic_beta]\n  keyword_weight: 0.25\n  zotero_weight: 0.75\nzotero:\n  ignore_path: [synthetic_omit]\nreranker:\n  strategy: multi_interest_profile\n  missing_abstract_factor: 0.65\n'
        corpus=[CorpusPaper(title='SYNTHETIC_PRIVATE_TITLE_A',abstract='SYNTHETIC_PRIVATE_ABSTRACT_A',doi='10.1234/a',added_date=datetime.now(timezone.utc),paths=['synthetic_omit']),CorpusPaper(title='SYNTHETIC_PRIVATE_TITLE_B',abstract='SYNTHETIC_PRIVATE_ABSTRACT_B',doi='10.1234/b',added_date=datetime.now(timezone.utc),paths=['synthetic_keep'])]
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)/'baseline';base.mkdir();shutil.copytree(actual_root/'config',base/'config');(base/'scripts').mkdir();shutil.copy2(actual_root/'scripts/prepare_workflow.py',base/'scripts/prepare_workflow.py')
            env={'BASELINE_ROOT':str(base),'CUSTOM_CONFIG':custom,'ZOTERO_ID':'synthetic_id','ZOTERO_KEY':'synthetic_zotero_key'}
            with mock.patch.dict(os.environ,env),mock.patch.object(r.subprocess,'check_output',return_value=c.PRODUCTION+'\n'),mock.patch.object(Executor,'fetch_zotero_corpus',return_value=corpus),mock.patch.object(Executor,'run',side_effect=AssertionError('no production run or delivery')):
                executor,filtered,profile,evidence=r.load_baseline(Path(temp)/'private')
                self.assertEqual(len(filtered),1);self.assertEqual(filtered[0].doi,'10.1234/b')
                self.assertEqual(evidence['effective_keyword_weight'],.25);self.assertEqual(evidence['effective_zotero_weight'],.75)
                self.assertEqual(evidence['missing_abstract_factor'],.65)
                self.assertFalse(executor.config.llm.enabled);self.assertFalse(executor.config.output.email.enabled)
                self.assertNotIn('SYNTHETIC_PRIVATE',c.dumps(evidence));self.assertNotIn('synthetic_zotero_key',c.dumps(evidence))

if __name__=='__main__':unittest.main()
