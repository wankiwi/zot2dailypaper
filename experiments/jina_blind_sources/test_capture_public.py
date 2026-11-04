"""Synthetic/offline tests: never access a service or user input."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import shlex
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import yaml

import capture_public as c
from snapshot_policy import freeze_cutoff


def payload():
    return {'schema':'jina_public_source_capture_v1',
            'source_config':{'source':{'arxiv':{'category':['cs.LG'],'window_days':7}},
                'preprint_interests':{'arxiv':{'categories':['cs.LG']}},
                'executor':{'source':['arxiv'],'debug':False}},
            'date_policy':{'mode':'completed_utc_day_end','additional_lag_days':0,
                           'minimum_asof_exclusive':'2026-11-04T00:00:00+00:00'},
            'expansion_arxiv_categories':['physics.chem-ph','cs.LG']}


class Fake:
    def __init__(self,cfg):
        self.config=cfg
        self.retriever_config=cfg.source.arxiv
        self.interests={}
    def convert_to_paper(self,value):
        return value


class Controls(unittest.TestCase):
    def test_source_only_contract(self):
        self.assertEqual(c.validate_input(payload()),payload())
    def test_no_private_or_credentials_in_input(self):
        for key in ('zotero','ranking_config','corpus','labels'):
            p=payload();p[key]={}
            with self.assertRaises(ValueError):c.validate_input(p)
        for key in ('api_key','password','mailto','interest_profile'):
            p=payload();p['source_config']['source']['arxiv'][key]='fixture'
            with self.assertRaisesRegex(ValueError,'private_or_credential'):c.validate_input(p)
    def test_debug_truncation_forbidden(self):
        p=payload();p['source_config']['executor']['debug']=True
        with self.assertRaises(ValueError):c.validate_input(p)
    def test_completed_new_day_earliest_nov5(self):
        with self.assertRaisesRegex(ValueError,'no_new_completed'):
            freeze_cutoff(payload()['date_policy'],datetime(2026,11,4,23,59,tzinfo=timezone.utc))
        self.assertTrue(freeze_cutoff(payload()['date_policy'],datetime(2026,11,5,tzinfo=timezone.utc))['asof_exclusive'].startswith('2026-11-05'))
    def test_delay_selects_current_completed_day_no_expiration(self):
        self.assertTrue(freeze_cutoff(payload()['date_policy'],datetime(2026,11,8,12,tzinfo=timezone.utc))['asof_exclusive'].startswith('2026-11-08'))
    def test_expansion_uses_effective_category_precedence(self):
        from zot2dailypaper.preprint_interests import categories_for
        from omegaconf import OmegaConf
        p=payload();expanded=c.expanded_config(p['source_config'],p['expansion_arxiv_categories'])
        self.assertEqual(categories_for(OmegaConf.create(expanded),'arxiv'),p['expansion_arxiv_categories'])
        self.assertEqual(p['source_config']['preprint_interests']['arxiv']['categories'],['cs.LG'])
    def test_history_only_identity_no_summary_or_channels(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'h.json'
            p.write_text(json.dumps({'version':1,'records':{'fixture':{'paper':{'title':'fixture','doi':'10.1/x','url':'https://example.org','tldr':'private-marker'},'channels':{'email':False}}}}))
            rows,sha=c.history_identities(p)
            self.assertEqual(len(rows),1);self.assertEqual(len(sha),64)
            self.assertNotIn('private-marker',json.dumps(rows));self.assertNotIn('channels',json.dumps(rows))
    def test_malformed_history_not_silently_complete(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'h.json';p.write_text('{}')
            result=c.capture(payload(),d,lambda x:None,p,datetime(2026,11,5,tzinfo=timezone.utc),lambda name:Fake,
                lambda *args:([],{'source':'arxiv','scope':args[2],'status':'empty','coverage_complete':True}))
            self.assertFalse(result['delivery_history_complete']);self.assertFalse(result['blind_ready'])
    def test_failed_partial_and_valid_empty_are_distinct(self):
        for status,complete in [('failed',False),('partial',False),('empty',True)]:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as d:
                p=Path(d)/'h.json';p.write_text('{"version":1,"records":{}}')
                checkpoints=[]
                result=c.capture(payload(),d,lambda x:checkpoints.append(copy.deepcopy(x)),p,
                    datetime(2026,11,5,tzinfo=timezone.utc),lambda name:Fake,
                    lambda *args:([],{'source':'arxiv','scope':args[2],'status':status,'coverage_complete':complete}))
                self.assertEqual(result['source_coverage_complete'],complete)
                self.assertEqual(len(result['source_attempts']),2)
                self.assertGreaterEqual(len(checkpoints),5);self.assertFalse(result['blind_ready'])
    def test_source_pin_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x';p.write_bytes(b'a');expected=hashlib.sha1(b'blob 1\0a').hexdigest()
            with patch.object(c,'EXPECTED_SOURCE_BLOBS',{'x':expected}):
                self.assertEqual(c.verify_sources(Path(d)),1)
                p.write_bytes(b'b')
                with self.assertRaisesRegex(ValueError,'source_snapshot'):c.verify_sources(Path(d))
    def test_encryption_roundtrip_no_plaintext(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'s.json';c.atomic_seal(p,{'public_candidates':[{'abstract':'fixture-only'}]},public,'a'*64)
            self.assertNotIn('fixture-only',p.read_text())
            v=json.loads(p.read_text());h={k:x for k,x in v.items() if k not in ('wrapped_key','nonce','ciphertext')}
            aad=c.canonical(h)
            aes=key.decrypt(base64.b64decode(v['wrapped_key']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=aad))
            plain=json.loads(AESGCM(aes).decrypt(base64.b64decode(v['nonce']),base64.b64decode(v['ciphertext']),aad))
            self.assertEqual(plain,{'public_candidates':[{'abstract':'fixture-only'}]})
    def test_cancel_not_swallowed_by_sdk_exception(self):
        caught=False
        try:
            try:raise c.CaptureCancelled()
            except Exception:self.fail('ordinary SDK exception swallowed cancellation')
        except c.CaptureCancelled:caught=True
        self.assertTrue(caught)
    def test_signal_entry_seals_before_sdk_cleanup_and_logs_no_private_text(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
        with tempfile.TemporaryDirectory() as d:
            directory=Path(d);(directory/'recipient.public.pem').write_bytes(public)
            output=directory/'out.json';ready=directory/'ready'
            code=f'''import sys,time
from pathlib import Path
sys.path.insert(0,{str(Path(c.__file__).parent)!r})
import capture_public as c
c.PUBLIC=Path({d!r})
c.verify_sources=lambda:0
def capture(value,private,checkpoint,history):
 checkpoint({{'status':'fixture_running','paid_calls':0,'blind_ready':False}})
 Path({str(ready)!r}).write_text('ready')
 print('fixture-sensitive-stdout',flush=True)
 try:
  while True:time.sleep(.01)
 except Exception:
  raise RuntimeError('fixture SDK swallowed cancellation')
c.capture=capture
raise SystemExit(c.main())
'''
            env={k:v for k,v in os.environ.items() if k not in c.DENIED_ENV}
            env.update(SEALED_OUTPUT=str(output),HISTORY_FILE='unused-fixture',JINA_BLIND_SOURCE_INPUT='{}',RUNNER_TEMP=d)
            command='exec '+shlex.quote(sys.executable)+' -c '+shlex.quote(code)+' 3>&1 > /dev/null 2>&1'
            child=subprocess.Popen(['/bin/bash','-c',command],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(ready.exists(),'fixture child did not reach capture')
                child.send_signal(signal.SIGTERM)
                stdout,stderr=child.communicate(timeout=5)
            finally:
                if child.poll() is None:child.kill();child.communicate()
            self.assertEqual(child.returncode,2)
            self.assertNotIn(b'fixture-sensitive',stdout+stderr)
            self.assertEqual(json.loads(stdout)['status'],'capture_interrupted')
            envelope=json.loads(output.read_text())
            header={k:v for k,v in envelope.items() if k not in ('wrapped_key','nonce','ciphertext')}
            aad=c.canonical(header)
            aes=key.decrypt(base64.b64decode(envelope['wrapped_key']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=aad))
            value=json.loads(AESGCM(aes).decrypt(base64.b64decode(envelope['nonce']),base64.b64decode(envelope['ciphertext']),aad))
            self.assertEqual(value['status'],'capture_interrupted');self.assertFalse(value['blind_ready'])
    def test_workflow_scope_and_ciphertext_only(self):
        text=(Path(__file__).resolve().parents[2]/'.github/workflows/test.yml').read_text()
        y=yaml.load(text,Loader=yaml.BaseLoader)
        self.assertEqual(set(y['on']),{'workflow_dispatch'})
        self.assertEqual(y['permissions'],{'contents':'read'})
        job=y['jobs']['public-source-capture'];self.assertEqual(job['timeout-minutes'],'45')
        self.assertEqual(sum(int(step['timeout-minutes']) for step in job['steps']),44)
        self.assertIn('experiment/jina-blind-source-20261104',job['if'])
        secrets=[step.get('env',{}) for step in job['steps']]
        keys=set().union(*(set(x) for x in secrets))
        self.assertFalse(keys & c.DENIED_ENV)
        self.assertTrue({'JINA_BLIND_SOURCE_INPUT','OPENREVIEW_PASSWORD','OPENALEX_API_KEY'}<=keys)
        artifact=[step['with'] for step in job['steps'] if step.get('uses')=='actions/upload-artifact@v4'][0]
        self.assertEqual(artifact['path'],'sealed/public-sources.sealed.json');self.assertEqual(artifact['retention-days'],'1')
        self.assertIn('exec .venv/bin/python',text)
        self.assertNotIn('workflow_state.py',text)
    def test_no_zotero_or_model_or_delivery_entrypoint(self):
        text=(Path(c.__file__)).read_text()
        for token in ('from zot2dailypaper.executor','fetch_zotero','SentenceTransformer(','send_email(','State('):
            self.assertNotIn(token,text)


if __name__=='__main__':unittest.main()
