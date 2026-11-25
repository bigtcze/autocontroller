"""Offline tests for the Engram policy and lifecycle; upstream binary not mocked as real."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import memory as m

class FakeBackend:
    def __init__(self): self.rows={}; self.next=1
    def save(self, project,key,title,entry):
        row=next((v for v in self.rows.values() if v['project']==project and v['topic_key']==key),None)
        ident=row['id'] if row else self.next
        if not row: self.next+=1
        self.rows[ident]={'id':ident,'project':project,'scope':'project','type':m.TYPE,'topic_key':key,'title':title,'content':json.dumps(entry)}
        return ident
    def search(self, project, query, limit=12):
        return [v for v in self.rows.values() if v['project']==project and query.casefold() in (v['title']+' '+v['content']).casefold()][:limit]
    def get(self, project, ident):
        v=self.rows[ident]
        if v['project']!=project: raise ValueError('wrong scope')
        return v,json.loads(v['content'])

def grant(repo='own/alpha', number=1):
    return {'repo':repo,'kind':'issue','number':number,'used':0,'queries':[],'records':0,'feedback':0,'returned':[]}

def lesson(repo='own/alpha',transferable=False):
    return {'title':'Worktree isolation','trigger':'Delegated worktree commands','lesson':'Verify the working directory before modifying files.','proof':'Reproduced wrong checkout writes and verified corrected working directory.','evidence':f'https://github.com/{repo}/pull/1','transferable':transferable}

class PolicyTests(unittest.TestCase):
    def setUp(self): self.b=FakeBackend()
    def test_scope_case_stable_and_distinct(self):
        self.assertEqual(m.scope('OWNER/REPO'),m.scope('owner/repo'))
        self.assertNotEqual(m.scope('one/repo'),m.scope('two/repo'))
    def test_local_private_and_shared_hypothesis(self):
        m.operation(self.b,grant(),'record',lesson())
        self.assertEqual(m.operation(self.b,grant('own/beta'),'search','worktree'),[])
        self.assertEqual(m.operation(self.b,grant(number=2),'search','worktree')[0]['status'],'candidate')
        m.operation(self.b,grant(),'record',lesson(transferable=True))
        hit=m.operation(self.b,grant('own/beta'),'search','worktree')[0]
        self.assertEqual(hit['scope'],'shared'); self.assertEqual(hit['status'],'candidate')
        self.assertNotIn('own/alpha',json.dumps(hit)); self.assertNotIn('proof',hit)
    def test_cross_repo_confirmation_and_retirement(self):
        m.operation(self.b,grant(),'record',lesson(transferable=True))
        g=grant('own/beta',2); hit=m.operation(self.b,g,'search','worktree')[0]
        f={'id':hit['id'],'scope':'shared','result':'confirmed','evidence':'https://github.com/own/beta/pull/2','reason':'Reproduced and verified the remedy independently.'}
        self.assertEqual(m.operation(self.b,g,'feedback',f)['status'],'shared')
        with self.assertRaises(ValueError): m.operation(self.b,g,'feedback',f)
        g3=grant('own/gamma',3); self.assertEqual(m.operation(self.b,g3,'search','worktree')[0]['status'],'shared')
        f.update(result='contradicted',evidence='https://github.com/own/gamma/pull/3')
        self.assertEqual(m.operation(self.b,g3,'feedback',f)['status'],'retired')
        self.assertEqual(m.operation(self.b,grant('own/delta'),'search','worktree'),[])
    def test_same_task_not_independent(self):
        g=grant();m.operation(self.b,g,'record',lesson());hit=m.operation(self.b,g,'search','worktree')[0]
        with self.assertRaises(ValueError):m.operation(self.b,g,'feedback',{'id':hit['id'],'scope':'local','result':'confirmed','evidence':'https://github.com/own/alpha/pull/1','reason':'Confirmed'})
    def test_feedback_requires_returned_id(self):
        hit=m.operation(self.b,grant(),'record',lesson())
        with self.assertRaises(ValueError):m.operation(self.b,grant('own/beta'),'feedback',{'id':hit['id'],'scope':'local','result':'confirmed','evidence':'https://github.com/own/beta/pull/2','reason':'Confirmed'})
    def test_dedup_preserves_status(self):
        a=m.operation(self.b,grant(),'record',lesson()); g=grant(number=2);m.operation(self.b,g,'search','worktree')
        m.operation(self.b,g,'feedback',{'id':a['id'],'scope':'local','result':'confirmed','evidence':'https://github.com/own/alpha/pull/2','reason':'Verified on independent task.'})
        b=m.operation(self.b,grant(number=3),'record',lesson()); self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.b.rows),1)
        self.assertEqual(self.b.get(m.scope('own/alpha'),a['id'])[1]['status'],'confirmed')
    def test_limits_and_no_repeated_result(self):
        for i in range(10):
            row=lesson();row['lesson']+=str(i);m.operation(self.b,grant(number=i+1),'record',row)
        g=grant(number=20); a=m.operation(self.b,g,'search','worktree'); self.assertLessEqual(len(a),3);self.assertLessEqual(len(json.dumps(a).encode())+1,750)
        self.assertEqual(m.operation(self.b,g,'search','worktree'),[])
        b=m.operation(self.b,g,'search','delegated'); self.assertFalse({x['id'] for x in a}&{x['id'] for x in b})
        for q in ['Verify','commands','modifying','working','files','directory','isolation','more']:
            m.operation(self.b,g,'search',q)
        self.assertLessEqual(g['used'],2000);self.assertLessEqual(len(g['queries']),8)
    def test_reject_sensitive_or_non_owned(self):
        for changes in [{'lesson':'password=secret'},{'evidence':'https://github.com/other/repo/pull/1'},{'lesson':'alpha only','transferable':True},{'proof':''}]:
            data=lesson();data.update(changes)
            with self.assertRaises(ValueError):m.operation(self.b,grant(),'record',data)
        self.assertEqual(self.b.rows,{})
    def test_grant_resume_preserves_budget_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);token=m.grant(p,p,'own/alpha','issue',1,False);g=m.read(p/'memory-grant.json');g['used']=321;m.atomic(p/'memory-grant.json',g)
            self.assertEqual(token,m.grant(p,p,'own/alpha','issue',1,True));self.assertEqual(m.read(p/'memory-grant.json')['used'],321)
            with self.assertRaises(m.MemoryError):m.grant(p,p,'own/alpha','issue',2,True)
            self.assertNotEqual(token,m.grant(p,p,'own/alpha','issue',1,False))

# Lifecycle tests inject upstream transport/start behavior and real local files.
class SocketDirectoryTests(unittest.TestCase):
    def test_socket_location_independent_of_home_and_environment(self):
        with patch.dict(os.environ,{'XDG_RUNTIME_DIR':'/untrusted','ENGRAM_SOCKET':'/untrusted/override.sock'}):
            expected=Path('/run/user')/str(os.getuid())/'autocontroller-memory/engram.sock'
            self.assertEqual(m.socket_path(),expected)
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(m.Runtime(tmp).sock,expected)

    def test_ancestor_group_write_is_refused(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            parent=Path(tmp)/'shared';parent.mkdir(mode=0o700);parent.chmod(0o770)
            leaf=parent/'private';leaf.mkdir(mode=0o700)
            with self.assertRaisesRegex(m.MemoryError,'Unsafe writable Engram socket ancestor'):
                m.validate_socket_directory(leaf/'engram.sock')
            parent.chmod(0o750)
            m.validate_socket_directory(leaf/'engram.sock')

    def test_private_leaf_and_no_symlinks(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            leaf=Path(tmp)/'private';leaf.mkdir(mode=0o700)
            m.validate_socket_directory(leaf/'engram.sock')
            leaf.chmod(0o755)
            with self.assertRaisesRegex(m.MemoryError,'mode 0700'):
                m.validate_socket_directory(leaf/'engram.sock')
            leaf.chmod(0o700); link=Path(tmp)/'link';link.symlink_to(leaf,target_is_directory=True)
            with self.assertRaisesRegex(m.MemoryError,'Unsafe Engram socket directory'):
                m.validate_socket_directory(link/'engram.sock')

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        # Never touch the real /run/user/<uid> socket: it may belong to the
        # production Engram. Use a private 0700 directory instead.
        self.sock_dir=Path(tempfile.mkdtemp(prefix='engram-test-'));self.sock_dir.chmod(0o700)
        self.addCleanup(lambda:__import__('shutil').rmtree(self.sock_dir,ignore_errors=True))
        self.sock_patch=patch.object(m,'socket_path',return_value=self.sock_dir/'engram.sock')
        self.sock_patch.start();self.addCleanup(self.sock_patch.stop)
        self.r=m.Runtime(Path(self.tmp.name))
        self.assertNotIn('/run/user',str(self.r.sock))
        self.addCleanup(self.r.close)
    def old(self):
        p=self.r.root/'gen-0123456789abcdef';p.mkdir();(p/'engram').write_text('old');(p/'data').mkdir()
        with sqlite3.connect(p/'data/engram.db') as c:c.execute('CREATE TABLE knowledge (v TEXT)');c.execute("INSERT INTO knowledge VALUES ('lesson')")
        m.atomic(self.r.active,{'generation':p.name,'version':'v3.1.0'});return p
    def archive(self):
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as tar:
            info=tarfile.TarInfo('engram');data=b'new-binary';info.size=len(data);tar.addfile(info,io.BytesIO(data))
        return stream.getvalue()
    def test_upgrade_candidate_copy_then_atomic_commit(self):
        old=self.old();data=self.archive();release=('v3.2.0','asset','checks','engram.tar.gz')
        def dl(url,max_bytes):return data if url=='asset' else (hashlib.sha256(data).hexdigest()+'  engram.tar.gz\n').encode()
        def start(folder):
            self.assertTrue(old.exists());self.assertEqual(self.r.current(),old)
            with sqlite3.connect(folder/'data/engram.db') as c:self.assertEqual(c.execute('SELECT v FROM knowledge').fetchone(),('lesson',))
            return {'version':'3.2.0'}
        with patch.object(self.r,'download',side_effect=dl),patch.object(self.r,'start_process',side_effect=start):new=self.r.stage(old,release)
        self.assertEqual(self.r.current(),new);self.assertFalse(old.exists())
    def test_checksum_failure_preserves_original(self):
        old=self.old()
        with patch.object(self.r,'download',return_value=b'wrong'):
            with self.assertRaises(m.MemoryError):self.r.stage(old,('v3.2.0','asset','checks','engram.tar.gz'))
        self.assertEqual(self.r.current(),old);self.assertEqual(list(self.r.root.glob('gen-*')),[old])
    def test_failed_migration_preserves_old_database(self):
        old=self.old();data=self.archive()
        def dl(url,max_bytes):return data if url=='asset' else (hashlib.sha256(data).hexdigest()+'  engram.tar.gz\n').encode()
        def broken(folder):
            (folder/'data/engram.db').write_bytes(b'broken');raise m.MemoryError('failed migration')
        with patch.object(self.r,'download',side_effect=dl),patch.object(self.r,'start_process',side_effect=broken):
            with self.assertRaises(m.MemoryError):self.r.stage(old,('v3.2.0','asset','checks','engram.tar.gz'))
        with sqlite3.connect(old/'data/engram.db') as c:self.assertEqual(c.execute('SELECT v FROM knowledge').fetchone(),('lesson',))
        self.assertEqual(self.r.current(),old)
    def test_missing_active_database_fails_closed(self):
        old=self.old();(old/'data/engram.db').unlink()
        with self.assertRaises(m.MemoryError): self.r.current()
    def test_unavailable_update_uses_existing_healthy_version_once_per_boot(self):
        old=self.old()
        with patch.object(self.r,'latest',side_effect=OSError('network')) as latest,patch.object(self.r,'start_process',return_value={'version':'3.1.0'}) as start,patch.object(self.r.api,'health',return_value={'version':'3.1.0'}):
            self.r.startup();self.r.startup();latest.assert_called_once();start.assert_called_with(old)
    def test_first_install_failure_no_fallback(self):
        with patch.object(self.r,'latest',side_effect=OSError('network')):
            with self.assertRaises(m.MemoryError):self.r.startup()
    def test_runtime_failure_marker_blocks_even_if_process_alive(self):
        self.r.proc=Mock();self.r.proc.poll.return_value=None; self.r.failed.touch()
        with self.assertRaises(m.MemoryError):self.r.check()
        self.r.proc=None
    def test_corrupt_db_no_reinitialization(self):
        old=self.old();(old/'data/engram.db').write_bytes(b'corrupted')
        with patch.object(self.r,'latest') as latest,patch.object(self.r,'start_process') as start:
            with self.assertRaises(m.MemoryError):self.r.startup()
            latest.assert_not_called();start.assert_not_called()

class ControllerIntegrationTests(unittest.TestCase):
    def test_memory_gate_failure_and_no_iteration(self):
        spec=importlib.util.spec_from_file_location('memory_controller',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py')); mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Mock();runtime.check.side_effect=m.MemoryError('offline');runtime.failed=Path(tmp)/'failure'
            w=mod.ProjectWorker({'name':'alpha','repo':'own/alpha','directory':tmp,'project_owner':'own','project_number':1},{'state_dir':tmp,'wrapper':'/bin/true','opencode':'/bin/true','_memory_runtime':runtime},threading.Event(),threading.Lock())
            with patch.object(w,'ensure_unsafe_group_gone') as guard:
                with self.assertRaises(m.MemoryError):w.one_iteration()
                guard.assert_not_called()
            self.assertTrue(runtime.failed.exists())
    def test_interrupt_uses_only_owned_sessions_and_preserves_marker(self):
        spec=importlib.util.spec_from_file_location('memory_guard',str(Path(__file__).resolve().parents[1]/'opencode-project-safe.py'));mod=importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'AUTONOMY_PROJECT_ROOT':tmp,'AUTONOMY_PROJECT_NAME':'repo','AUTONOMY_PENDING_MARKER':tmp+'/marker','AUTONOMY_OPENCODE':'/bin/true'}):
            spec.loader.exec_module(mod);mod.STATE.touch()
            with patch.object(mod,'verify_v2_service'),patch.object(mod,'load_marker',return_value={}),patch.object(mod,'resolve_pending_root',return_value='root'),patch.object(mod,'active_sessions',side_effect=[['root','child','foreign'],['foreign']]),patch.object(mod,'root_session_id',side_effect=lambda sid:'root' if sid in ('root','child') else 'foreign'),patch.object(mod,'opencode_api') as api,patch.object(mod.time,'sleep'):
                self.assertEqual(mod.interrupt_owned(),0)
                self.assertEqual([x.args[1] for x in api.call_args_list],['/api/session/root/interrupt','/api/session/child/interrupt']);self.assertTrue(mod.STATE.exists())
