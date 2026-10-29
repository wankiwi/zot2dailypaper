"""Synthetic-only tests. No user titles, research interests or labels embedded."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import runner


class GenericTests(unittest.TestCase):
    def test_main_schedule_and_rerun_refused(self):
        valid={"GITHUB_REPOSITORY":runner.REPO,"GITHUB_REF":"refs/heads/"+runner.BRANCH,
               "GITHUB_EVENT_NAME":"workflow_dispatch","GITHUB_RUN_ATTEMPT":"1",
               "GITHUB_RUN_ID":"123","GITHUB_SHA":"synthetic-sha"}
        runner.assert_job(valid)
        for field,value in (("GITHUB_REF","refs/heads/main"),("GITHUB_EVENT_NAME","schedule"),("GITHUB_RUN_ATTEMPT","2")):
            with self.assertRaises(runner.Stop):runner.assert_job(dict(valid,**{field:value}))

    def test_missing_data_blocks_before_budget_or_model(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(runner.Stop,"approved_public_dataset"):
                runner.load_approved_public_data(Path(path),set("abcde"))

    def test_approval_and_private_field_guards(self):
        ids=set("abcde")
        data={"data_approval.public.json":{"public_disclosure_approved":True},
              "papers.public.json":[{"id":i,"title":"synthetic title","abstract":"synthetic abstract"} for i in sorted(ids)],
              "rubric.public.json":{"version":"synthetic","instructions":"synthetic criteria","levels":[]},
              "source_review.public.json":{"live_ready":True},
              "pricing.public.json":{"verified_on":datetime.now(timezone.utc).date().isoformat()}}
        with tempfile.TemporaryDirectory() as path:
            root=Path(path)
            def write():
                for name,value in data.items():(root/name).write_text(json.dumps(value))
            write();runner.load_approved_public_data(root,ids)
            data["data_approval.public.json"]["public_disclosure_approved"]=False;write()
            with self.assertRaisesRegex(runner.Stop,"disclosure_approval"):
                runner.load_approved_public_data(root,ids)
            data["data_approval.public.json"]["public_disclosure_approved"]=True
            data["papers.public.json"][0]["label"]=1;write()
            with self.assertRaisesRegex(runner.Stop,"no_labels_or_private_profile"):
                runner.load_approved_public_data(root,ids)

    def test_remote_budget_claim_is_once_only(self):
        original=runner.github;calls=[]
        try:
            runner.github=lambda *args,**kwargs:{"sha":"already-present"}
            with self.assertRaisesRegex(runner.Stop,"already_claimed"):
                runner.reserve_remote_budget("123","synthetic")
            def fake(method,*args,**kwargs):
                calls.append(method)
                return None if method=="GET" else {"content":{"sha":"synthetic"}}
            runner.github=fake
            result=runner.reserve_remote_budget("123","synthetic")
            self.assertEqual(calls,["GET","PUT"])
            self.assertEqual(result["reserved_usd"],"1.000000000")
        finally:runner.github=original

    def test_core_usage_cache_and_reasoning_accounting(self):
        with tempfile.TemporaryDirectory() as path:
            core=runner.load_core(Path(path))
            usage={"input_tokens":1000,"input_tokens_details":{"cached_tokens":100,"cache_write_tokens":200},
                   "output_tokens":300,"output_tokens_details":{"reasoning_tokens":200},"total_tokens":1300}
            self.assertEqual(core.usage_cost("llm",usage)["estimated_cost_usd"],"0.000246000")


if __name__ == "__main__":unittest.main()
