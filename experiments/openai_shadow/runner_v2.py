"""Sources-only by default; one authorized, cumulative v2 request when explicit."""
import argparse
import base64
from datetime import datetime,timezone
import json
import os
from pathlib import Path

import budget_v2 as budget
import runner as v1
import sources_v2 as sources

ROOT=Path(__file__).resolve().parent


def paths():
    temp=Path(os.environ['RUNNER_TEMP'])
    private=temp/'shadow-v2-private';output=temp/'shadow-v2-output'
    private.mkdir(mode=0o700,exist_ok=True);output.mkdir(mode=0o700,exist_ok=True)
    return private,output


def read_remote():
    entry=v1.github('GET')
    if not entry or 'sha' not in entry:raise budget.BudgetStop('cumulative_remote_ledger_missing')
    state=json.loads(base64.b64decode(entry['content']))
    budget.validate(state)
    return state,entry['sha']


def put_remote(state,sha):
    v1.github('PUT',{'message':'Record authorized cumulative v2 experiment accounting',
                    'branch':v1.BRANCH,'sha':sha,
                    'content':base64.b64encode(json.dumps(state,sort_keys=True).encode()).decode()})


class SharedLedger:
    """Remote shared ledger authorizes; inner ledger is only a request receipt."""
    def __init__(self,core,output):
        self.core=core;self.output=output
        self.inner=core.Ledger(output/'v2_request_receipt.json')
        self.cumulative=None;self.new_attempts=0
    def reserve(self,method,request):
        if method!='decisions':raise budget.BudgetStop('only_one_decisions_v2_authorized')
        state,sha=read_remote()
        amount=self.core.reserve_nanos(method,request)
        proposed=budget.reserve(state,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'],self.core.digest(request),amount)
        self.inner.reserve(method,request) # durable local evidence before remote CAS
        self.core.write_json(self.output/'budget_ledger.json',proposed)
        put_remote(proposed,sha) # Must succeed before any model request can start.
        self.cumulative=proposed;self.new_attempts=1
        return amount
    def finish(self,method,fields):
        actual=self.inner.finish(method,fields)
        if self.cumulative is None:raise budget.BudgetStop('missing_confirmed_cumulative_reservation')
        completed=budget.finish(self.cumulative,os.environ['GITHUB_RUN_ID'],actual)
        self.core.write_json(self.output/'budget_ledger.json',completed)
        self.cumulative=completed
        try:
            remote,sha=read_remote()
            ext=remote['extension'];mine=completed['extension']
            if ext.get('run_id')!=mine['run_id'] or ext.get('request_sha256')!=mine['request_sha256']:
                raise budget.BudgetStop('remote_owner_changed')
            put_remote(completed,sha)
        except Exception as error:
            # Remote reservation remains conservative; never retry the model.
            self.cumulative['remote_finalization_error_class']=type(error).__name__
            self.core.write_json(self.output/'budget_ledger.json',self.cumulative)
        return actual
    def inspect(self):
        return self.cumulative


def source_stage(private,output,get=None):
    core=v1.load_core(private)
    papers=json.loads((ROOT/'papers.public.json').read_text())
    config=json.loads((ROOT/'sources_v2.public.json').read_text())
    protocol=json.loads((ROOT/'protocol_v2.public.json').read_text())
    if len(papers)!=5 or {p['id'] for p in papers}!=core.EXPECTED_IDS:
        raise sources.SourceStop('same_five_scope_changed')
    if core.digest(papers)!=protocol.get('v1_papers_sha256'):
        raise sources.SourceStop('v1_input_fixture_changed_before_abstract_completion')
    complete,evidence,ready=sources.prepare(papers,config,get=get or sources.fetch)
    price=json.loads((ROOT/'pricing.public.json').read_text())
    price_ready=price.get('version')==core.PRICE_VERSION and price.get('verified_on')==datetime.now(timezone.utc).date().isoformat()
    status={'phase':'sources_only','experiment':budget.V2_ID,'ready':ready,'price_ready':price_ready,
            'api_requests_this_run':0,'historical_cost_usd':budget.usd(budget.V1_COST),
            'evidence':evidence,'papers_sha256':core.digest(complete),'private_labels_received':False}
    core.write_json(output/'source_status.json',status)
    if ready:
        core.write_json(private/'papers.json',complete)
        core.write_json(private/'fixture_manifest.json',{'papers_sha256':core.digest(complete)})
    print(json.dumps({'phase':'sources_only','ready':ready,'price_ready':price_ready,
                      'missing':[key for key,value in evidence.items() if value['evidence_status']!='abstract_available'],
                      'source_failures':{key:value for key,value in evidence.items() if value['evidence_status']!='abstract_available'},
                      'api_requests_this_run':0},sort_keys=True))
    return status


def score_stage(private,output):
    core=v1.load_core(private)
    status={'phase':'v2_score','status':'blocked_before_model','api_requests_this_run':0,
            'historical_cost_usd':budget.usd(budget.V1_COST),'private_labels_received':False}
    ledger=None
    try:
        evidence=core.read_json(output/'source_status.json')
        if evidence.get('ready') is not True:raise sources.SourceStop('full_five_abstracts_required_no_title_only_repeat')
        papers=core.load_fixture()
        if any(not p['abstract'].strip() for p in papers) or core.digest(papers)!=evidence['papers_sha256']:
            raise sources.SourceStop('complete_evidence_snapshot_mismatch')
        price=core.read_json(ROOT/'pricing.public.json')
        if price.get('version')!=core.PRICE_VERSION or price.get('verified_on')!=datetime.now(timezone.utc).date().isoformat():
            raise sources.SourceStop('current_day_pricing_required')
        rubric=core.read_json(ROOT/'rubric.public.json') # EXACT v1 rubric; no label-driven tuning.
        protocol=core.read_json(ROOT/'protocol_v2.public.json')
        if core.digest(rubric)!=protocol.get('v1_rubric_sha256'):
            raise sources.SourceStop('rubric_changed_not_the_predeclared_ablation')
        core.write_json(private/'rubric.json',rubric)
        core.write_json(private/'pricing_snapshot.json',price)
        core.write_json(private/'source_review.json',{'live_ready':True,'title_only_fallback_approved':False,'title_only_ids':[]})
        key=core.credential(os.environ)
        check=core.preflight(papers,os.environ)
        if not check['live_ready']:raise sources.SourceStop('full_evidence_or_client_preflight_blocked')
        request=core.payload('decisions',papers,rubric)
        core.write_json(output/'request_metadata.json',{
            'method':'decisions_same_v1_rubric_full_abstracts','model_requested':core.MODEL,
            'request_sha256':core.digest(request),'request_bytes':len(core.dumps(request).encode()),
            'rubric_sha256':core.digest(rubric),'original_rubric_commit':budget.V1_COMMIT,
            'question_count':5,'raw_request_omitted_for_copyright':True,
            'sources':evidence['evidence']})
        ledger=SharedLedger(core,output)
        result=core.run_one('decisions',request,ledger,key,output=output)
        # Decisions return scores, distributions and usage, not abstract text.
        status.update(status=result['status'],api_requests_this_run=ledger.new_attempts,
                      cumulative_budget=ledger.inspect()['summary'],
                      new_estimated_cost_usd=result.get('estimated_cost_usd'),
                      model_returned=result.get('model_returned'),scores=result.get('scores'))
    except Exception as error:
        status['error_class']=type(error).__name__
        if isinstance(error,(sources.SourceStop,budget.BudgetStop,core.Blocked)):status['reason']=str(error)
        if ledger is not None:
            status['api_requests_this_run']=ledger.new_attempts
            if ledger.cumulative is not None:status['cumulative_budget']=ledger.cumulative['summary']
    core.write_json(output/'run_status.json',status)
    print(json.dumps(status,sort_keys=True))
    return status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--sources-only',action='store_true');mode.add_argument('--score',action='store_true')
    args=parser.parse_args();v1.assert_job(os.environ)
    private,output=paths()
    if args.sources_only:source_stage(private,output)
    else:score_stage(private,output)


if __name__=='__main__':main()
