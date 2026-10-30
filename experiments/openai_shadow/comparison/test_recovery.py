"""Synthetic controlled recovery; no live calls, private profile or human labels."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote
import logic as L
import costs
import experiment as E
import recover as R
import recovery_sources as S
import test_comparison as F

def fixture():
    from zot2dailypaper.abstracts import clean_abstract
    p=F.protocol();rows=F.rows(20)
    for i,x in enumerate(rows):
        x['source']=('journals','biorxiv','chemrxiv')[i%3]
        x['abstract']=clean_abstract(x['abstract']);x['abstract_sha256']=L.prior.text_sha(x['abstract']);x['abstract_bytes']=len(x['abstract'].encode())
        x['abstract_provenance']={'source':'synthetic','url':'https://api.crossref.org/works/'+x['canonical_id']}
        x['baseline_direction_index']=x['direction']
    old=costs.reserve(F.prior_ledger(),costs.FAILED_RUN,'ade4e223ba02c46dee9aa4d64a3d1de480a5f2a8',20,L.bound_plans(rows,p))
    old=costs.start(old,costs.FAILED_RUN,0,'llm',old['round']['calls'][0]['reserved_nanos'])
    old=costs.finish(old,costs.FAILED_RUN,0,{'status':'parse_failure','usage_complete':True,'measured_cost_nanos':485700,'input_tokens':927,'output_tokens':786,'reasoning_tokens':516})
    old=costs.close(old,costs.FAILED_RUN,False)
    baseline={'production_commit':L.prior.PRODUCTION,'profile_settings_sha256':'f'*64,'keyword_count':6}
    scores={x['id']:x['baseline_score'] for x in rows};order=sorted(scores,key=lambda k:(-scores[k],k));blind,mapping=L.prior.blind_rows(rows)
    original={'run_id':costs.FAILED_RUN,'protocol_sha256':L.digest(p),'candidates':rows,'baseline':baseline,'baseline_scores':scores,'baseline_order':order,'blind_rows':blind,'blind_to_scored_id':mapping,'pool':{'selection':'SYNTHETIC_PRIVATE_SELECTION'},'model_receipts':[{'status':'parse_failure','estimated_cost_usd':'0.000485700'}]}
    snapshot={'schema':1,'id':costs.RECOVERY,'source_run_id':costs.FAILED_RUN,'source_commit':'ade4e223ba02c46dee9aa4d64a3d1de480a5f2a8','artifact_sha256':'e'*64,'ledger_sha256':L.digest(old),'protocol_sha256':L.digest(p),'candidates':R.project_candidates(rows),'baseline_reference':{'source_run_id':costs.FAILED_RUN,'snapshot_sha256':L.digest(baseline),'scores_sha256':L.digest({'baseline_scores':scores,'baseline_order':order})},'candidate_snapshot_sha256':L.digest(R.project_candidates(rows)),'blind_order_sha256':L.digest({'blind_rows':blind,'blind_to_scored_id':mapping})}
    def get(url):
        doi=unquote(url.split('/works/',1)[-1] if '/works/' in url else url.split('/details/biorxiv/',1)[-1]);x=next(x for x in rows if x['canonical_id']==doi)
        if '/works/' in url:return {'message':{'DOI':doi,'title':[x['title']],'abstract':x['abstract']}}
        return {'collection':[{'doi':doi,'title':x['title'],'abstract':x['abstract'],'version':'1'}]}
    return p,rows,old,original,{'protocol':p,'recovery':snapshot},get

class RecoveryTests(unittest.TestCase):
    def test_key_order_does_not_change_frozen_protocol_or_path_indices(self):
        p=F.protocol();normalized=json.loads(L.dumps(p));self.assertEqual(L.validate_protocol(normalized)['criterion_sha256'],p['criterion_sha256'])
        self.assertEqual(json.loads(L.decision_request(F.rows(1)[0],normalized)['input'])['criterion']['path_order'],['C','M','E'])
    def test_fixed_input_has_no_baseline_scores_or_blind_mapping(self):
        p,rows,old,original,doc,get=fixture();R.validate_input(doc)
        text=L.dumps(doc);self.assertNotIn('baseline_order',text);self.assertNotIn('blind_to_scored_id',text);self.assertNotIn('SYNTHETIC_PRIVATE_SELECTION',text)
        doc['recovery']['candidates'][0]['abstract_sha256']='f'*64
        with self.assertRaises(L.Stop):R.validate_input(doc)
    def test_original_sources_must_match_exact_title_doi_and_abstract_hash(self):
        p,rows,old,original,doc,get=fixture();resolved,failures=S.resolve_all(doc['recovery']['candidates'],get)
        self.assertEqual(len(resolved),20);self.assertEqual(failures,[])
        row=doc['recovery']['candidates'][0]
        raw=get('https://api.crossref.org/works/'+row['canonical_id'])
        for field,value in [('DOI','10.4321/different'),('title',['Different source title']),('abstract','A changed abstract.')]:
            bad=copy.deepcopy(raw);bad['message'][field]=value
            with self.assertRaises(L.Stop):S.resolve(row,lambda url:bad)
    def test_verified_fixed_abstract_avoids_refetch_and_still_requires_exact_hash(self):
        p,rows,old,original,doc,get=fixture();row=rows[2]
        entry={'text':row['abstract'],'source_url':'https://publisher.example.test/journal/','source_kind':'synthetic_publisher_hash_verified'}
        doc['recovery']['verified_public_abstracts']={row['id']:entry};R.validate_input(doc)
        called=[]
        def guarded(url):
            called.append(url)
            self.assertNotIn(row['canonical_id'],unquote(url))
            return get(url)
        resolved,failures=S.resolve_all(doc['recovery']['candidates'],guarded,doc['recovery']['verified_public_abstracts'])
        self.assertEqual(len(resolved),20);self.assertEqual(len(called),19);self.assertEqual(failures,[])
        bad=copy.deepcopy(doc);bad['recovery']['verified_public_abstracts'][row['id']]['text']+=' changed'
        with self.assertRaisesRegex(L.Stop,'original_hash'):R.validate_input(bad)
        self.assertEqual(doc['recovery']['candidate_snapshot_sha256'],L.digest(doc['recovery']['candidates']))
    def test_partial_preflight_retains_only_verified_public_inputs_in_private_result(self):
        p,rows,old,original,doc,get=fixture();env=dict(F.ENV,GITHUB_RUN_ID='888',GITHUB_SHA='d'*40,SHADOW_PROTOCOL_V2=L.dumps(doc))
        def one_missing(url):
            if rows[2]['canonical_id'] in unquote(url):return {'message':{'DOI':rows[2]['canonical_id'],'title':[rows[2]['title']]}}
            return get(url)
        result={}
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,env),mock.patch.object(E,'price_guard',return_value={}),mock.patch.object(L.adapter,'read_remote',return_value=(old,'a'*40)),mock.patch.object(L.adapter,'put_remote') as put:
            with self.assertRaisesRegex(L.Stop,'frozen_abstract_refetch_failed'):R.execute(Path(tmp),result,get=one_missing)
        self.assertEqual(len(result['verified_public_inputs']),19);self.assertEqual(len(result['source_preflight_failures']),1);put.assert_not_called()
    def test_named_recovery_preserves_failed_cost_and_prevents_second_recovery(self):
        p,rows,old,original,doc,get=fixture();plans=L.bound_plans(rows,p)
        state=costs.reserve_recovery(old,'888','d'*40,plans)
        self.assertEqual(state['previous_ledger'],old);self.assertEqual(state['summary']['known_cost_total_usd'],'0.002228700');self.assertEqual(state['schema'],5)
        with self.assertRaises(L.Stop):costs.reserve_recovery(state,'999','e'*40,plans)
        with self.assertRaises(L.Stop):costs.ready(state)
        bad=copy.deepcopy(plans);bad[0]['reserved_nanos']=costs.CAP
        with self.assertRaises(L.Stop):costs.reserve_recovery(old,'888','d'*40,bad)
    def test_unknown_or_different_failed_round_cannot_enable_recovery(self):
        p,rows,old,original,doc,get=fixture()
        for modify in ('run','unknown','cost'):
            bad=copy.deepcopy(old)
            if modify=='run':bad['round']['run_id']='1234'
            elif modify=='unknown':bad['round']['calls'][0]['status']='unknown'
            else:bad['round']['calls'][0]['measured_cost_nanos']+=1
            with self.assertRaises(L.Stop):costs.recovery_ready(bad)
    def test_source_drift_stops_before_any_claim_or_paid_request(self):
        p,rows,old,original,doc,get=fixture();sent=[]
        env=dict(F.ENV,GITHUB_RUN_ID='888',GITHUB_SHA='d'*40,SHADOW_PROTOCOL_V2=L.dumps(doc))
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,env),mock.patch.object(E,'price_guard',return_value={}),mock.patch.object(L.adapter,'read_remote',return_value=(old,'a'*40)),mock.patch.object(L.adapter,'put_remote') as put:
            with self.assertRaisesRegex(L.Stop,'frozen_abstract_refetch_failed'):R.execute(Path(tmp),{},send=lambda *a:sent.append(1),get=lambda url:{'message':{'DOI':'10.4321/changed'}})
        put.assert_not_called();self.assertEqual(sent,[])
    def test_complete_recovery_keeps_original_pairing_and_all_historical_costs(self):
        p,rows,old,original,doc,get=fixture();remote={'state':old,'sha':'a'*40};sent=[]
        def read():return copy.deepcopy(remote['state']),remote['sha']
        def put(state,sha):self.assertEqual(sha,remote['sha']);remote['state']=copy.deepcopy(state)
        def send(endpoint,request,key):
            self.assertEqual(remote['state']['schema'],5);self.assertEqual(remote['state']['previous_ledger'],old);sent.append(endpoint)
            if endpoint.endswith('/responses'):
                paper={'id':json.loads(request['input'])['paper_id']}
                return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':200,'input_tokens_details':{'cached_tokens':0},'output_tokens':200,'output_tokens_details':{'reasoning_tokens':50},'total_tokens':400},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(F.record(paper))}]}]}
            value=F.response();value['usage']={'input_tokens':200,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens':0,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':200};return value
        env=dict(F.ENV,GITHUB_RUN_ID='888',GITHUB_SHA='d'*40,SHADOW_PROTOCOL_V2=L.dumps(doc))
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,env),mock.patch.object(E,'price_guard',return_value={}),mock.patch.object(L.adapter,'read_remote',read),mock.patch.object(L.adapter,'put_remote',put):
            result={};R.execute(Path(tmp),result,send=send,get=get)
        self.assertEqual(len(sent),60);self.assertEqual(result['status'],'completed');self.assertEqual(len(result['direct']),20);self.assertEqual(len(result['augmented']),20)
        self.assertEqual(result['cumulative_budget']['known_cost_total_usd'],'0.005428700');self.assertEqual(result['cumulative_budget']['unknown_or_pending_reserved_usd'],'0.000000000')
        paired=R.attach_original_pairing(result,original)
        self.assertEqual(paired['baseline_scores'],original['baseline_scores']);self.assertEqual(paired['blind_rows'],original['blind_rows']);self.assertEqual(paired['blind_to_scored_id'],original['blind_to_scored_id']);self.assertEqual(paired['candidates'],original['candidates'])
        altered=copy.deepcopy(original);altered['baseline_scores']['T001']+=1
        result['candidates']=doc['recovery']['candidates']
        with self.assertRaisesRegex(L.Stop,'baseline_or_blind_order'):R.attach_original_pairing(result,altered)
    def test_recovery_workflow_uses_same_masked_input_and_encrypted_artifact(self):
        import yaml
        root=L.ROOT.parents[2];caller=yaml.safe_load((root/'.github/workflows/test.yml').read_text());flow=yaml.safe_load((root/'.github/workflows/heldout-encrypted.yml').read_text())
        job=caller['jobs']['fixed-sample-recovery'];self.assertEqual(job['with']['experiment_mode'],'recover');self.assertEqual(job['secrets']['private_protocol'],'${{ vars.SHADOW_PROTOCOL_V2 }}')
        run=next(x for x in flow['jobs']['pilot']['steps'] if 'SHADOW_PROTOCOL_V2' in x.get('env',{}));self.assertIn('comparison/recover.py',run['run']);self.assertEqual(run['env']['SHADOW_PROTOCOL_V2'],'${{ secrets.private_protocol }}')
    def test_encrypted_recovery_delivers_original_blind_order_only_with_original_file(self):
        import csv
        import importlib.util
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        p,rows,old,original,doc,get=fixture()
        result={'status':'completed','protocol_sha256':L.digest(p),'recovery_id':costs.RECOVERY,'recovery_source':doc['recovery'],'candidates':doc['recovery']['candidates']}
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        pub=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo);priv=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        encrypted=L.prior.seal(result,pub,experiment_id=L.ROUND,protocol_sha256=L.CONTEXT_SHA)
        spec=importlib.util.spec_from_file_location('recover_decoder_fixture',L.ROOT/'decrypt.py');decoder=importlib.util.module_from_spec(spec);spec.loader.exec_module(decoder)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(L.Stop,'original_results_required'):decoder.write_deliverables(encrypted,priv,Path(tmp),p)
            summary=decoder.write_deliverables(encrypted,priv,Path(tmp),p,original)
            with Path(summary['blind_pack']).open(encoding='utf-8-sig') as f:table=list(csv.reader(f))
            self.assertEqual(len(table),21);self.assertEqual([row[0] for row in table[1:]],[x['id'] for x in original['blind_rows']])
            self.assertTrue(all(len(row)==6 for row in table));self.assertTrue(all(row[-2:]==['',''] for row in table[1:]))
    def test_failed_preflight_returns_failure_code_and_preserves_encrypted_artifact(self):
        import contextlib
        import io
        p,rows,old,original,doc,get=fixture()
        def blocked(private,result):raise L.Stop('synthetic_fixed_source_drift')
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,{'RUNNER_TEMP':tmp}),mock.patch.object(L.adapter,'read_remote',return_value=(old,'a'*40)),contextlib.redirect_stdout(io.StringIO()):
            code=E.main(execute_fn=blocked,historical_usd='0.002228700')
            self.assertEqual(code,1);self.assertTrue((Path(tmp)/'comparison-public/result.sealed.json').is_file())
            status=json.loads((Path(tmp)/'comparison-public/status.public.json').read_text());self.assertEqual(status['status'],'blocked_before_paid');self.assertEqual(status['reason'],'synthetic_fixed_source_drift')

if __name__=='__main__':unittest.main()
