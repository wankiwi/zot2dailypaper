"""One named recovery of a settled format failure, with the exact frozen papers."""
import json
import os
import sys
import costs
import experiment as E
import logic as L
import recovery_sources as sources

SNAPSHOT_KEYS={'schema','id','source_run_id','source_commit','artifact_sha256','ledger_sha256','protocol_sha256','candidates','baseline_reference','candidate_snapshot_sha256','blind_order_sha256'}
CANDIDATE_KEYS=('id','canonical_id','title','link','source','group','abstract_provenance','abstract_sha256','abstract_bytes')

def project_candidates(rows):return [{k:x[k] for k in CANDIDATE_KEYS} for x in rows]

def validate_input(doc):
    if set(doc)!={'protocol','recovery'}:raise L.Stop('recovery_private_input_wrapper_invalid')
    p=L.validate_protocol(doc['protocol']);s=doc['recovery']
    if set(s)-{'verified_public_abstracts'}!=SNAPSHOT_KEYS or s['schema']!=1 or s['id']!=costs.RECOVERY or s['source_run_id']!=costs.FAILED_RUN or s['source_commit']!='ade4e223ba02c46dee9aa4d64a3d1de480a5f2a8' or s['protocol_sha256']!=L.digest(p):raise L.Stop('recovery_snapshot_identity_changed')
    rows=s['candidates']
    if len(rows)!=20 or any(set(x)!=set(CANDIDATE_KEYS) for x in rows) or [x['id'] for x in rows]!=[f'T{i:03d}' for i in range(1,21)] or len({x['canonical_id'] for x in rows})!=20 or L.digest(rows)!=s['candidate_snapshot_sha256']:raise L.Stop('recovery_fixed_candidates_changed')
    if any(not 150<=x['abstract_bytes']<=12000 for x in rows):raise L.Stop('recovery_full_abstract_evidence_missing')
    if s['baseline_reference'].get('source_run_id')!=costs.FAILED_RUN or not s['baseline_reference'].get('snapshot_sha256'):raise L.Stop('original_real_baseline_reference_missing')
    supplied=s.get('verified_public_abstracts',{})
    if not isinstance(supplied,dict) or set(supplied)-set(x['id'] for x in rows):raise L.Stop('supplied_abstract_outside_fixed_candidates')
    for row in rows:
        if row['id'] in supplied:sources.fixed_input(row,supplied[row['id']])
    return p,s

def execute(private,result,send=None,get=sources.fetch):
    L.adapter.previous_runner.assert_job(os.environ)
    state,_=L.adapter.read_remote();costs.recovery_ready(state);E.price_guard()
    p,s=validate_input(json.loads(os.environ.get('SHADOW_PROTOCOL_V2','{}')))
    if L.digest(state)!=s['ledger_sha256']:raise L.Stop('settled_ledger_differs_from_verified_recovery_input')
    L.core().credential(os.environ)
    result.update(protocol_sha256=L.digest(p),private_protocol=p,recovery_id=costs.RECOVERY,recovery_source=s,baseline=s['baseline_reference'],candidates=s['candidates'],phase='fixed_sources_preflight',source_preflight_failures=[])
    rows,failures=sources.resolve_all(s['candidates'],get,s['verified_public_abstracts'] if 'verified_public_abstracts' in s else {})
    result['source_preflight_failures']=failures
    # Public source text is retained only inside the encrypted result. It can
    # then be reused after local verification rather than fetched repeatedly.
    result['verified_public_inputs']=[{k:x[k] for k in ('id','title','abstract','abstract_sha256','recovery_source_url')} for x in rows]
    if failures or len(rows)!=20:raise L.Stop('frozen_abstract_refetch_failed_or_drifted_before_paid')
    result['source_preflight']={'matched_count':20,'all_original_title_and_abstract_hashes_match':True,'retrievals':[{'paper_id':x['id'],'url':x['recovery_source_url'],'abstract_sha256':L.prior.text_sha(x['abstract'])} for x in rows]}
    plans=L.bound_plans(rows,p)
    if sum(x['reserved_nanos'] for x in plans)>costs.CAP-costs.RECOVERY_HISTORY:raise L.Stop('fixed_20_recovery_does_not_fit_original_remaining_budget')
    result.update(phase='frozen_before_paid',request_plans=plans)
    L.adapter.write(private/'recovery_before_paid.json',result)
    E.price_guard();state,sha=L.adapter.read_remote()
    if L.digest(state)!=s['ledger_sha256']:raise L.Stop('recovery_ledger_changed_before_paid')
    L.adapter.put_remote(costs.reserve_recovery(state,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'],plans),sha)
    result.update(phase='paid_comparison',status='in_progress',extraction_records={},direct={},augmented={});byid={x['id']:x for x in rows};success=False
    try:
        for index,plan in enumerate(plans):
            paper=byid[plan['paper_id']];kind=plan['kind']
            request=L.extract_request(paper,p) if kind=='extract' else L.decision_request(paper,p,result['extraction_records'][paper['id']] if kind=='augmented' else None)
            parsed=E.call(index,plan,request,paper,p,private,result,send=send)
            if kind=='extract':result['extraction_records'][paper['id']]=parsed
            else:result[kind][paper['id']]=parsed
        result['rankings']={name:L.rank(result[name]) for name in ('direct','augmented')};result.update(status='completed',phase='completed');success=True
    finally:
        state,sha=L.adapter.read_remote();closed=costs.close(state,os.environ['GITHUB_RUN_ID'],success);L.adapter.put_remote(closed,sha);result['cumulative_budget']=closed['summary']

def attach_original_pairing(result,original):
    s=result['recovery_source']
    if original.get('run_id')!=costs.FAILED_RUN or original.get('protocol_sha256')!=result['protocol_sha256'] or project_candidates(original['candidates'])!=s['candidates'] or result['candidates']!=s['candidates']:raise L.Stop('original_fixed_candidate_pairing_mismatch')
    if L.digest(original['baseline'])!=s['baseline_reference']['snapshot_sha256'] or L.digest({'baseline_scores':original['baseline_scores'],'baseline_order':original['baseline_order']})!=s['baseline_reference']['scores_sha256'] or L.digest({'blind_rows':original['blind_rows'],'blind_to_scored_id':original['blind_to_scored_id']})!=s['blind_order_sha256']:raise L.Stop('original_baseline_or_blind_order_mismatch')
    expected=set(x['id'] for x in original['candidates'])
    if set(original['baseline_scores'])!=expected or set(original['baseline_order'])!=expected:raise L.Stop('original_actual_baseline_incomplete')
    for name in ('baseline','baseline_scores','baseline_order','blind_rows','blind_to_scored_id','pool','candidates'):result[name]=original[name]
    result['prior_paid_failed_receipts']=original['model_receipts']
    result['pairing_restored_from_original_run']=costs.FAILED_RUN
    return result

if __name__=='__main__':sys.exit(E.main(execute_fn=execute,historical_usd=costs.usd(costs.RECOVERY_HISTORY)))
