"""Shared shape of a project entry as onboarding.py writes it.

Imported by tests/test_admission_and_authority.py (controller-side fixture) and
tests/test_onboarding_integration.py (which asserts that the entry onboarding
really writes equals this shape, so drift between the two is caught).
Stdlib-only and side-effect free.
"""


def onboarded_project_entry(*, name, repo, directory, base_branch, project_owner, project_number,
                            bootstrap_issue, merge_method, approver, standard_version='1'):
    return {
        'name': name, 'enabled': True, 'repo': repo, 'directory': str(directory),
        'base_branch': base_branch, 'project_owner': project_owner, 'project_number': project_number,
        'required_check': 'required', 'required_workflow': 'required.yml', 'merge_method': merge_method,
        'bootstrap_issue': bootstrap_issue, 'standard_version': standard_version,
        'release': {'enabled': True, 'approver': approver, 'workflow': 'release.yml',
                    'strategy': 'tag_workflow', 'maintenance_days': 14,
                    'maintenance_min_renovate_prs': 3, 'required_assets': []},
    }
