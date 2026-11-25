"""OMO Slim owns model routing and independent review; controller owns GitHub gates."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('omo_controller',str(Path(__file__).resolve().parents[1]/'autonomy-controller.py'))
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class OmoRoleRouting(unittest.TestCase):
    def test_orchestrator_delegates_implementation_to_fixer(self):
        p=mod.work_prompt('org/project','issue',42,'https://github.com/org/project/issues/42')
        self.assertIn('OMO Slim `fixer` subagent',p)
        self.assertIn('Delegate implementation,',p)
        self.assertIn('Do not do routine implementation yourself',p)
        self.assertIn('Closes #42',p)

    def test_oracle_review_is_inside_orchestrator_workflow(self):
        p=mod.work_prompt('org/project','issue',42,'url')
        self.assertIn('INDEPENDENT code review',p)
        self.assertIn('`oracle`',p)
        self.assertIn('`fixer`',p)
        self.assertIn('DRAFT',p)  # native GitHub draft gate
        self.assertIn('Do not create custom',p)
        self.assertNotIn('opus-latest',p)
        self.assertNotIn('haiku-latest',p)
        self.assertFalse(hasattr(mod,'review_prompt'))
        self.assertNotIn('AUTOCONTROLLER_REVIEW_PASS',p)

    def test_review_not_separate_phase_or_proprietary_token(self):
        s=(Path(__file__).resolve().parents[1]/'autonomy-controller.py').read_text()
        self.assertNotIn('phase="review"',s)
        self.assertNotIn('AUTOCONTROLLER_REVIEW_FAIL',s)
        self.assertNotIn('AUTOCONTROLLER_REVIEW_PASS',s)
        self.assertIn('phase="ci"',s)

    def test_ci_or_prior_review_findings_return_to_fixer_and_oracle(self):
        p=mod.work_prompt('org/project','issue',42,'url',
                          'Docker claim false',repair_for=77)
        self.assertIn('`fixer`',p)
        self.assertIn('`oracle`',p)
        self.assertIn('Docker claim false',p)
        self.assertIn('Main CI failed after merging PR #77',p)

    def test_preserve_release_skill_and_native_only(self):
        p=mod.work_prompt('org/project','maintenance',0,'v2.0.1',release_tag='v2.0.1')
        self.assertIn('skills/github-release/SKILL.md',p)
        self.assertIn('NEVER tag or publish releases',p)

if __name__=='__main__':unittest.main()
