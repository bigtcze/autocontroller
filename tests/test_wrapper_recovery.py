"""Exercise real guard subprocess with a fake OpenCode V2 API (no network)."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

WRAPPER=Path(__file__).resolve().parents[1]/'opencode-project-safe.py'


class WrapperRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.repo=root/'repo';self.repo.mkdir()
        subprocess.run(['git','init','-q','-b','main',str(self.repo)],check=True)
        self.pending=root/'state'/'pending.json'
        self.fake=root/'fake-opencode'
        self.fake.write_text('''#!/usr/bin/env python3
import json,sys
args=sys.argv[1:]
if args[:1]==['api']:
    path=args[2]
    data={'/api/info':{'version':'2.0.0'},'/api/session/active':{},'/api/session':[]}.get(path)
    if data is None:
        print('not found',file=sys.stderr);sys.exit(1)
    print(json.dumps({'data':data}));sys.exit(0)
if args[:1]==['run']:
    print('HTTP 429: quota reached');sys.exit(1)
print('unknown',file=sys.stderr);sys.exit(1)
''')
        self.fake.chmod(0o700)
        self.env={**os.environ,'AUTONOMY_PROJECT_ROOT':str(self.repo),'AUTONOMY_PROJECT_NAME':'test','AUTONOMY_PENDING_MARKER':str(self.pending),'AUTONOMY_OPENCODE':str(self.fake)}

    def cli(self,*args):
        return subprocess.run([sys.executable,str(WRAPPER),*args],env=self.env,cwd=self.repo,text=True,capture_output=True,timeout=30)

    def test_unstarted_quota_creates_marker_and_can_recover_same_task(self):
        first=self.cli('run','--auto','--title','quota test','hello')
        self.assertEqual(first.returncode,78,(first.stdout,first.stderr))
        self.assertIn('429',first.stdout)
        self.assertTrue(self.pending.exists())
        self.assertEqual(json.loads(self.pending.read_text())['status'],'starting')
        clear=self.cli('--clear-unstarted-quota')
        self.assertEqual(clear.returncode,0,(clear.stdout,clear.stderr))
        self.assertFalse(self.pending.exists())

    def test_session_id_marker_is_never_dropped(self):
        self.pending.parent.mkdir(parents=True)
        self.pending.write_text(json.dumps({'status':'starting','title':'x','session_id':'ses_existing'}))
        result=self.cli('--clear-unstarted-quota')
        self.assertEqual(result.returncode,2)
        self.assertTrue(self.pending.exists())

    def test_resuming_marker_is_never_dropped(self):
        self.pending.parent.mkdir(parents=True)
        self.pending.write_text(json.dumps({'status':'resuming','title':'x'}))
        result=self.cli('--clear-unstarted-quota')
        self.assertEqual(result.returncode,2)
        self.assertTrue(self.pending.exists())

    def test_neutral_guard_check_has_no_pending(self):
        result=self.cli('--check')
        self.assertEqual(result.returncode,0,(result.stdout,result.stderr))


if __name__=='__main__':unittest.main()

class SelectionPhaseRecoveryPromptTests(unittest.TestCase):
    def test_selection_recovery_is_readonly_triage_not_coding(self):
        import importlib.util
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location('selection_wrapper',str(WRAPPER))
        mod=importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
             'AUTONOMY_PHASE':'selection',
             'AUTONOMY_PROJECT_ROOT':tmp,
             'AUTONOMY_PROJECT_NAME':'repo',
             'AUTONOMY_PENDING_MARKER':tmp+'/pending.json',
             'AUTONOMY_OPENCODE':'/bin/true'}):
            spec.loader.exec_module(mod)
            text=mod.recovery_prompt('PROJECT_RECOVERY_COMPLETE_TEST')
        self.assertIn("For phase 'selection'",text)
        self.assertIn('READ-ONLY OMO backlog triage',text)
        self.assertIn('Never start coding during selection',text)
        self.assertIn('PROJECT_RECOVERY_COMPLETE_TEST',text)
