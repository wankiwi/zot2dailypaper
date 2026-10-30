"""Manual comparison. Reuses the verified production adapter, crypto and allowance."""
from datetime import datetime,timezone
import copy
import json
import os
from pathlib import Path
import sys

import logic as L
import costs

def price_guard():
    p=json.loads((L.ROOT/'pricing.public.json').read_text())
    if p['version']!=L.PRICE_VERSION or p['verified_on']!=datetime.now(timezone.utc).date().isoformat():raise L.Stop('current_day_price_verification_required')
    return p

def usage(method,response):
    raw=copy.deepcopy(response.get('usage'));basis=None
    # Explicit caching with no breakpoints disables cache writes by contract.
    # Standard Responses may omit this counter; preserve the inference in accounting.
    if isinstance(raw,dict) and isinstance(raw.get('input_tokens_details'),dict) and 'cache_write_tokens' not in raw['input_tokens_details']:
        raw['input_tokens_details']['cache_write_tokens']=0;basis='request_disables_caching_explicit_no_breakpoints'
    measured=L.core().usage_cost(method,raw)
    if basis:measured['cache_write_tokens_basis']=basis
    return measured

def call(index,plan,request,paper,protocol,private,result,send=None):
    c=L.core();method=plan['method'];amount=c.reserve_nanos(method,request)
    state,sha=L.adapter.read_remote();proposed=costs.start(state,os.environ['GITHUB_RUN_ID'],index,method,amount)
    inner=c.Ledger(private/f'call_{index:03d}.json');inner.reserve(method,request)
    L.adapter.put_remote(proposed,sha) # Confirmed CAS before crossing the paid boundary.
    fields={};parsed=None;response=None
    try:
        result['api_requests_started']=result.get('api_requests_started',0)+1
        response=(send or c.transport)(c.ENDPOINTS[method],request,c.credential(os.environ))
        if not isinstance(response,dict):raise L.Stop('response_not_an_object')
    except Exception as error:
        fields={'status':'transport_failure_usage_unknown','usage_complete':False,'error_class':type(error).__name__}
    if response is not None:
        fields=usage(method,response);fields.update(model_returned=response.get('model'),response_sha256=L.digest(c.sanitized_response(response,c.credential(os.environ))))
        try:
            if not c.valid_model(response.get('model')):raise L.Stop('unexpected_model')
            if method=='llm':parsed=L.parse_record(response,paper)
            else:parsed=L.parse_decision(response,protocol)
            if not fields['usage_complete']:fields['status']='usage_unknown_stop'
            elif fields['input_tokens']>c.input_bound(method,request) or (method=='llm' and fields['output_tokens']>c.MAX_OUTPUT_TOKENS):fields['status']='usage_bound_exceeded'
            else:fields['status']='completed'
        except Exception as error:
            fields['status']='parse_failure';fields['error_class']=type(error).__name__
    receipt=inner.finish(method,fields)
    result.setdefault('model_receipts',[]).append(dict(receipt,kind=plan['kind'],paper_id=paper['id']))
    state,sha=L.adapter.read_remote();updated=costs.finish(state,os.environ['GITHUB_RUN_ID'],index,receipt);L.adapter.put_remote(updated,sha)
    if method=='decisions' and response is not None:result.setdefault('sanitized_decision_responses',[]).append({'kind':plan['kind'],'paper_id':paper['id'],'response':c.sanitized_response(response,c.credential(os.environ))})
    if receipt.get('status')!='completed':raise L.Stop('paid_request_failed_no_retry')
    return parsed

