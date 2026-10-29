"""Offline source/evidence/cumulative-budget tests; never call a provider."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import os
from datetime import datetime,timezone
from html import escape
import contextlib
import io
import budget_v2 as b
import sources_v2 as s
import runner_v2 as r
import runner as v1


def original_claim():
    return {'schema':1,'plan':'openai-shadow-20261029-v1','run_id':b.V1_RUN,
            'source_sha':b.V1_COMMIT,'status':'reserved_outcome_unknown','currency':'USD',
            'limit_usd':'1.000000000','reserved_usd':'1.000000000'}


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.original=original_claim()
        self.state=b.reconcile(self.original,'a'*40,b.verified_receipt())
    def test_reconciliation_preserves_original_and_counts_spend(self):
        self.assertEqual(self.state['history_v1']['original_claim'],self.original)
        self.assertEqual(self.state['summary']['known_cost_total_usd'],'0.000173600')
        self.assertEqual(self.state['summary']['remaining_uncommitted_usd'],'0.999826400')
    def test_receipt_unknown_or_tampered_is_not_settled(self):
        for change in ({'usage_complete':False},{'unknown_usage_attempts':1},{'estimated_cost_nanos':0}):
            with self.assertRaises(b.BudgetStop):b.reconcile(self.original,'a'*40,dict(b.verified_receipt(),**change))
    def test_exactly_one_v2_and_original_cap(self):
        state=b.reserve(self.state,'123','b'*40,'c'*64,20_000_000)
        with self.assertRaises(b.BudgetStop):b.reserve(state,'124','b'*40,'d'*64,1)
        with self.assertRaises(b.BudgetStop):b.reserve(self.state,'123','b'*40,'c'*64,b.CAP-b.V1_COST+1)
        self.assertEqual(self.state['extension']['status'],'authorized_not_started')
    def test_unknown_usage_keeps_entire_bound_and_blocks_reuse(self):
        state=b.reserve(self.state,'123','b'*40,'c'*64,20_000_000)
        done=b.finish(state,'123',{'status':'transport_failure','usage_complete':False})
        self.assertEqual(done['summary']['unknown_or_pending_reserved_usd'],'0.020000000')
        with self.assertRaises(b.BudgetStop):b.reserve(done,'124','b'*40,'d'*64,1)
    def test_measured_cost_is_added_to_v1(self):
        state=b.reserve(self.state,'123','b'*40,'c'*64,20_000_000)
        done=b.finish(state,'123',{'status':'completed','usage_complete':True,'measured_cost_nanos':300_000})
        self.assertEqual(done['summary']['known_cost_total_usd'],'0.000473600')
        self.assertTrue(done['summary']['extension_consumed'])
    def test_wrong_owner_and_cost_overrun_stop(self):
        state=b.reserve(self.state,'123','b'*40,'c'*64,20_000_000)
        with self.assertRaises(b.BudgetStop):b.finish(state,'999',{'usage_complete':False})
        with self.assertRaises(b.BudgetStop):b.finish(state,'123',{'status':'completed','usage_complete':True,'measured_cost_nanos':20_000_001})
        with self.assertRaises(b.BudgetStop):b.finish(state,'123',{'status':'completed','usage_complete':True})
    def test_nature_exact_section_title_and_doi(self):
        body='Synthetic abstract for bounded extraction and identity checks. '*5
        html='<meta name="citation_title" content="Synthetic title"><h1>Synthetic title</h1><p>10.1234/synthetic</p><section id="Abs1-content"><p>'+body+'</p></section>'
        route={'identifier':'10.1234/synthetic','end_pattern':'^Access options'}
        self.assertEqual(s.html_abstract(html,route,'Synthetic title'),s.validated_text(body))
        with self.assertRaises(s.SourceStop):s.html_abstract(html,route,'Wrong title')
        with self.assertRaises(s.SourceStop):s.html_abstract(html,dict(route,identifier='10.1234/wrong'),'Synthetic title')
    def test_secondary_provenance_and_abstract_boundary(self):
        body='Synthetic secondary abstract with enough text for an extraction test. '*5
        html='<h1>Synthetic title</h1><p>10.1234/synthetic</p><h2>Abstract</h2><p>'+body+'</p><h2>No file available</h2><p>not abstract</p>'
        route={'identifier':'10.1234/synthetic','end_pattern':'^No file available'}
        self.assertEqual(s.html_abstract(html,route,'Synthetic title'),s.validated_text(body))
    def test_inline_title_and_abstract_fragments_preserve_dom_text(self):
        body='Synthetic abstract with interfacial water and enough evidence for bounded extraction. '*4
        html='<h1>Solar-operated inter<span>layer</span> electro&shy;thermal evaporator</h1><meta name="citation_doi" content="10.1234/synthetic"><section id="Abs1-content"><p>'+body[:12]+'<wbr>'+body[12:]+'</p></section>'
        route={'identifier':'10.1234/synthetic','end_pattern':'^Access options'}
        self.assertEqual(s.html_abstract(html,route,'Solar-operated interlayer electrothermal evaporator'),s.validated_text(body))
    def test_publisher_document_title_requires_exact_article_and_allowlisted_suffix(self):
        body='Synthetic abstract for exact metadata matching without fuzzy title acceptance. '*4
        route={'identifier':'10.1234/synthetic','end_pattern':'^Access options','publisher_suffixes':['Nature Water']}
        html='<head><title>Synthetic title | Nature Water</title></head><p>10.1234/synthetic</p><section id="Abs1-content">'+body+'</section>'
        self.assertEqual(s.html_abstract(html,route,'Synthetic title'),s.validated_text(body))
        for changed in (html.replace('Nature Water','Another Publisher'),html.replace('Synthetic title |','Another Synthetic title |')):
            with self.assertRaises(s.SourceStop):s.html_abstract(changed,route,'Synthetic title')
        og='<meta property="og:title" content="Synthetic title"><p>10.1234/synthetic</p><section id="Abs1-content">'+body+'</section>'
        self.assertEqual(s.html_abstract(og,route,'Synthetic title'),s.validated_text(body))
    def test_declared_doi_must_match_even_if_expected_doi_is_in_related_references(self):
        body='Synthetic abstract with enough text to test strict identifier validation. '*4
        html='<h1>Synthetic title</h1><meta name="citation_doi" content="10.1234/another"><p>Related DOI 10.1234/synthetic</p><section id="Abs1-content">'+body+'</section>'
        route={'identifier':'10.1234/synthetic','end_pattern':'^Access options'}
        with self.assertRaisesRegex(s.SourceStop,'declared_source_doi_mismatch'):s.html_abstract(html,route,'Synthetic title')
    def test_challenge_stays_blocked_with_bounded_nonbody_diagnostics(self):
        html='<head><title>Access verification</title><link rel="canonical" href="https://example.org/article?session=hidden#fragment"></head><p>CHALLENGE_BODY_MUST_STAY_PRIVATE</p>'
        with self.assertRaises(s.SourceStop) as caught:
            s.html_abstract(html,{'identifier':'10.1234/synthetic'},'Synthetic title')
        self.assertEqual(str(caught.exception),'source_title_mismatch')
        diagnostic=caught.exception.diagnostics
        self.assertEqual(diagnostic['observed_titles'],[{'field':'document_title','value':'Access verification'}])
        self.assertEqual(diagnostic['canonical_urls'],['https://example.org/article'])
        self.assertNotIn('CHALLENGE_BODY_MUST_STAY_PRIVATE',json.dumps(diagnostic))
        self.assertEqual(s.public_url('https://example.org/paper?session=hidden#fragment'),'https://example.org/paper')
    def test_pubmed_identity_and_structured_abstract(self):
        body='Synthetic public abstract with no private data. '*5
        xml='<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>42949622</PMID><Article><ArticleTitle>Synthetic title</ArticleTitle><Abstract><AbstractText>'+body+'</AbstractText></Abstract></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1002/anie.3951000</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'
        self.assertEqual(s.pubmed_abstract(xml,{},'Synthetic title'),s.validated_text(body))
        with self.assertRaises(s.SourceStop):s.pubmed_abstract(xml,{},'Wrong title')
    def test_missing_evidence_is_null_not_low_relevance(self):
        papers=json.loads((r.ROOT/'papers.public.json').read_text())
        config=json.loads((r.ROOT/'sources_v2.public.json').read_text());calls=[]
        for key in ('P267','P508'):config[key].pop('user_provided_abstract',None) # Generic unavailable-source fallback.
        def missing(url):calls.append(url);raise OSError('synthetic offline source')
        _,evidence,ready=s.prepare(papers,config,get=missing)
        self.assertFalse(ready);self.assertEqual(len(calls),6)
        for key in ('P041','P267','P508'):
            self.assertEqual(evidence[key]['evidence_status'],'insufficient_evidence')
            self.assertIsNone(evidence[key]['relevance_score'])
    def test_user_provided_abstracts_are_verbatim_labeled_and_skip_blocked_pages(self):
        papers=json.loads((r.ROOT/'papers.public.json').read_text())
        config=json.loads((r.ROOT/'sources_v2.public.json').read_text());calls=[]
        def missing(url):calls.append(url);raise OSError('synthetic offline source')
        completed,evidence,ready=s.prepare(papers,config,get=missing)
        for key in ('P267','P508'):
            provided=config[key]['user_provided_abstract']
            self.assertEqual(next(p['abstract'] for p in completed if p['id']==key),provided)
            self.assertEqual(evidence[key]['source']['kind'],'user_provided_abstract')
            self.assertTrue(evidence[key]['source']['not_independently_publisher_fetched'])
            self.assertEqual(evidence[key]['evidence_status'],'abstract_available')
            self.assertTrue(all(item['url'] not in calls for item in config[key]['routes']))
            self.assertNotIn(provided,json.dumps(evidence))
        self.assertFalse(ready);self.assertEqual(len(calls),2)
    def test_invalid_user_provided_abstract_does_not_fallback_to_http(self):
        paper={'id':'P508','title':'Synthetic title','abstract':''};calls=[]
        config={'P508':{'user_provided_abstract':'too short','routes':[{'url':'https://example.org'}]}}
        _,evidence,ready=s.prepare([paper],config,get=lambda url:calls.append(url))
        self.assertFalse(ready);self.assertEqual(calls,[])
        self.assertIsNone(evidence['P508']['relevance_score'])
    def test_failed_source_stage_never_calls_model_or_remote_budget(self):
        with tempfile.TemporaryDirectory() as path:
            private=Path(path)/'private';output=Path(path)/'out'
            private.mkdir();output.mkdir()
            old_read=r.read_remote
            def forbidden():raise AssertionError('remote budget must not be touched')
            r.read_remote=forbidden
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    stage=r.source_stage(private,output,get=lambda url:(_ for _ in ()).throw(OSError('offline')))
                self.assertFalse(stage['ready'])
                with contextlib.redirect_stdout(io.StringIO()):result=r.score_stage(private,output)
                self.assertEqual(result['api_requests_this_run'],0)
                self.assertEqual(result['reason'],'full_five_abstracts_required_no_title_only_repeat')
            finally:r.read_remote=old_read
            for file in output.glob('*.json'):
                text=file.read_text()
                self.assertNotIn('Much of the water that matters',text)
                self.assertFalse(file.name.endswith('.request.json'))

    def test_core_sends_only_after_remote_cumulative_reservation(self):
        with tempfile.TemporaryDirectory() as path:
            output=Path(path);core=v1.load_core(output/'private')
            ids=sorted(core.EXPECTED_IDS)
            papers=[{'id':key,'title':'Synthetic title','abstract':'Synthetic abstract'} for key in ids]
            rubric=json.loads((r.ROOT/'rubric.public.json').read_text())
            request=core.payload('decisions',papers,rubric)
            remote={'state':copy.deepcopy(self.state),'sha':'a'*40};calls=[]
            old_read,old_put=r.read_remote,r.put_remote
            old_env={key:os.environ.get(key) for key in ('GITHUB_RUN_ID','GITHUB_SHA')}
            os.environ.update(GITHUB_RUN_ID='123',GITHUB_SHA='b'*40)
            def read():return copy.deepcopy(remote['state']),remote['sha']
            def put(state,sha):
                self.assertEqual(sha,remote['sha']);remote['state']=copy.deepcopy(state);remote['sha']='d'*40
                calls.append('remote_'+state['extension']['status'])
            def send(*args):
                self.assertEqual(remote['state']['extension']['status'],'reserved_outcome_unknown')
                calls.append('model')
                return {'model':core.MODEL,'usage':{'input_tokens':1000,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens':0,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':1000},'answers':[{'type':'score','name':key,'score':1.5,'confidence':0.5,'probabilities':[{'value':i,'label':str(i),'probability':0.25} for i in range(4)]} for key in ids]}
            r.read_remote=read;r.put_remote=put
            try:
                ledger=r.SharedLedger(core,output)
                result=core.run_one('decisions',request,ledger,'synthetic-test-key',send=send)
                self.assertEqual(result['status'],'completed')
                self.assertEqual(calls,['remote_reserved_outcome_unknown','model','remote_completed'])
                self.assertEqual(remote['state']['summary']['known_cost_total_usd'],'0.000273600')
                self.assertEqual(remote['state']['history_v1']['original_claim'],self.original)
            finally:
                r.read_remote=old_read;r.put_remote=old_put
                for key,value in old_env.items():
                    if value is None:os.environ.pop(key,None)
                    else:os.environ[key]=value

    def test_failed_remote_reservation_never_calls_model(self):
        with tempfile.TemporaryDirectory() as path:
            output=Path(path);core=v1.load_core(output/'private')
            papers=[{'id':key,'title':'Synthetic title','abstract':'Synthetic abstract'} for key in sorted(core.EXPECTED_IDS)]
            request=core.payload('decisions',papers,json.loads((r.ROOT/'rubric.public.json').read_text()))
            old_read,old_put=r.read_remote,r.put_remote
            old_env={key:os.environ.get(key) for key in ('GITHUB_RUN_ID','GITHUB_SHA')}
            os.environ.update(GITHUB_RUN_ID='123',GITHUB_SHA='b'*40)
            r.read_remote=lambda:(copy.deepcopy(self.state),'a'*40)
            def denied(*args):raise OSError('synthetic remote failure')
            r.put_remote=denied;sent=[]
            try:
                ledger=r.SharedLedger(core,output)
                with self.assertRaises(OSError):core.run_one('decisions',request,ledger,'synthetic-test-key',send=lambda *args:sent.append(1))
                self.assertEqual(sent,[]);self.assertEqual(ledger.new_attempts,0)
            finally:
                r.read_remote=old_read;r.put_remote=old_put
                for key,value in old_env.items():
                    if value is None:os.environ.pop(key,None)
                    else:os.environ[key]=value

    def test_complete_synthetic_pipeline_keeps_abstracts_out_of_public_artifacts(self):
        with tempfile.TemporaryDirectory() as path:
            base=Path(path);private=base/'private';out=base/'out';fixtures=base/'fixtures'
            private.mkdir();out.mkdir();fixtures.mkdir()
            for name in ('papers.public.json','sources_v2.public.json','rubric.public.json','pricing.public.json','protocol_v2.public.json'):
                data=json.loads((r.ROOT/name).read_text())
                if name=='pricing.public.json':data['verified_on']=datetime.now(timezone.utc).date().isoformat()
                (fixtures/name).write_text(json.dumps(data))
            papers=json.loads((fixtures/'papers.public.json').read_text());lookup={p['id']:p for p in papers}
            marker='SYNTHETIC_FULL_ABSTRACT for complete-source integration testing. '*5
            routes=json.loads((fixtures/'sources_v2.public.json').read_text())
            source_calls=[]
            def fetch(url):
                source_calls.append(url)
                key=next(k for k,v in routes.items() if any(item['url']==url for item in v.get('routes',[])))
                title=escape(lookup[key]['title']);route=routes[key]['routes'][0]
                if route['format']=='pubmed_xml':
                    return '<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>42949622</PMID><Article><ArticleTitle>'+title+'</ArticleTitle><Abstract><AbstractText>'+marker+'</AbstractText></Abstract></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1002/anie.3951000</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'
                return '<h1>'+title+'</h1><p>'+route['identifier']+'</p><section id="Abs1-content"><p>'+marker+'</p></section>'
            old_root,old_load,old_read,old_put=r.ROOT,v1.load_core,r.read_remote,r.put_remote
            old_env={k:os.environ.get(k) for k in ('GITHUB_RUN_ID','GITHUB_SHA','OPENAI_API_KEY','OPENAI_API_KEY_ISSUER','OPENAI_API_BASE','OPENAI_BASE_URL')}
            core=v1.load_core(private);original_run_one=core.run_one
            remote={'state':copy.deepcopy(self.state),'sha':'a'*40};sent=[]
            def send(*args):
                sent.append(1)
                self.assertEqual(remote['state']['extension']['status'],'reserved_outcome_unknown')
                return {'model':core.MODEL,'usage':{'input_tokens':1000,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},'output_tokens':0,'output_tokens_details':{'reasoning_tokens':0},'total_tokens':1000},'answers':[{'type':'score','name':key,'score':1.5,'confidence':0.5,'probabilities':[{'value':i,'label':str(i),'probability':0.25} for i in range(4)]} for key in sorted(core.EXPECTED_IDS)]}
            def run_one(method,request,ledger,key,output=None):return original_run_one(method,request,ledger,key,send=send,output=output)
            def put(state,sha):
                self.assertEqual(sha,remote['sha']);remote['state']=copy.deepcopy(state);remote['sha']='d'*40
            core.run_one=run_one;r.ROOT=fixtures;v1.load_core=lambda work:core
            r.read_remote=lambda:(copy.deepcopy(remote['state']),remote['sha']);r.put_remote=put
            os.environ.update(GITHUB_RUN_ID='123',GITHUB_SHA='b'*40,OPENAI_API_KEY='synthetic-test-key',OPENAI_API_KEY_ISSUER='https://api.openai.com',OPENAI_API_BASE='https://api.openai.com/v1',OPENAI_BASE_URL='')
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    source=r.source_stage(private,out,get=fetch)
                self.assertTrue(source['ready'])
                self.assertEqual(source_calls,[routes['P041']['routes'][0]['url']])
                with contextlib.redirect_stdout(io.StringIO()):result=r.score_stage(private,out)
                self.assertEqual(result['status'],'completed')
                self.assertEqual(len(sent),1)
                self.assertEqual(result['cumulative_budget']['known_cost_total_usd'],'0.000273600')
                self.assertEqual(len(result['scores']),5)
                for file in out.glob('*.json'):
                    self.assertNotIn('SYNTHETIC_FULL_ABSTRACT',file.read_text())
                    self.assertNotIn('Much of the water that matters',file.read_text())
                    self.assertNotIn('synthetic-test-key',file.read_text())
                    self.assertNotIn('Electrocatalysis is central to electrified energy conversion technologies',file.read_text())
                    self.assertNotIn('As conflicts and frequent natural disasters intensify',file.read_text())
                self.assertFalse(any(out.glob('*.request.json')))
            finally:
                r.ROOT=old_root;v1.load_core=old_load;r.read_remote=old_read;r.put_remote=old_put
                for key,value in old_env.items():
                    if value is None:os.environ.pop(key,None)
                    else:os.environ[key]=value


if __name__=='__main__':unittest.main()
