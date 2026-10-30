"""Offline synthetic fixtures only. No real criteria, prior labels or corpus."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import logic as L
import costs
import experiment as E

def protocol():
    p={'id':L.ROUND,'schema':2,'criterion':{'target_domain':'SYNTHETIC_DOMAIN','paths':{'C':'synthetic direct path','M':'synthetic transfer path','E':'synthetic observation path'},'path_policy':'equal, multiple allowed','value_levels':['none','peripheral','useful','strong'],'interest_levels':['none','adjacent','substantive'],'interest_directions':[{'id':f'D{i}','name':f'Synthetic direction {i}'} for i in range(1,6)],'evidence_rule':'Insufficient evidence abstains.'},'exclude_studies':[{'title':f'EXCLUDED_PRIOR_TITLE_{i}','identifiers':[f'10.4321/seen{i}']} for i in range(29)],'sampling':{'target_count':24,'balanced':True},'execution':{'sample_sizes':list(L.SIZES),'category_terms':{'C':['simulation'],'M':['learning'],'E':['measurement']}}}
    p['criterion_sha256']=L.digest(p['criterion']);p['sampling_sha256']=L.digest({'sampling':p['sampling'],'category_terms':p['execution']['category_terms']});return p

def rows(n=96):
    return [{'id':f'T{i+1:03d}','canonical_id':f'10.4321/new{i:03d}','title':f'UniqueTopic{i} IndependentStudy{i}',
             'abstract':('Synthetic simulation learning and measurement evidence with sufficient public study facts. ')*5,
             'source':'journals' if i%3==0 else 'arxiv' if i%3==1 else 'chemrxiv','group':'journals' if i%3==0 else 'preprints',
             'direction':i%6,'keyword_cosine':i/200,'baseline_score':float(i),'link':f'https://doi.org/10.4321/new{i:03d}',
             'abstract_provenance':{'source':'synthetic'},'sampling_path_indices':['C','M','E']} for i in range(n)]

def response(value=2,interest=1,paths=1,directions=1,evidence='enough'):
    def score(name,grade,n):return {'type':'score','name':name,'score':float(grade),'probabilities':[{'value':i,'label':str(i),'probability':float(i==grade)} for i in range(n)],'confidence':1.0}
    def choice(name,selected,allowed):return {'type':'choice','name':name,'choice':selected,'probabilities':[{'value':x,'probability':float(x==selected)} for x in allowed],'confidence':1.0}
    return {'model':'gpt-6-luna','answers':[choice('evidence',evidence,['enough','insufficient']),score('value',value,4),score('interest',interest,3),choice('paths',str(paths),[str(i) for i in range(8)]),choice('directions',str(directions),[str(i) for i in range(32)])]}

def record(paper):
    return {'paper_id':paper['id'],'facts':{k:{'text':'A synthetic supported paraphrase.','sentences':[1]} for k in ('object','method','finding')},'hypothesis':{'use_or_question':'A clearly proposed synthetic use.','approach_and_prerequisites':'A condition requiring validation.','unknowns':['Transferability is not measured.']}}

def prior_ledger():
    spec=importlib.util.spec_from_file_location('synthetic_prior_fixture',L.ROOT.parent/'heldout/test_heldout.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    state=module.history();b=L.prior_budget
    plans=[{'request_sha256':str(i)*64,'reserved_nanos':1_000_000} for i in range(6)]
    state=b.reserve(state,'40886358502','046b062e869092044ed7b6f2ebbdbefd62d9bea1',plans)
    for i,tokens in enumerate((2101,2273,2071,2138,2298,2179)):
        state=b.start(state,'40886358502',i,plans[i]['request_sha256']);state=b.finish_call(state,'40886358502',i,{'status':'completed','usage_complete':True,'measured_cost_nanos':tokens*100,'input_tokens':tokens})
    return b.close(state,'40886358502',True)

ENV={'GITHUB_REPOSITORY':'wankiwi/zot2dailypaper','GITHUB_REF':'refs/heads/experiment/openai-shadow-20261029','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_RUN_ATTEMPT':'1','GITHUB_WORKFLOW':'Test','GITHUB_RUN_ID':'777','GITHUB_SHA':'c'*40,'OPENAI_API_KEY':'synthetic_key','OPENAI_API_KEY_ISSUER':'https://api.openai.com','OPENAI_API_BASE':'https://api.openai.com/v1','OPENAI_BASE_URL':''}

class Tests(unittest.TestCase):
    def test_protocol_and_sampling_are_fixed_without_old_score_threshold(self):
        p=L.validate_protocol(protocol());a,meta=L.sample(rows(),p,24)
        changed=[dict(x,baseline_score=100-x['baseline_score'],keyword_cosine=1-x['keyword_cosine']) for x in rows()]
        b,_=L.sample(changed,p,24)
        self.assertEqual([x['canonical_id'] for x in a],[x['canonical_id'] for x in b]);self.assertEqual(len({x['canonical_id'] for x in a}),24)
        self.assertEqual(set(meta['strata'].values()),{'uniform','C','M','E'})
    def test_prior_29_ids_versions_and_near_titles_are_excluded(self):
        p=protocol();items=rows(3);items[0]['canonical_id']='10.4321/seen3';items[1]['title']=p['exclude_studies'][4]['title'];items[2]['abstract']=''
        scope={'eligibility':{'exclude_development_dois':[s for x in p['exclude_studies'] for s in x['identifiers']],'exclude_development_arxiv':[]}}
        kept,excluded=L.prior.eligible(items,scope,[{'title':x['title']} for x in p['exclude_studies']]);self.assertEqual(kept,[]);self.assertEqual(len(excluded),3)
        self.assertEqual(L.prior.canonical('https://arxiv.org/abs/2610.12345v7'),'arxiv:2610.12345')
    def test_research_value_is_the_gate_and_interest_prioritizes_qualified(self):
        p=protocol();out={'A':L.parse_decision(response(3,0,directions=0),p),'B':L.parse_decision(response(2,2),p),'C':L.parse_decision(response(1,2),p)}
        self.assertEqual(L.rank(out)['order'],['B','A','C']);self.assertTrue(out['A']['qualified'])
    def test_missing_or_inconsistent_evidence_abstains_not_zero(self):
        p=protocol()
        for args in ({'evidence':'insufficient'},{'paths':0},{'interest':2,'directions':0}):
            out=L.parse_decision(response(**args),p);self.assertEqual(out['status'],'abstain');self.assertIsNone(out['value']);self.assertEqual(L.rank({'X':out})['order'],[])
        self.assertEqual(L.parse_decision(response(0,0,paths=0,directions=0),p)['status'],'judged')
    def test_multiselect_paths_and_directions_and_explicit_order(self):
        p=protocol();r=L.parse_decision(response(paths=5,directions=17),p)
        self.assertEqual(r['path_indices'],[0,2]);self.assertEqual(r['interest_direction_indices'],[0,4])
        req=L.decision_request(rows(1)[0],p);self.assertEqual(json.loads(req['input'])['criterion']['path_order'],['C','M','E'])
    def test_extraction_facts_hypotheses_separate_and_final_scores_forbidden(self):
        paper=rows(1)[0];r=record(paper)
        wrap=lambda value:{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}
        self.assertEqual(L.parse_record(wrap(r),paper),r)
        bad=copy.deepcopy(r);bad['score']=3
        with self.assertRaises(L.Stop):L.parse_record(wrap(bad),paper)
        bad=copy.deepcopy(r);bad['facts']['method']['sentences']=[9999]
        with self.assertRaises(L.Stop):L.parse_record(wrap(bad),paper)
        bad=copy.deepcopy(r);bad['facts']['method']['text']=None
        with self.assertRaisesRegex(L.Stop,'missing_fact_cannot_claim_evidence'):L.parse_record(wrap(bad),paper)
    def test_same_paper_and_rules_only_record_differs_between_arms(self):
        p=protocol();paper=rows(1)[0];paper['corpus']='PRIVATE_ZOTERO_SENTINEL';paper['label']='HUMAN_LABEL_SENTINEL'
        direct=L.decision_request(paper,p);enh=L.decision_request(paper,p,record(paper));a=json.loads(direct['input']);b=json.loads(enh['input'])
        self.assertEqual(a['paper'],b['paper']);self.assertEqual(a['criterion'],b['criterion']);self.assertNotIn('extraction_record',a)
        for request in (direct,enh,L.extract_request(paper,p)):
            text=L.dumps(request);self.assertNotIn('PRIVATE_ZOTERO_SENTINEL',text);self.assertNotIn('HUMAN_LABEL_SENTINEL',text);self.assertNotIn('EXCLUDED_PRIOR_TITLE',text)
    def test_response_cache_and_reasoning_cost_without_double_counting(self):
        r={'usage':{'input_tokens':1000,'input_tokens_details':{'cached_tokens':100,'cache_write_tokens':50},'output_tokens':200,'output_tokens_details':{'reasoning_tokens':50},'total_tokens':1200}}
        cost=E.usage('llm',r);self.assertTrue(cost['usage_complete']);self.assertEqual(cost['measured_cost_nanos'],192250)
        r['usage']['input_tokens_details'].pop('cache_write_tokens');cost=E.usage('llm',r);self.assertIn('cache_write_tokens_basis',cost)
    def test_old_history_is_preserved_and_cap_cannot_reset(self):
        p=protocol();planned=L.bound_plans(rows(24),p);old=prior_ledger();state=costs.reserve(old,'777','c'*40,24,planned)
        self.assertEqual(state['previous_ledger'],old);self.assertEqual(state['summary']['known_cost_total_usd'],'0.001743000')
        with self.assertRaises(L.Stop):costs.ready(state)
        over=copy.deepcopy(planned);over[0]['reserved_nanos']=costs.CAP
        with self.assertRaises(L.Stop):costs.reserve(old,'777','c'*40,24,over)
    def test_unknown_usage_blocks_later_calls_and_releases_unsent_only(self):
        p=protocol();planned=L.bound_plans(rows(24),p);s=costs.reserve(prior_ledger(),'777','c'*40,24,planned)
        s=costs.start(s,'777',0,'llm',planned[0]['reserved_nanos']);s=costs.finish(s,'777',0,{'status':'timeout','usage_complete':False})
        with self.assertRaises(L.Stop):costs.start(s,'777',1,'llm',planned[1]['reserved_nanos'])
        s=costs.close(s,'777',False);self.assertEqual(s['summary']['unknown_or_pending_reserved_usd'],costs.usd(planned[0]['reserved_nanos']))
    def test_new_crypto_context_roundtrip_preserves_receiver(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        pub=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        priv=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        sealed=L.prior.seal({'private':'SENSITIVE_RANK_SENTINEL'},pub,experiment_id=L.ROUND,protocol_sha256=L.CONTEXT_SHA)
        self.assertNotIn('SENSITIVE_RANK_SENTINEL',L.dumps(sealed));self.assertEqual(L.prior.unseal(sealed,priv,expected_experiment=L.ROUND,expected_protocol_sha256=L.CONTEXT_SHA)['private'],'SENSITIVE_RANK_SENTINEL')
        with self.assertRaises(L.Stop):L.prior.unseal(sealed,priv)
    def test_complete_synthetic_three_arm_run_and_public_accounting_redaction(self):
        p=protocol();remote={'state':prior_ledger(),'sha':'a'*40};sent=[]
        def read():return copy.deepcopy(remote['state']),remote['sha']
        def put(state,sha):self.assertEqual(sha,remote['sha']);remote['state']=copy.deepcopy(state)
        def retrieve(executor,corpus,profile,plan,development,report,selector):
            self.assertEqual(corpus,['PRIVATE_ZOTERO_SENTINEL']);selected,meta=selector(rows());return selected,{'selection':meta,'eligible_count':96}
        def transport(endpoint,request,key):
            self.assertEqual(remote['state']['round']['status'],'reserved');sent.append(endpoint)
            if endpoint.endswith('/responses'):
                doc=json.loads(request['input']);item={'id':doc['paper_id']};value=record(item)
                return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':200,'input_tokens_details':{'cached_tokens':0},'output_tokens':200,'output_tokens_details':{'reasoning_tokens':50},'total_tokens':400},'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}
            out=response();out['usage']={'input_tokens':200,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens':0,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':200};return out
        env=dict(ENV,SHADOW_PROTOCOL_V2=json.dumps(p))
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,env),mock.patch.object(E,'price_guard',return_value={'synthetic':True}),mock.patch.object(L.adapter,'read_remote',read),mock.patch.object(L.adapter,'put_remote',put),mock.patch.object(L.adapter,'load_baseline',return_value=(object(),['PRIVATE_ZOTERO_SENTINEL'],object(),{'profile_settings_sha256':'f'*64})),mock.patch.object(L.adapter,'retrieve_and_rank',side_effect=retrieve):
            result={'phase':'preflight','status':'blocked_before_paid'};E.execute(Path(tmp),result,send=transport)
        n=len(result['candidates']);self.assertEqual(n,24);self.assertEqual(len(sent),72);self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['direct']),n);self.assertEqual(len(result['augmented']),n)
        self.assertEqual(result['rankings']['direct']['order'],result['rankings']['augmented']['order'])
        self.assertEqual(result['cumulative_budget']['known_cost_total_usd'],'0.005583000')
        self.assertNotIn('PRIVATE_ZOTERO_SENTINEL',L.dumps(result));self.assertNotIn('synthetic_key',L.dumps(result))
        public=L.dumps(remote['state']['round']);self.assertNotIn('SYNTHETIC_DOMAIN',public);self.assertNotIn('T001',public);self.assertNotIn('path_indices',public)
        self.assertTrue(all('baseline_direction_index' in x and 'sampling_path_indices' in x for x in result['candidates']))
        self.assertTrue(all(set(x)=={'id','title','link','abstract_excerpt','relevance','reason'} for x in result['blind_rows']))
    def test_failed_remote_reservation_sends_nothing(self):
        p=protocol();paper=rows(1)[0];plans=L.bound_plans(rows(24),p);s=costs.reserve(prior_ledger(),'777','c'*40,24,plans);sent=[]
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,ENV),mock.patch.object(L.adapter,'read_remote',return_value=(s,'a'*40)),mock.patch.object(L.adapter,'put_remote',side_effect=OSError('synthetic CAS rejection')):
            with self.assertRaises(OSError):E.call(0,plans[0],L.extract_request(paper,p),paper,p,Path(tmp),{},send=lambda *args:sent.append(1))
        self.assertEqual(sent,[])
    def test_blind_export_checks_private_protocol_mapping_and_field_allowlist(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import csv
        spec=importlib.util.spec_from_file_location('comparison_decoder_test',L.ROOT/'decrypt.py');decoder=importlib.util.module_from_spec(spec);spec.loader.exec_module(decoder)
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        pub=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        priv=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
        p=protocol();papers=rows(12);blind,mapping=L.prior.blind_rows(papers)
        result={'status':'completed','protocol_sha256':L.digest(p),'candidates':papers,'blind_rows':blind,'blind_to_scored_id':mapping}
        sealed=L.prior.seal(result,pub,experiment_id=L.ROUND,protocol_sha256=L.CONTEXT_SHA)
        with tempfile.TemporaryDirectory() as tmp:
            summary=decoder.write_deliverables(sealed,priv,Path(tmp),p)
            with Path(summary['blind_pack']).open(encoding='utf-8-sig') as f:table=list(csv.reader(f))
            self.assertEqual(len(table),13);self.assertEqual(len(table[0]),6)
            changed=copy.deepcopy(p);changed['sampling']['target_count']=20;changed['sampling_sha256']=L.digest({'sampling':changed['sampling'],'category_terms':changed['execution']['category_terms']})
            with self.assertRaisesRegex(L.Stop,'private_protocol_mismatch'):decoder.write_deliverables(sealed,priv,Path(tmp),changed)
            result['blind_rows'][0]['score']=3
            sealed=L.prior.seal(result,pub,experiment_id=L.ROUND,protocol_sha256=L.CONTEXT_SHA)
            with self.assertRaisesRegex(L.Stop,'non_neutral_fields'):decoder.write_deliverables(sealed,priv,Path(tmp),p)
    def test_workflow_masks_private_inputs_and_exports_only_sealed_results(self):
        import yaml
        root=L.ROOT.parents[2];caller=yaml.safe_load((root/'.github/workflows/test.yml').read_text());flow=yaml.safe_load((root/'.github/workflows/heldout-encrypted.yml').read_text())
        job=caller['jobs']['paired-comparison'];self.assertEqual(job['with']['experiment_mode'],'compare')
        self.assertEqual(job['secrets']['private_protocol'],'${{ vars.SHADOW_PROTOCOL_V2 }}')
        steps=flow['jobs']['pilot']['steps'];run=next(x for x in steps if 'SHADOW_PROTOCOL_V2' in x.get('env',{}))
        self.assertEqual(run['env']['SHADOW_PROTOCOL_V2'],'${{ secrets.private_protocol }}');self.assertEqual(run['env']['CUSTOM_CONFIG'],'${{ secrets.private_configuration }}')
        self.assertNotIn('vars.',L.dumps(run));self.assertFalse(any('MAIL' in key or 'SMTP' in key for key in run['env']))
        artifact=next(x for x in steps if x.get('uses','').startswith('actions/upload-artifact@'))
        paths=artifact['with']['path'].splitlines();self.assertEqual(len(paths),4)
        self.assertTrue(all(x.endswith(('result.sealed.json','status.public.json')) and '-public/' in x for x in paths))

if __name__=='__main__':unittest.main()
