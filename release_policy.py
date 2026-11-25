"""GitHub-native release eligibility, human acceptance and publication.

No repository configuration or controller state is created by this module.
All authorizations are bound to an exact release tag and main commit SHA.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Callable

SEMVER = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
RELEASE_ISSUE = re.compile(r'(?i)\b(?:feature\s+)?release\s*[:\-–]?\s*(v\d+\.\d+\.\d+)\b')
RELEASE_ISSUE_REVERSED = re.compile(r'(?i)\b(v\d+\.\d+\.\d+)\s+(?:feature\s+)?release\b')
APPROVAL_REQUEST = 'AUTOCONTROLLER_FEATURE_RELEASE_REQUEST'
APPROVAL_RESPONSE = 'APPROVE FEATURE RELEASE'
RENO_LOGINS = {'renovate[bot]', 'app/renovate', 'renovate'}
TECHNICAL_LABELS = {'dependencies','dependency','maintenance','chore','security','bugfix','bug','fix'}


def version(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER.fullmatch(str(tag or ''))
    return tuple(map(int,match.groups())) if match else None


def release_issue_tag(title: str) -> str | None:
    match = RELEASE_ISSUE.search(title or '') or RELEASE_ISSUE_REVERSED.search(title or '')
    return match.group(1).lower() if match else None


def explicitly_feature_title(title: str) -> bool:
    return bool(re.search(r'(?i)\bfeature\s+release\b', title or ''))


def is_feature_release(tag: str, previous_tag: str | None) -> bool:
    current = version(tag)
    prior = version(previous_tag) if previous_tag else None
    if not current:
        return False
    if prior is None:
        return True  # Initial release always needs owner acceptance.
    return current[:2] > prior[:2]


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(timezone.utc)


def released_since(release: dict, now: datetime) -> float:
    return (now-parse_date(release['published_at'])).total_seconds()/86400


def latest_release(repo: str, pages: Callable) -> dict | None:
    releases=pages(f'repos/{repo}/releases?per_page=100')
    valid=[x for x in releases if isinstance(x,dict) and not x.get('draft')
           and not x.get('prerelease') and version(x.get('tag_name')) and x.get('published_at')]
    return max(valid,key=lambda x:(parse_date(x['published_at']),version(x['tag_name']))) if valid else None


def latest_main_sha(repo: str, base: str, gh_json: Callable) -> str:
    data=gh_json('api',f'repos/{repo}/branches/{base}')
    sha=((data.get('commit') or {}).get('sha') or '')
    if not re.fullmatch(r'[a-fA-F0-9]{40}',sha):
        raise ValueError('Unable to determine exact main commit SHA')
    return sha


def compare_tag_to_sha(repo: str, tag: str, sha: str, gh_json: Callable) -> bool:
    data=gh_json('api',f'repos/{repo}/compare/{tag}...{sha}')
    return data.get('status') == 'ahead' and int(data.get('ahead_by',0))>0


def merged_since(repo: str, base: str, published_at: str, gh_json: Callable, fetch_pr: Callable) -> list[dict]:
    # GitHub Search includes date-granular boundaries; check exact merged_at on each result.
    date=parse_date(published_at).strftime('%Y-%m-%d')
    query=f'repo:{repo} is:pr is:merged base:{base} merged:>={date}'
    data=gh_json('api','--method','GET','search/issues','-f',f'q={query}','-f','per_page=100')
    results=data.get('items') if isinstance(data,dict) else None
    if not isinstance(results,list) or int(data.get('total_count',0))>len(results):
        raise ValueError('Merged PR search truncated or unavailable; refusing maintenance release')
    out=[]
    for item in results:
        pr=fetch_pr(repo,int(item['number']))
        if not pr.get('merged_at') or parse_date(pr['merged_at'])<=parse_date(published_at):
            continue
        if (pr.get('base') or {}).get('ref')!=base:
            continue
        out.append(pr)
    return out


def technical_pr(pr: dict) -> bool:
    login=str((pr.get('user') or {}).get('login') or '').lower()
    branch=str((pr.get('head') or {}).get('ref') or '').lower()
    labels={str(x.get('name') or '').lower() for x in (pr.get('labels') or [])}
    return login in RENO_LOGINS or branch.startswith('renovate/') or bool(labels & TECHNICAL_LABELS)


def renovate_pr(pr: dict) -> bool:
    login=str((pr.get('user') or {}).get('login') or '').lower()
    branch=str((pr.get('head') or {}).get('ref') or '').lower()
    return login in RENO_LOGINS or branch.startswith('renovate/')


def maintenance_due(release: dict | None, now: datetime, prs: list[dict],
                    *, days=14, minimum=3, feature_active=False) -> bool:
    if release is None or feature_active or released_since(release,now)<days:
        return False
    # Avoid silently publishing a feature under a patch version.
    if any(not technical_pr(p) for p in prs):
        return False
    return sum(renovate_pr(p) for p in prs)>=minimum


def approve_request_body(tag: str, sha: str) -> str:
    return (f'{APPROVAL_REQUEST} tag={tag} sha={sha}\n\n'
            f'Feature release **{tag}** is ready at commit `{sha}`.\n'
            'Development and required CI are complete; publication is waiting for the owner.\n'
            'To approve this **exact candidate**, add a new GitHub Issue comment consisting of:\n\n'
            f'`{APPROVAL_RESPONSE} {tag} {sha}`\n\n'
            'Any changed candidate SHA requires a new approval. No other task requires approval.')


def approval_state(comments: list[dict],tag: str,sha: str,approver: str) -> tuple[bool,bool]:
    """Return (request exists, owner approved after request); never treat bot text as approval."""
    marker=f'{APPROVAL_REQUEST} tag={tag} sha={sha}'
    requests=[c for c in comments if str(c.get('body') or '').splitlines()[0:1]==[marker]]
    if not requests:
        return False,False
    latest=max(requests,key=lambda c:int(c.get('id') or 0))
    request_id=int(latest.get('id') or 0)
    expected=f'{APPROVAL_RESPONSE} {tag} {sha}'
    approved=any(int(c.get('id') or 0)>request_id and str(c.get('body') or '').strip()==expected
                 and str((c.get('user') or {}).get('login') or '').lower()==approver.lower()
                 for c in comments)
    return True,approved


def next_patch_tag(previous_tag: str) -> str:
    v=version(previous_tag)
    if not v:
        raise ValueError('No stable version for patch release')
    return f'v{v[0]}.{v[1]}.{v[2]+1}'
