"""One explicitly authorized v2 extension of the original USD 1 allowance."""
import copy
from decimal import Decimal
import re

CAP = 1_000_000_000
V1_COST = 173_600
V1_RUN = "40847588593"
V1_COMMIT = "2e757aa4a9a83d5800b4baf49a67636af1271020"
V1_ARTIFACT = 12885697249
V1_DIGEST = "e09da142a8429ade03438821860fbf90bb15f9a96ebd9fee67adf380e90c5ea2"
V2_ID = "same-five-complete-abstracts-v2"


class BudgetStop(ValueError):
    pass


def usd(nanos):
    return format(Decimal(nanos)/Decimal(1_000_000_000), ".9f")


def verified_receipt():
    return {"run_id":V1_RUN,"source_commit":V1_COMMIT,"artifact_id":V1_ARTIFACT,
            "artifact_sha256":V1_DIGEST,"verification_source":"parent read original ZIP",
            "requests":1,"usage_complete":True,"unknown_usage_attempts":0,
            "input_tokens":1736,"cached_input_tokens":0,"cache_write_tokens":0,
            "output_tokens":0,"reasoning_tokens":0,"estimated_cost_nanos":V1_COST,
            "estimated_cost_usd":usd(V1_COST),"invoice_verified":False,
            "price_currency":"USD","input_usd_per_million":"0.10"}


def reconcile(original, original_blob_sha, receipt):
    expected={"schema":1,"plan":"openai-shadow-20261029-v1","run_id":V1_RUN,
              "source_sha":V1_COMMIT,"status":"reserved_outcome_unknown",
              "currency":"USD","limit_usd":"1.000000000","reserved_usd":"1.000000000"}
    if original != expected or not re.fullmatch(r"[0-9a-f]{40}",original_blob_sha or ""):
        raise BudgetStop("original_claim_changed_preserve_and_investigate")
    if receipt != verified_receipt():
        raise BudgetStop("v1_receipt_not_the_verified_complete_original")
    result={"schema":2,"plan":"openai-shadow-20261029-shared-usd1","currency":"USD",
            "limit_nanos":CAP,"limit_usd":usd(CAP),
            "history_v1":{"original_claim":copy.deepcopy(original),"original_claim_blob_sha":original_blob_sha,
                          "original_reserved_usd":"1.000000000","settlement":"complete_usage_verified",
                          "settled_estimated_cost_nanos":V1_COST,"settled_estimated_cost_usd":usd(V1_COST),
                          "released_unused_reservation_usd":usd(CAP-V1_COST),"receipt":copy.deepcopy(receipt)},
            "extension":{"id":V2_ID,"authorization_date":"2026-10-29","same_five_papers_only":True,
                         "max_additional_requests":1,"status":"authorized_not_started"}}
    return summarize(result)


def validate(state):
    try:
        hist=state["history_v1"];ext=state["extension"]
        if state["schema"]!=2 or state["currency"]!="USD" or state["limit_nanos"]!=CAP:
            raise BudgetStop("shared_budget_identity_changed")
        if hist["receipt"]!=verified_receipt() or hist["settled_estimated_cost_nanos"]!=V1_COST:
            raise BudgetStop("historical_cost_or_evidence_changed")
        expected_claim={"schema":1,"plan":"openai-shadow-20261029-v1","run_id":V1_RUN,
                        "source_sha":V1_COMMIT,"status":"reserved_outcome_unknown","currency":"USD",
                        "limit_usd":"1.000000000","reserved_usd":"1.000000000"}
        if hist["original_claim"]!=expected_claim or not re.fullmatch(r"[0-9a-f]{40}",hist.get('original_claim_blob_sha','')):
            raise BudgetStop("original_reservation_not_preserved")
        if ext["id"]!=V2_ID or ext["max_additional_requests"]!=1 or ext["same_five_papers_only"] is not True:
            raise BudgetStop("unapproved_extension")
        if ext["status"] not in ("authorized_not_started","reserved_outcome_unknown","completed","failed","usage_unknown"):
            raise BudgetStop("unknown_extension_status")
        if ext["status"]!="authorized_not_started":
            reserve=ext["reserved_nanos"]
            if type(reserve) is not int or not 0<reserve<=CAP-V1_COST:
                raise BudgetStop("invalid_shared_reservation")
            cost=ext.get("measured_cost_nanos")
            if cost is not None and (type(cost) is not int or cost<0 or cost>reserve):
                raise BudgetStop("measured_cost_exceeded_reservation_stop")
        return state
    except (KeyError,TypeError) as error:
        raise BudgetStop("invalid_cumulative_ledger") from error


def summarize(state):
    validate(state);ext=state["extension"]
    used=ext.get("measured_cost_nanos")
    outstanding=ext.get("reserved_nanos",0) if used is None else 0
    committed=V1_COST+(used or 0)+outstanding
    if committed>CAP:raise BudgetStop("shared_usd1_cap_exceeded")
    state["summary"]={"historical_known_cost_usd":usd(V1_COST),
                      "known_cost_total_usd":usd(V1_COST+(used or 0)),
                      "unknown_or_pending_reserved_usd":usd(outstanding),
                      "remaining_uncommitted_usd":usd(CAP-committed),
                      "extension_consumed":ext["status"]!="authorized_not_started",
                      "invoice_verified":False}
    return state


def reserve(state,run_id,source_sha,request_sha,reserved_nanos):
    state=copy.deepcopy(validate(state));ext=state["extension"]
    if ext["status"]!="authorized_not_started":raise BudgetStop("v2_already_consumed_no_retry")
    if type(reserved_nanos) is not int or not 0<reserved_nanos<=CAP-V1_COST:
        raise BudgetStop("reservation_exceeds_remaining_original_allowance")
    if not str(run_id).isdigit() or not re.fullmatch(r"[0-9a-f]{40}",source_sha or "") or not re.fullmatch(r"[0-9a-f]{64}",request_sha or ""):
        raise BudgetStop("invalid_run_or_request_identity")
    ext.update(status="reserved_outcome_unknown",run_id=str(run_id),source_sha=source_sha,
               request_sha256=request_sha,reserved_nanos=reserved_nanos,reserved_usd=usd(reserved_nanos),
               measured_cost_nanos=None,usage_complete=False)
    return summarize(state)


def finish(state,run_id,outcome):
    state=copy.deepcopy(validate(state));ext=state["extension"]
    if ext["status"]!="reserved_outcome_unknown" or ext["run_id"]!=str(run_id):
        raise BudgetStop("reservation_owner_or_terminal_status_mismatch")
    complete=outcome.get("usage_complete") is True
    cost=outcome.get("measured_cost_nanos") if complete else None
    if complete and type(cost) is not int:raise BudgetStop('complete_usage_requires_measured_cost')
    ext.update(status="completed" if outcome.get("status")=="completed" and complete else "failed" if complete else "usage_unknown",
               usage_complete=complete,measured_cost_nanos=cost,request_outcome=outcome)
    return summarize(state)
