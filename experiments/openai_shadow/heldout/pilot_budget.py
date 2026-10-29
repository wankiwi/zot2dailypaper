"""One new held-out pilot, retaining the complete original v1/v2 ledger."""
import copy
import re
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import budget_v2 as previous
from common import PLAN_ID,Stop

CAP=1_000_000_000
HISTORY=437_000
MAX_CALLS=24
usd=previous.usd

def verify_previous(state):
    previous.validate(state)
    ext=state['extension'];receipt=ext.get('request_outcome',{})
    if state['history_v1']['original_claim_blob_sha']!='7fe07670fbcb6650a4835add634ec0edd931af73' or ext.get('request_sha256')!='89a083dbd6e256e14ec264f31aa447f0e087db908594646ab0c12a06d8fac0b7':raise Stop('verified_historical_reservation_identity_changed')
    required={'status':'completed','usage_complete':True,'input_tokens':2634,'cached_input_tokens':0,'cache_write_tokens':0,'output_tokens':0,'reasoning_tokens':0,'measured_cost_nanos':263400}
    if ext.get('run_id')!='40863920037' or ext.get('source_sha')!='cc3fb9d98d1efe8d512831aad0c70853a5e9e2ff' or ext.get('status')!='completed' or ext.get('measured_cost_nanos')!=263400 or any(receipt.get(k)!=v for k,v in required.items()):raise Stop('v1_v2_history_not_the_verified_settled_receipts')
    if state['summary']['known_cost_total_usd']!='0.000437000' or state['summary']['unknown_or_pending_reserved_usd']!='0.000000000':raise Stop('history_spend_or_pending_reservation_changed')
    return state

def ready(state):
    if state.get('schema')==2:verify_previous(state);return True
    validate(state)
    if state['pilot']['status']!='authorized_not_started':raise Stop('heldout_pilot_already_consumed_no_retry')
    return True

def validate(state):
    try:
        if state['schema']!=3 or state['limit_nanos']!=CAP or state['currency']!='USD':raise Stop('original_budget_cap_changed')
        verify_previous(state['previous_ledger'])
        pilot=state['pilot']
        if pilot['id']!=PLAN_ID or pilot['max_calls']!=MAX_CALLS or pilot['status'] not in ('authorized_not_started','reserved','completed','failed','usage_unknown'):raise Stop('unapproved_pilot_identity_or_status')
        if pilot['status']!='authorized_not_started':
            if not 1<=len(pilot['calls'])<=MAX_CALLS:raise Stop('unapproved_call_count')
            if any(type(c['reserved_nanos']) is not int or c['reserved_nanos']<=0 for c in pilot['calls']):raise Stop('invalid_call_reservation')
            if sum(c['reserved_nanos'] for c in pilot['calls'])>CAP-HISTORY:raise Stop('original_usd1_cap_exceeded')
            for call in pilot['calls']:
                if call['status'] not in ('not_started','pending','complete','unknown','not_sent'):raise Stop('invalid_call_status')
                if call['status']=='complete' and (type(call.get('measured_cost_nanos')) is not int or not 0<=call['measured_cost_nanos']<=call['reserved_nanos']):raise Stop('invalid_measured_usage_or_overrun')
        return state
    except (KeyError,TypeError) as error:raise Stop('invalid_shared_pilot_ledger') from error

def summary(state):
    validate(state);calls=state['pilot'].get('calls',[])
    known=HISTORY+sum(c.get('measured_cost_nanos',0) for c in calls if c['status']=='complete')
    pending=sum(c['reserved_nanos'] for c in calls if c['status'] in ('not_started','pending','unknown'))
    if known+pending>CAP:raise Stop('original_usd1_cap_exceeded')
    return {'known_cost_total_usd':usd(known),'unknown_or_pending_reserved_usd':usd(pending),'remaining_uncommitted_usd':usd(CAP-known-pending),'pilot_consumed':state['pilot']['status']!='authorized_not_started','invoice_verified':False}

def reserve(state,run_id,source_sha,plans):
    ready(state)
    if not str(run_id).isdigit() or not re.fullmatch('[0-9a-f]{40}',source_sha or '') or any(not re.fullmatch('[0-9a-f]{64}',p.get('request_sha256','')) for p in plans):raise Stop('invalid_run_or_request_identity')
    if state['schema']==2:
        state={'schema':3,'currency':'USD','limit_nanos':CAP,'limit_usd':usd(CAP),'previous_ledger':copy.deepcopy(state),'pilot':{'id':PLAN_ID,'max_calls':MAX_CALLS,'status':'authorized_not_started'}}
    else:state=copy.deepcopy(state)
    if not 1<=len(plans)<=MAX_CALLS:raise Stop('invalid_planned_request_count')
    calls=[{'index':i,'request_sha256':p['request_sha256'],'reserved_nanos':p['reserved_nanos'],'status':'not_started'} for i,p in enumerate(plans)]
    state['pilot'].update(status='reserved',run_id=str(run_id),source_sha=source_sha,calls=calls)
    state['summary']=summary(state);return state

def owner(state,run_id):
    validate(state)
    if state['pilot']['status']!='reserved' or state['pilot']['run_id']!=str(run_id):raise Stop('pilot_owner_or_status_mismatch')

def start(state,run_id,index,request_hash):
    state=copy.deepcopy(state);owner(state,run_id);calls=state['pilot']['calls'];call=calls[index]
    if call['status']!='not_started' or call['request_sha256']!=request_hash or any(c['status'] in ('pending','unknown') for c in calls) or any(c['status']!='complete' for c in calls[:index]):raise Stop('request_reuse_or_prior_unknown_usage')
    call['status']='pending';state['summary']=summary(state);return state

def finish_call(state,run_id,index,receipt):
    state=copy.deepcopy(state);owner(state,run_id);call=state['pilot']['calls'][index]
    if call['status']!='pending':raise Stop('call_not_pending')
    complete=receipt.get('usage_complete') is True
    # Public accounting is a strict allowlist: no scores, paper IDs, labels or profiles.
    names=('status','usage_complete','input_tokens','cached_input_tokens','cache_write_tokens','output_tokens','reasoning_tokens','total_tokens','measured_cost_nanos','estimated_cost_usd','price_version','currency','model_requested','model_returned','response_sha256')
    call['receipt']={k:receipt[k] for k in names if k in receipt}
    call['status']='complete' if complete else 'unknown'
    if complete:call['measured_cost_nanos']=receipt.get('measured_cost_nanos')
    state['summary']=summary(state);return state

def close(state,run_id,success):
    state=copy.deepcopy(state);owner(state,run_id)
    if success and any(c['status']!='complete' or c.get('receipt',{}).get('status')!='completed' for c in state['pilot']['calls']):raise Stop('complete_success_requires_all_measured_results')
    for call in state['pilot']['calls']:
        if call['status']=='not_started':call['status']='not_sent'
    unknown=any(c['status'] in ('pending','unknown') for c in state['pilot']['calls'])
    state['pilot']['status']='usage_unknown' if unknown else 'completed' if success else 'failed'
    state['summary']=summary(state);return state
