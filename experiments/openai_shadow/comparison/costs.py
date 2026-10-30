"""One authorized comparison slot in the original allowance; no reset or retry."""
import copy
import re
from logic import prior_budget,ROUND,Stop,SIZES

CAP=1_000_000_000
HISTORY=1_743_000
usd=prior_budget.usd

def history(state):
    prior_budget.validate(state)
    p=state['pilot'];calls=p.get('calls',[])
    if p.get('status')!='completed' or p.get('run_id')!='40886358502' or p.get('source_sha')!='046b062e869092044ed7b6f2ebbdbefd62d9bea1' or len(calls)!=6 or any(x['status']!='complete' or x.get('receipt',{}).get('status')!='completed' for x in calls):raise Stop('prior_heldout_round_not_settled')
    if sum(x['measured_cost_nanos'] for x in calls)!=1_306_000 or prior_budget.summary(state)['known_cost_total_usd']!='0.001743000' or prior_budget.summary(state)['unknown_or_pending_reserved_usd']!='0.000000000':raise Stop('historical_cost_evidence_changed')
    return state

def ready(state):
    if state.get('schema')==3:history(state);return
    validate(state)
    raise Stop('comparison_slot_already_consumed_no_automatic_retry')

def validate(state):
    try:
        if state['schema']!=4 or state['currency']!='USD' or state['limit_nanos']!=CAP or state['round']['id']!=ROUND:raise Stop('shared_allowance_or_round_identity_changed')
        history(state['previous_ledger']);r=state['round'];calls=r['calls']
        if r['sample_count'] not in SIZES or len(calls)!=3*r['sample_count'] or r['status'] not in ('reserved','completed','failed','usage_unknown'):raise Stop('invalid_round_scope_or_status')
        if any(x['kind'] not in ('extract','direct','augmented') or x['method']!=('llm' if x['kind']=='extract' else 'decisions') or type(x['reserved_nanos']) is not int or x['reserved_nanos']<=0 or x['status'] not in ('not_started','pending','complete','unknown','not_sent') for x in calls):raise Stop('invalid_call_plan')
        if any(sum(x['kind']==kind for x in calls)!=r['sample_count'] for kind in ('extract','direct','augmented')):raise Stop('unequal_arm_scope')
        if sum(x['reserved_nanos'] for x in calls)>CAP-HISTORY:raise Stop('original_usd1_cap_exceeded')
        for x in calls:
            if x['status']=='complete' and (type(x.get('measured_cost_nanos')) is not int or not 0<=x['measured_cost_nanos']<=x['reserved_nanos']):raise Stop('measured_cost_exceeds_verified_bound')
        return state
    except (KeyError,TypeError) as error:raise Stop('invalid_comparison_ledger') from error

def summary(state):
    validate(state);calls=state['round']['calls']
    known=HISTORY+sum(x['measured_cost_nanos'] for x in calls if x['status']=='complete')
    pending=sum(x['reserved_nanos'] for x in calls if x['status'] in ('not_started','pending','unknown'))
    if known+pending>CAP:raise Stop('original_usd1_cap_exceeded')
    return {'known_cost_total_usd':usd(known),'unknown_or_pending_reserved_usd':usd(pending),'remaining_uncommitted_usd':usd(CAP-known-pending),'comparison_consumed':True,'invoice_verified':False}

def reserve(state,run_id,source_sha,n,plans):
    ready(state)
    if not str(run_id).isdigit() or not re.fullmatch('[0-9a-f]{40}',source_sha or ''):raise Stop('invalid_run_identity')
    result={'schema':4,'currency':'USD','limit_nanos':CAP,'limit_usd':usd(CAP),'previous_ledger':copy.deepcopy(state),'round':{'id':ROUND,'run_id':str(run_id),'source_sha':source_sha,'sample_count':n,'status':'reserved','calls':[{'index':i,'kind':p['kind'],'method':p['method'],'reserved_nanos':p['reserved_nanos'],'status':'not_started'} for i,p in enumerate(plans)]}}
    result['summary']=summary(result);return result

def owner(state,run_id):
    validate(state)
    if state['round']['status']!='reserved' or state['round']['run_id']!=str(run_id):raise Stop('comparison_owner_or_status_mismatch')

def start(state,run_id,index,method,actual_bound):
    state=copy.deepcopy(state);owner(state,run_id);calls=state['round']['calls'];c=calls[index]
    if c['status']!='not_started' or c['method']!=method or actual_bound>c['reserved_nanos'] or any(x['status']!='complete' for x in calls[:index]):raise Stop('call_reuse_prior_failure_or_unreserved_input')
    c['status']='pending';state['summary']=summary(state);return state

def finish(state,run_id,index,receipt):
    state=copy.deepcopy(state);owner(state,run_id);c=state['round']['calls'][index]
    if c['status']!='pending':raise Stop('call_not_pending')
    keys=('status','usage_complete','input_tokens','cached_input_tokens','cache_write_tokens','cache_write_tokens_basis','output_tokens','reasoning_tokens','total_tokens','measured_cost_nanos','estimated_cost_usd','price_version','currency','model_requested','model_returned','response_sha256','error_class','http_status')
    c['receipt']={k:receipt[k] for k in keys if k in receipt}
    c['status']='complete' if receipt.get('usage_complete') is True else 'unknown'
    if c['status']=='complete':c['measured_cost_nanos']=receipt.get('measured_cost_nanos')
    state['summary']=summary(state);return state

def close(state,run_id,success):
    state=copy.deepcopy(state);owner(state,run_id)
    if success and any(x['status']!='complete' or x.get('receipt',{}).get('status')!='completed' for x in state['round']['calls']):raise Stop('success_requires_all_complete_results')
    for x in state['round']['calls']:
        if x['status']=='not_started':x['status']='not_sent'
    unknown=any(x['status'] in ('unknown','pending') for x in state['round']['calls'])
    state['round']['status']='usage_unknown' if unknown else 'completed' if success else 'failed'
    state['summary']=summary(state);return state
