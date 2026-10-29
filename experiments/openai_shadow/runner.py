"""Generic manual test-branch client. No dataset, rubric or labels are embedded."""
import argparse
import base64
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REPO = "wankiwi/zot2dailypaper"
BRANCH = "experiment/openai-shadow-20261029"
PUBLIC_ROOT = Path(__file__).resolve().parent
STATE_PATH = "experiments/openai_shadow/state/budget.json"
PREFIX = "https://api.github.com/repos/" + REPO + "/contents/"


class Stop(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Stop("redirect_refused")


def github(method, body=None, allow_missing=False):
    url = PREFIX + STATE_PATH
    if method == "GET":
        url += "?" + urllib.parse.urlencode({"ref": BRANCH})
    req = urllib.request.Request(url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as res:
            raw = res.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise Stop("budget_response_too_large")
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        if allow_missing and error.code == 404:
            return None
        raise Stop("github_budget_request_failed") from None


def assert_job(env):
    expected = {"GITHUB_REPOSITORY": REPO, "GITHUB_REF": "refs/heads/" + BRANCH,
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ATTEMPT": "1"}
    if any(env.get(name) != value for name, value in expected.items()):
        raise Stop("not_the_authorized_manual_test_branch_attempt")
    if not env.get("GITHUB_RUN_ID", "").isdigit() or not env.get("GITHUB_SHA"):
        raise Stop("missing_run_identity")


def load_core(work):
    spec = importlib.util.spec_from_file_location("isolated_shadow_core", PUBLIC_ROOT / "core.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    core.ROOT = work
    return core


def load_approved_public_data(root, expected_ids):
    names = ("data_approval.public.json", "papers.public.json", "rubric.public.json",
             "source_review.public.json", "pricing.public.json")
    if any(not (root/name).is_file() for name in names):
        raise Stop("approved_public_dataset_and_rubric_not_supplied")
    approval, papers, rubric, review, price = [json.loads((root/name).read_text()) for name in names]
    if approval.get("public_disclosure_approved") is not True:
        raise Stop("explicit_public_disclosure_approval_required")
    if len(papers) != 5 or {p["id"] for p in papers} != expected_ids:
        raise Stop("unapproved_five_paper_scope")
    if any(set(p) != {"id", "title", "abstract"} for p in papers):
        raise Stop("unapproved_paper_fields_no_labels_or_private_profile")
    if any(not isinstance(p[k], str) for p in papers for k in ("id", "title", "abstract")) or any(not p["title"].strip() for p in papers):
        raise Stop("invalid_public_title_or_abstract")
    missing={p["id"] for p in papers if not p["abstract"].strip()}
    title_only_allowed=(approval.get("title_only_fallback_approved") is True
                        and review.get("title_only_fallback_approved") is True
                        and set(review.get("title_only_ids",[]))==missing)
    if missing and not title_only_allowed:
        raise Stop("complete_abstracts_or_explicit_title_only_scope_required")
    if set(rubric) != {"version", "instructions", "levels"} or review.get("live_ready") is not True:
        raise Stop("public_rubric_or_source_review_incomplete")
    if price.get("verified_on") != datetime.now(timezone.utc).date().isoformat():
        raise Stop("pricing_not_verified_today")
    methods = approval.get("methods", ["decisions"])
    if methods not in (["decisions"], ["decisions", "llm"]):
        raise Stop("unapproved_methods")
    if "llm" in methods and not price.get("responses_schema_verified", False):
        raise Stop("optional_responses_schema_unverified")
    return papers, rubric, review, price, methods


def reserve_remote_budget(run_id, source_sha):
    existing = github("GET", allow_missing=True)
    if existing is not None:
        raise Stop("budget_already_claimed_do_not_reset_or_rerun")
    claim = {"schema":1,"plan":"openai-shadow-20261029-v1", "run_id":run_id,
             "source_sha":source_sha,"status":"reserved_outcome_unknown",
             "currency":"USD","limit_usd":"1.000000000","reserved_usd":"1.000000000"}
    # Contents API create is atomic: a second creator without sha is rejected.
    body = {"message":"Reserve isolated experiment allowance", "branch":BRANCH,
            "content":base64.b64encode(json.dumps(claim).encode()).decode()}
    github("PUT", body)
    return claim


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true")
    args=parser.parse_args()
    assert_job(os.environ)
    output=Path(os.environ["RUNNER_TEMP"])/"shadow-output"
    output.mkdir(mode=0o700,exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix="shadow-local-",dir=os.environ["RUNNER_TEMP"]))
    core=load_core(work)
    status={"status":"preflight", "api_requests_sent":0,"estimated_spend_usd":"0.000000000",
            "secret_present":bool(os.environ.get("OPENAI_API_KEY")),"private_labels_received":False}
    try:
        papers,rubric,review,price,methods=load_approved_public_data(PUBLIC_ROOT,core.EXPECTED_IDS)
        status["scoring_basis"]={p["id"]:"abstract" if p["abstract"].strip() else "title_only" for p in papers}
        status["evaluation_role"]="rubric-only development sanity test; no current-profile baseline or held-out quality claim"
        if price.get("version") != core.PRICE_VERSION:
            raise Stop("unverified_price_version")
        for name,value in (("papers.json",papers),("rubric.json",rubric),("source_review.json",review),("pricing_snapshot.json",price)):
            core.write_json(work/name,value)
        core.write_json(work/"fixture_manifest.json",{"papers_sha256":core.digest(papers)})
        key=core.credential(os.environ)
        if any(not core.preflight(papers,os.environ,method)["live_ready"] for method in methods):
            raise Stop("model_preflight_blocked")
        if args.preflight_only:
            status["status"]="ready_no_call_requested"
        else:
            reserve_remote_budget(os.environ["GITHUB_RUN_ID"],os.environ["GITHUB_SHA"])
            ledger=core.Ledger(output/"budget_ledger.json")
            for method in methods:
                request=core.payload(method,papers,rubric)
                core.write_json(output/(method+".request.json"),request)
                result=core.run_one(method,request,ledger,key,output=output)
                if result.get("status") != "completed":break
            state=ledger.inspect()
            status.update(status="finished",api_requests_sent=len(state["attempts"]),
                          estimated_spend_usd=state["summary"]["estimated_cost_from_complete_usage_usd"],
                          unknown_usage_attempts=state["summary"]["unknown_usage_attempts"])
    except Stop as error:
        status.update(status="blocked_before_model_call",reason=str(error))
    except Exception as error:
        # No error message/body or traceback can echo credentials/paper content.
        status.update(status="stopped",error_class=type(error).__name__)
        ledger_file=output/"budget_ledger.json"
        if ledger_file.exists():
            state=json.loads(ledger_file.read_text())
            status.update(api_requests_sent=len(state["attempts"]),
                          estimated_spend_usd=state["summary"]["estimated_cost_from_complete_usage_usd"],
                          unknown_usage_attempts=state["summary"]["unknown_usage_attempts"])
    core.write_json(output/"run_status.json",status)
    print(json.dumps(status,sort_keys=True))


if __name__ == "__main__":
    main()