def execute(private,result,send=None):
    L.adapter.previous_runner.assert_job(os.environ)
    state,_=L.adapter.read_remote();costs.ready(state);price_guard()
    if not os.environ.get('SHADOW_PROTOCOL_V2'):raise L.Stop('experiment_private_protocol_variable_missing')
    protocol=L.validate_protocol(json.loads(os.environ['SHADOW_PROTOCOL_V2']))
    result.update(protocol_sha256=L.digest(protocol),private_protocol=protocol)
    L.core().credential(os.environ)
    result['phase']='baseline_and_source_preflight'
    executor,corpus,profile,baseline=L.adapter.load_baseline(private);result['baseline']=baseline;result['pool']={}
    exclusions=protocol['exclude_studies']
    filter_plan={'eligibility':{'exclude_development_dois':[s for x in exclusions for s in x['identifiers']],'exclude_development_arxiv':[]}}
    captured={}
    def selector(pool):
        captured['pool']=pool
        return L.sample(pool,protocol,24)
    first,pool=L.adapter.retrieve_and_rank(executor,corpus,profile,filter_plan,[{'title':x['title']} for x in exclusions],result['pool'],selector=selector)
    result['pool']=pool;chosen=None;rejections=[]
    for n in L.SIZES:
        rows,selection=L.sample(captured['pool'],protocol,n);rows=sorted(rows,key=lambda x:x['canonical_id'])
        for i,x in enumerate(rows,1):x['id']=f'T{i:03d}'
        try:
            plans=L.bound_plans(rows,protocol)
            if sum(x['reserved_nanos'] for x in plans)>costs.CAP-costs.HISTORY:raise L.Stop('worst_case_exceeds_remaining_original_allowance')
            chosen=(rows,selection,plans);break
        except Exception as error:rejections.append({'size':n,'reason':type(error).__name__})
    if chosen is None:raise L.Stop('no_predeclared_sample_size_fits_original_budget')
    rows,selection,plans=chosen;n=len(rows);result['budget_scale_rejections']=rejections
    result['pool'].update(selection=selection,candidate_ids=[x['canonical_id'] for x in rows],scored_snapshot_sha256=L.digest([{k:x[k] for k in ('id','title','abstract')} for x in rows]),freeze_time_utc=datetime.now(timezone.utc).isoformat())
    result['candidates']=[{k:x[k] for k in ('id','canonical_id','title','link','source','group','abstract_provenance','sampling_path_indices')}|{'abstract_sha256':L.prior.text_sha(x['abstract']),'abstract_bytes':len(x['abstract'].encode()),'baseline_direction_index':x['direction']} for x in rows]
    result['baseline_scores']={x['id']:x['baseline_score'] for x in rows};result['baseline_order']=sorted(result['baseline_scores'],key=lambda k:(-result['baseline_scores'][k],k))
    result['blind_rows'],result['blind_to_scored_id']=L.prior.blind_rows(rows)
    result['phase']='frozen_before_paid';result['request_plans']=plans
    L.adapter.write(private/'snapshot_before_paid.json',result)
    price_guard();state,sha=L.adapter.read_remote();claimed=costs.reserve(state,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'],n,plans);L.adapter.put_remote(claimed,sha)
    result.update(phase='paid_comparison',status='in_progress',extraction_records={},direct={},augmented={});byid={x['id']:x for x in rows};success=False
    try:
        for index,plan in enumerate(plans):
            paper=byid[plan['paper_id']];kind=plan['kind']
            if kind=='extract':request=L.extract_request(paper,protocol)
            else:request=L.decision_request(paper,protocol,result['extraction_records'][paper['id']] if kind=='augmented' else None)
            parsed=call(index,plan,request,paper,protocol,private,result,send=send)
            if kind=='extract':result['extraction_records'][paper['id']]=parsed
            else:result[kind][paper['id']]=parsed
        result['rankings']={name:L.rank(result[name]) for name in ('direct','augmented')}
        result.update(status='completed',phase='completed');success=True
    finally:
        state,sha=L.adapter.read_remote();closed=costs.close(state,os.environ['GITHUB_RUN_ID'],success);L.adapter.put_remote(closed,sha);result['cumulative_budget']=closed['summary']

def main():
    temp=Path(os.environ['RUNNER_TEMP']);private=temp/'comparison-private';public=temp/'comparison-public'
    private.mkdir(mode=0o700,exist_ok=True);public.mkdir(mode=0o700,exist_ok=True)
    result={'experiment':L.ROUND,'run_id':os.environ.get('GITHUB_RUN_ID'),'source_commit':os.environ.get('GITHUB_SHA'),'phase':'preflight','status':'blocked_before_paid','human_labels_received':False}
    safe={'experiment':L.ROUND,'historical_estimated_usd':'0.001743000'}
    try:execute(private,result)
    except Exception as error:
        if result['phase']=='paid_comparison':result['status']='failed_after_reservation'
        result['error_class']=type(error).__name__
        if isinstance(error,L.Stop) or (type(error).__name__=='Blocked' and type(error).__module__.startswith('heldout_client_')):result['reason']=str(error)
    finally:
        try:
            state,_=L.adapter.read_remote();result['cumulative_budget']=costs.summary(state) if state.get('schema')==4 else state['summary']
        except Exception:result['accounting_readable']=False
        recipient=(L.ROOT.parent/'heldout/recipient.public.pem').read_bytes()
        L.adapter.write(public/'result.sealed.json',L.prior.seal(result,recipient,experiment_id=L.ROUND,protocol_sha256=L.CONTEXT_SHA))
        safe.update({k:result.get(k) for k in ('status','phase','error_class','reason','cumulative_budget','api_requests_started')})
        safe.update(sample_count=len(result.get('candidates',[])),baseline_ready='baseline' in result)
        L.adapter.write(public/'status.public.json',safe)
        print(L.dumps(safe))

if __name__=='__main__':main()
