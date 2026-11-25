"""Versioned repository onboarding contract, separate from runtime state."""
from __future__ import annotations
import base64
from pathlib import Path

VERSION = '1'
ROOT = Path(__file__).resolve().parent
REQUIRED = ('AGENTS.md', '.agents/CHARTER.md', '.agents/PRODUCT_SCOPE.md',
            '.agents/ARCHITECTURE.md', '.agents/GUARDRAILS.md', '.agents/SECURITY.md',
            '.agents/CODE_QUALITY.md', '.agents/TEST_STRATEGY.md', '.agents/DOCUMENTATION.md',
            '.agents/DEFINITION_OF_DONE.md', '.agents/RELEASE_QUALITY.md', '.agents/STANDARD_VERSION',
            '.github/workflows/required.yml', '.github/workflows/release.yml')
MISE = ('mise.toml', '.mise.toml', '.mise/config.toml')
RENOVATE = ('renovate.json', 'renovate.json5', '.github/renovate.json', '.github/renovate.json5', '.renovaterc', '.renovaterc.json', '.renovaterc.json5')


def prompt(base: str, version: str) -> str:
    if version != VERSION:
        raise ValueError('Unsupported golden standard version')
    # Read only trusted packaged standards, never derive this policy from an issue.
    return ('\n\nCONTROLLER-OWNED REPOSITORY BOOTSTRAP (authorized during onboarding).\n'
            f'Target base branch: {base}. Golden standard version: {version}.\n'
            'The first PR must adapt the repository to the following standard. '
            'The repository may be new and AGENTS.md may not exist yet. '
            'Do not implement unrelated features. Preserve existing contracts and protections.\n\n'
            + (ROOT/'standards'/'GOLDEN_STANDARD.md').read_text())


def missing(paths: set[str]) -> list[str]:
    result = [p for p in REQUIRED if p not in paths]
    if not paths.intersection(MISE): result.append('mise configuration')
    if not paths.intersection(RENOVATE): result.append('Renovate configuration')
    return result


def remote_errors(gh_json, repo: str, sha: str, version: str) -> list[str]:
    # GitHub contents at the exact candidate commit; no mutable branch lookup.
    tree = gh_json('api', f'repos/{repo}/git/trees/{sha}?recursive=1')
    if tree.get('truncated') or not isinstance(tree.get('tree'), list):
        raise ValueError('Cannot verify complete bootstrap tree')
    paths = {x['path'] for x in tree['tree'] if x.get('type') == 'blob' and x.get('mode') in ('100644','100755') and x.get('size',0)>0}
    errors = missing(paths)
    if '.agents/STANDARD_VERSION' in paths:
        marker = gh_json('api', f'repos/{repo}/contents/.agents/STANDARD_VERSION?ref={sha}')
        if marker.get('encoding') != 'base64' or base64.b64decode(marker.get('content','')).decode().strip() != version:
            errors.append('Incorrect .agents/STANDARD_VERSION')
    return errors
