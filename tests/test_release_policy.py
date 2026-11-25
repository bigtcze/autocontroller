"""Offline release tests: human gate is only for feature publication."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

DIR=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('controller_releases', DIR/'autonomy-controller.py')
ctl=importlib.util.module_from_spec(spec);spec.loader.exec_module(ctl)
import release_policy as rp

SHA='a'*40
OLD='b'*40
NOW=datetime(2026,11,24,14,0,tzinfo=timezone.utc)
PREV={'tag_name':'v1.2.4','published_at':'2026-11-01T14:00:00Z'}

def pr(n, user='renovate[bot]', labels=None, merged='2026-11-15T00:00:00Z'):
    return {'number':n,'user':{'login':user},'head':{'ref':f'renovate/pkg-{n}' if user=='renovate[bot]' else f'feature/{n}'},
            'merged_at':merged,'base':{'ref':'main'},'labels':[{'name':x} for x in labels or []]}


class ReleasePolicyTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(rp.version('v2.10.0'),(2,10,0))
        self.assertIsNone(rp.version('v1.0.0-rc1'))
    def test_issue_title(self):
        self.assertEqual(rp.release_issue_tag('CH-55 Feature release v1.0.0'),'v1.0.0')
        self.assertIsNone(rp.release_issue_tag('Update release docs'))
    def test_explicit_feature_ticket_is_distinct_from_patch_version(self):
        self.assertTrue(rp.explicitly_feature_title('Feature release v1.2.5'))
        self.assertFalse(rp.explicitly_feature_title('Maintenance release v1.2.5'))
    def test_major_minor_initial_require_approval(self):
        self.assertTrue(rp.is_feature_release('v1.0.0',None))
        self.assertTrue(rp.is_feature_release('v2.0.0','v1.9.9'))
        self.assertTrue(rp.is_feature_release('v1.3.0','v1.2.4'))
    def test_patch_never_requires_approval(self):
        self.assertFalse(rp.is_feature_release('v1.2.5','v1.2.4'))
    def test_reversed_release_ticket_title(self):
        self.assertEqual(rp.release_issue_tag('DEV-52 v5.0.0 Release'), 'v5.0.0')
    def test_due_only_after_fourteen_days(self):
        self.assertFalse(rp.maintenance_due(PREV,datetime(2026,11,14,13,tzinfo=timezone.utc),[pr(1),pr(2),pr(3)]))
        self.assertTrue(rp.maintenance_due(PREV,NOW,[pr(1),pr(2),pr(3)]))
    def test_insufficient_renovate(self):
        self.assertFalse(rp.maintenance_due(PREV,NOW,[pr(1),pr(2)]))
    def test_active_feature_prevents_maintenance(self):
        self.assertFalse(rp.maintenance_due(PREV,NOW,[pr(1),pr(2),pr(3)],feature_active=True))
    def test_unapproved_feature_pr_must_not_ship_via_patch(self):
        self.assertFalse(rp.maintenance_due(PREV,NOW,[pr(1),pr(2),pr(3),pr(5,'example-owner')]))
    def test_bugfix_pr_is_permitted(self):
        self.assertTrue(rp.maintenance_due(PREV,NOW,[pr(1),pr(2),pr(3),pr(6,'example-owner',['bugfix'])]))
    def test_next_patch(self):
        self.assertEqual(rp.next_patch_tag('v1.9.9'),'v1.9.10')
    def test_exact_account_and_sha_required(self):
        request={'id':1,'body':rp.approve_request_body('v2.0.0',SHA),'user':{'login':'autocontroller'}}
        allowed={'id':2,'body':f'{rp.APPROVAL_RESPONSE} v2.0.0 {SHA}','user':{'login':'example-owner'}}
        wrong_sha={'id':3,'body':f'{rp.APPROVAL_RESPONSE} v2.0.0 {OLD}','user':{'login':'example-owner'}}
        wrong_user={'id':4,'body':f'{rp.APPROVAL_RESPONSE} v2.0.0 {SHA}','user':{'login':'renovate[bot]'}}
        self.assertEqual(rp.approval_state([request,wrong_sha,wrong_user],'v2.0.0',SHA,'example-owner'),(True,False))
        self.assertEqual(rp.approval_state([request,allowed],'v2.0.0',SHA,'example-owner'),(True,True))
        self.assertEqual(rp.approval_state([request,allowed],'v2.0.0',OLD,'example-owner'),(False,False))
    def test_old_approval_ignored_after_new_request(self):
        request={'id':1,'body':rp.approve_request_body('v2.0.0',SHA)}
        approved={'id':2,'body':f'{rp.APPROVAL_RESPONSE} v2.0.0 {SHA}','user':{'login':'example-owner'}}
        newer={'id':3,'body':rp.approve_request_body('v2.0.0',SHA)}
        self.assertEqual(rp.approval_state([request,approved,newer],'v2.0.0',SHA,'example-owner'),(True,False))
    def test_search_date_is_filtered_at_timestamp_boundary(self):
        data={'total_count':2,'items':[{'number':1},{'number':2}]}
        def gh(*x):return data
        bynum={1:pr(1,merged='2026-11-01T13:00:00Z'),2:pr(2,merged='2026-11-05T13:00:00Z')}
        rows=rp.merged_since('org/repo','main',PREV['published_at'],gh,lambda repo,n:bynum[n])
        self.assertEqual([p['number'] for p in rows],[2])
    def test_search_truncation_fails_closed(self):
        with self.assertRaises(ValueError):rp.merged_since('org/repo','main',PREV['published_at'],lambda *x:{'total_count':120,'items':[]},None)
    def test_no_previous_release_never_auto_patch(self):
        self.assertFalse(rp.maintenance_due(None,NOW,[pr(1),pr(2),pr(3)]))


class ControllerReleaseTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        self.root=Path(t.name)
        cfg={'name':'demo','repo':'org/demo','directory':str(self.root/'repo'),
             'project_owner':'org','project_number':1,
             'release':{'enabled':True,'approver':'example-owner','workflow':'release.yml','required_assets':['one.tar']}}
        (self.root/'repo').mkdir()
        self.w=ctl.ProjectWorker(cfg,{'state_dir':str(self.root/'state'),'wrapper':'wrapper','opencode':'oc'},threading.Event(),threading.Lock())
        self.item={'number':10,'title':'Feature release v2.0.0','item_id':'x',
                   'status':'In Progress','url':'https://github.com/org/demo/issues/10'}
        self.cp={'kind':'issue','number':10,'url':self.item['url'],'phase':'release-approval',
                 'release_tag':'v2.0.0','release_sha':SHA,'previous_release_tag':'v1.2.4',
                 'pr_number':20,'reviewed_sha':OLD,'merge_commit':SHA}
        self.w.save_checkpoint(self.cp)
    def test_approval_request_is_idempotent(self):
        calls=[]
        def api(*args,**kw):
            calls.append(args)
            return {'id':9} if 'POST' in args else {}
        with patch.object(ctl,'gh_api_pages',return_value=[]),patch.object(ctl,'gh_json',side_effect=api),patch.object(self.w,'release_latest',return_value=PREV),patch.object(ctl,'issue_state',return_value='open'):
            self.w.publish_release_cycle(dict(self.cp),'issue',10,self.item)
        self.assertEqual(len([x for x in calls if 'POST' in x]),1)
        # With a durable request, retries must never submit duplicate comments.
        marked={'id':11,'body':rp.approve_request_body('v2.0.0',SHA)}
        with patch.object(ctl,'gh_api_pages',return_value=[marked]),patch.object(ctl,'gh_json',side_effect=AssertionError('must not post again')),patch.object(self.w,'release_latest',return_value=PREV):
            self.w.publish_release_cycle(ctl.json_read(self.w.cycle_state),'issue',10,self.item)
        self.assertEqual(ctl.json_read(self.w.cycle_state)['phase'],'release-approval')
    def test_feature_approval_never_triggers_ai(self):
        cp=ctl.json_read(self.w.cycle_state)
        cp['approval_issue_reconciled']=True;self.w.save_checkpoint(cp)
        with patch.object(self.w,'release_latest',return_value=PREV),patch.object(self.w,'release_request',return_value=False),patch.object(self.w,'run_wrapper') as agent:
            self.w._cycle('issue',10,self.item['url'],None)
            agent.assert_not_called()
            self.assertEqual(ctl.json_read(self.w.cycle_state)['phase'],'release-approval')
    def test_shared_ai_quota_does_not_block_existing_human_approval(self):
        cp=ctl.json_read(self.w.cycle_state)
        cp['approval_issue_reconciled']=True;self.w.save_checkpoint(cp)
        with patch.object(self.w,'ensure_unsafe_group_gone'),patch.object(self.w,'quota_until',return_value=1e15),patch.object(self.w,'checkout_neutral',return_value=True),patch.object(self.w,'release_latest',return_value=PREV),patch.object(self.w,'release_request',return_value=True),patch.object(self.w,'run_wrapper') as agent:
            self.w.one_iteration()
            agent.assert_not_called()
            self.assertEqual(ctl.json_read(self.w.cycle_state)['phase'],'release-publish')
    def test_owner_approval_enters_publish_not_new_code(self):
        cp=ctl.json_read(self.w.cycle_state)
        cp['approval_issue_reconciled']=True;self.w.save_checkpoint(cp)
        with patch.object(self.w,'release_latest',return_value=PREV),patch.object(self.w,'release_request',return_value=True),patch.object(self.w,'run_wrapper') as agent:
            self.w._cycle('issue',10,self.item['url'],None)
            self.assertEqual(ctl.json_read(self.w.cycle_state)['phase'],'release-publish')
            agent.assert_not_called()
    def test_maintenance_no_owner_approval(self):
        cp={'kind':'maintenance','number':0,'url':'v1.2.5','phase':'release-publish',
            'release_tag':'v1.2.5','release_sha':SHA,'previous_release_tag':'v1.2.4'}
        self.w.save_checkpoint(cp)
        with patch.object(self.w,'release_latest',return_value=PREV),patch.object(self.w,'ensure_release_pipeline'),patch.object(ctl,'main_ci_state',return_value='passed'),patch.object(rp,'compare_tag_to_sha',return_value=True),patch.object(ctl,'push_annotated_tag') as tag,patch.object(self.w,'release_request') as approval:
            self.w.publish_release_cycle(cp,'maintenance',0,None)
            approval.assert_not_called();tag.assert_called_once_with('org/demo','v1.2.5',SHA)
        self.assertEqual(ctl.json_read(self.w.cycle_state)['phase'],'release-verify')
    def test_verify_waits_for_artifacts(self):
        cp={'kind':'maintenance','number':0,'url':'v1.2.5','phase':'release-verify',
            'release_tag':'v1.2.5','release_sha':SHA,'previous_release_tag':'v1.2.4'}
        with patch.object(self.w,'release_latest',return_value={'tag_name':'v1.2.5'}),patch.object(ctl,'tag_target',return_value=SHA),patch.object(ctl,'published_release',return_value={'assets':[]}),patch.object(self.w,'complete_checkpoint') as done:
            self.w.publish_release_cycle(cp,'maintenance',0,None)
            done.assert_not_called()
    def test_missing_release_notes_file_prevents_tag(self):
        self.w.release_cfg['notes_path']='docs/releases/{tag}.md'
        cp={'kind':'maintenance','number':0,'url':'v1.2.5','phase':'release-publish',
            'release_tag':'v1.2.5','release_sha':SHA,'previous_release_tag':'v1.2.4'}
        with patch.object(self.w,'release_latest',return_value=PREV),patch.object(self.w,'ensure_release_pipeline'),patch.object(ctl,'main_ci_state',return_value='passed'),patch.object(rp,'compare_tag_to_sha',return_value=True),patch.object(ctl,'gh_json',side_effect=ctl.ControllerError('Not Found (HTTP 404)')),patch.object(ctl,'push_annotated_tag') as tag:
            with self.assertRaises(ctl.ControllerError):self.w.publish_release_cycle(cp,'maintenance',0,None)
            tag.assert_not_called()
    def test_native_pipeline_required_before_tag(self):
        cp={'kind':'maintenance','number':0,'url':'v1.2.5','phase':'release-publish',
            'release_tag':'v1.2.5','release_sha':SHA,'previous_release_tag':'v1.2.4'}
        with patch.object(self.w,'release_latest',return_value=PREV),patch.object(self.w,'ensure_release_pipeline',side_effect=ctl.ControllerError('release.yml missing')),patch.object(ctl,'push_annotated_tag') as tag:
            with self.assertRaises(ctl.ControllerError):self.w.publish_release_cycle(cp,'maintenance',0,None)
            tag.assert_not_called()
    def test_tag_idempotent_existing_remote_ref(self):
        with patch.object(ctl,'tag_target',return_value=SHA),patch.object(ctl,'gh_json') as gh:
            ctl.push_annotated_tag('org/demo','v1.2.5',SHA)
            gh.assert_not_called()
    def test_tag_collision_fails_closed(self):
        with patch.object(ctl,'tag_target',return_value=OLD):
            with self.assertRaises(ctl.ControllerError):ctl.push_annotated_tag('org/demo','v1.2.5',SHA)
    def test_maintenance_retry_prevents_spam(self):
        self.w.cycle_state.unlink(missing_ok=True)
        ctl.json_atomic(self.w.retries_file,{'maintenance:0':{'until':1e15,'failures':3}})
        with patch.object(self.w,'ensure_unsafe_group_gone'),patch.object(self.w,'quota_until',return_value=0),patch.object(self.w,'checkout_neutral',return_value=True),patch.object(ctl,'renovate_prs',return_value=[]),patch.object(self.w.project,'items',return_value=[]),patch.object(self.w,'check_maintenance',return_value={'tag':'v1.2.5','renovate_count':4}),patch.object(self.w,'_cycle') as work:
            self.w.one_iteration()
            work.assert_not_called()
    def test_disabled_release_ticket_does_not_block_other_issues(self):
        self.w.cycle_state.unlink(missing_ok=True)
        self.w.release_cfg={'enabled':False}
        cards=[self.item, {'number':11,'title':'Fix UI regression','status':'Backlog',
                           'item_id':'y','url':'https://github.com/org/demo/issues/11'}]
        with patch.object(self.w,'ensure_unsafe_group_gone'),patch.object(self.w,'quota_until',return_value=0), \
             patch.object(self.w,'checkout_neutral',return_value=True), \
             patch.object(ctl,'renovate_prs',return_value=[]), \
             patch.object(self.w.project,'items',return_value=cards), \
             patch.object(self.w,'select_next_issue') as selection:
            self.w.one_iteration()
            selection.assert_called_once()
            self.assertEqual([c['number'] for c in selection.call_args.args[0]],[11])

    def test_technical_release_config_never_requires_approver_if_disabled(self):
        cfg={'projects':[{'name':'demo','repo':'org/demo','project_owner':'org','project_number':1,'directory':'/home/example/repositories/demo','release':{'enabled':False}}]}
        ctl.validate_config(cfg)
    def test_release_skill_is_outside_repo(self):
        skill=DIR/'skills/github-release/SKILL.md'
        self.assertTrue(skill.exists())
        prompt=ctl.work_prompt('org/demo','maintenance',0,'v1.2.5',release_tag='v1.2.5')
        self.assertIn(str(ctl.INSTALL_ROOT/'skills/github-release/SKILL.md'),prompt)
        self.assertIn('NEVER tag or publish releases',prompt)

if __name__=='__main__':unittest.main()
