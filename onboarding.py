#!/usr/bin/env python3
"""Interactive, resumable GitHub onboarding. All runtime state stays outside checkouts."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import fcntl

from bootstrap_policy import VERSION
import release_policy as releases

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT/'config.json'
SPEC = importlib.util.spec_from_file_location('controller', ROOT/'autonomy-controller.py')
c = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(c)


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def gh(*args):
    return c.gh_json(*args)


def repo_url(value):
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+?)(?:\.git)?/?',value.strip(),re.I)
    if not match or match[2] in ('.','..'):
        raise ValueError('Enter a GitHub repository URL, e.g. https://github.com/owner/repository')
    return '/'.join(match.groups())


def identifier(repo):
    return re.sub(r'[^a-z0-9_-]', '-',repo.split('/')[1].lower())[:42].strip('-_')[:42] or 'repository'


def repo_key(repo):
    return identifier(repo)+'-'+hashlib.sha256(repo.lower().encode()).hexdigest()[:12]


def opencode_binary():
    binary = shutil.which('opencode') or str(Path.home()/'.opencode/bin/opencode')
    if not Path(binary).is_file() or not os.access(binary,os.X_OK):
        raise ValueError('Install and configure OpenCode V2 and OMO Slim before onboarding.')
    return str(Path(binary).absolute())


def runtime_preflight(binary):
    # No model request: use the same API contract as the existing safe wrapper.
    raw=c.command([binary,'api','GET','/api/info'],timeout=30)
    info=json.loads(raw); info=info.get('data',info)
    if not str(info.get('version','')).startswith('2.'):
        raise ValueError('A running OpenCode V2 service is required by the current safe wrapper.')
    # The V2 agent.get endpoint resolves the actual registered agent. Do not
    # start a paid probe or silently fall back to a built-in agent.
    for role in ('orchestrator','fixer','oracle'):
        try:
            agent=json.loads(c.command([binary,'api','GET',f'/api/agent/{role}'],timeout=30))
            if not isinstance(agent.get('data',agent),dict): raise ValueError('invalid agent response')
        except (ValueError,c.ControllerError) as exc:
            raise ValueError(f'OMO Slim role {role} is unavailable in the OpenCode V2 service.') from exc
    print('OpenCode V2 API and the three required OMO roles are reachable.')


def authorize():
    if os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'):
        raise ValueError('Unset temporary GH_TOKEN/GITHUB_TOKEN; onboarding needs persistent gh login for the background service.')
    if subprocess.run(['gh','auth','status','--hostname','github.com'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        run('gh','auth','login','--hostname','github.com','--git-protocol','https','--web','--scopes','project,workflow')
    # Scope header contains only scope names. Never read/print the token.
    headers=c.command(['gh','api','-i','user'],timeout=30)
    scope_line=next((line for line in headers.splitlines() if line.lower().startswith('x-oauth-scopes:')), '')
    scopes={v.strip() for v in scope_line.partition(':')[2].split(',')}
    if not {'repo','project','workflow'}.issubset(scopes):
        run('gh','auth','refresh','--hostname','github.com','--scopes','repo,project,workflow')
    run('gh','auth','setup-git','--hostname','github.com')
    identity=gh('api','user')
    if not identity.get('login') or not identity.get('id'): raise ValueError('Could not resolve GitHub account')
    return identity


def repo_details(repo):
    info=gh('api',f'repos/{repo}')
    permissions=info.get('permissions') or {}
    if info.get('archived') or info.get('disabled') or not permissions.get('push'):
        raise ValueError('Repository is archived, disabled, or lacks write access.')
    if not info.get('has_issues'):
        if not permissions.get('admin'): raise ValueError('Issues are disabled and this account cannot enable them.')
        info=gh('api','--method','PATCH',f'repos/{repo}','-F','has_issues=true')
    canonical=info['full_name']; branch=info.get('default_branch') or 'main'
    if not branch or branch.startswith('-'): raise ValueError('Invalid default branch')
    method=next((method for method, field in (('squash','allow_squash_merge'),('merge','allow_merge_commit'),('rebase','allow_rebase_merge')) if info.get(field)),None)
    if not method: raise ValueError('Repository has no supported merge method.')
    return info,canonical,branch,method


def create_checkout(repo, branch, directory, user):
    directory.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    expected=f'https://github.com/{repo}.git'
    if directory.exists():
        if not (directory/'.git').is_dir() or directory.is_symlink(): raise ValueError('Existing checkout path is not a managed Git repository')
        # Read the stored origin, not `remote get-url`: a global insteadOf rewrite
        # (mirror/proxy) would make get-url return the rewritten address and refuse
        # a checkout that onboarding itself cloned.
        remote=c.command(['git','-C',str(directory),'config','--get','remote.origin.url']).strip()
        if repo_url(remote).lower()!=repo.lower(): raise ValueError('Checkout origin does not match repository')
        if c.command(['git','-C',str(directory),'status','--porcelain']).strip(): raise ValueError('Existing checkout has local work; refusing to overwrite it')
    else:
        run('git','clone','--',expected,str(directory))
    # Configure identity only in this managed checkout, not globally.
    email=user.get('email') or f"{user['id']}+{user['login']}@users.noreply.github.com"
    run('git','config','--local','user.name',user.get('name') or user['login'],cwd=directory)
    run('git','config','--local','user.email',email,cwd=directory)
    # Empty repos need a base ref before a bootstrap PR can be created.
    refs=c.command(['git','-C',str(directory),'ls-remote','--refs','origin']).strip()
    local_head=subprocess.run(['git','-C',str(directory),'rev-parse','--verify','HEAD'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if not refs and not local_head:
        run('git','check-ref-format','--branch',branch,cwd=directory)
        run('git','symbolic-ref','HEAD',f'refs/heads/{branch}',cwd=directory)
        (directory/'README.md').write_text(f'# {repo.split("/")[1]}\n\nRepository initialized for its standardization PR.\n')
        run('git','add','--','README.md',cwd=directory)
        run('git','commit','-m','Initialize empty repository',cwd=directory)
        # No force; a concurrent first push wins and setup fails closed.
        run('git','push','--set-upstream','origin',f'HEAD:refs/heads/{branch}',cwd=directory)
    elif not refs and local_head:
        # Resume only our exact seed commit, never push an unrelated checkout.
        msg=c.command(['git','-C',str(directory),'log','-1','--format=%s']).strip()
        count=c.command(['git','-C',str(directory),'rev-list','--count','HEAD']).strip()
        if msg!='Initialize empty repository' or count!='1': raise ValueError('Remote empty but checkout has unrelated commits')
        run('git','push','--set-upstream','origin',f'HEAD:refs/heads/{branch}',cwd=directory)
    else:
        # Onboarding must not overwrite or adopt an arbitrary local branch.
        current=c.command(['git','-C',str(directory),'branch','--show-current']).strip()
        if current!=branch: raise ValueError(f'Checkout is on {current!r}, expected {branch!r}')
        run('git','pull','--ff-only','origin',branch,cwd=directory)
        if c.command(['git','-C',str(directory),'rev-parse','HEAD']).strip()!=c.command(['git','-C',str(directory),'rev-parse','FETCH_HEAD']).strip():
            raise ValueError('Local checkout is ahead of the remote; refusing to adopt it')


def project_for(repo, owner, config):
    title=f'AutoController · {repo}'
    data=gh('project','list','--owner',owner,'--format','json','--limit','1000','--closed')
    projects=data.get('projects')
    if not isinstance(projects,list) or data.get('totalCount',len(projects))>len(projects): raise ValueError('Project list truncated; refusing possible duplicate')
    matches=[p for p in projects if p.get('title')==title]
    if len(matches)>1 or (matches and matches[0].get('closed')): raise ValueError('Ambiguous or closed onboarding Project; resolve in GitHub')
    p=matches[0] if matches else gh('project','create','--owner',owner,'--title',title,'--format','json')
    number=int(p['number'])
    if any(str(x.get('project_owner','')).lower()==owner.lower() and x.get('project_number')==number for x in config['projects']):
        raise ValueError('Project is already assigned to another repository')
    project=c.GitHubProject(repo,owner,number)
    # Reject contamination by another repository BEFORE linking or any other
    # mutation (ordinary controller filters foreign issues but onboarding never
    # adopts a shared project).
    raw=gh('project','item-list',str(number),'--owner',owner,'--format','json','--limit','1000')
    rows=raw.get('items')
    if not isinstance(rows,list) or raw.get('totalCount',len(rows))>len(rows): raise ValueError('Project item list incomplete')
    for item in rows:
        content=item.get('content') or {}
        if content.get('type','').lower()=='issue' and not c.normalize_project_item(item,repo):
            raise ValueError('Existing Project contains issues from another repository')
    run('gh','project','link',str(number),'--owner',owner,'--repo',repo)
    project.ensure_metadata()
    return project,p.get('url') or gh('project','view',str(number),'--owner',owner,'--format','json')['url']


TRUSTED_ASSOCIATIONS={'OWNER','MEMBER','COLLABORATOR'}


def find_or_create_bootstrap(repo, number):
    title='Repository bootstrap: Golden Standard v'+VERSION
    marker=f'<!-- autocontroller-bootstrap:v{VERSION} -->'
    issues=c.gh_api_pages(f'repos/{repo}/issues?state=all&per_page=100',max_items=10000)
    # Marker text is attacker-controlled on a public repository. Only issues written
    # by trusted members may be adopted as the bootstrap; outsider copies are ignored.
    matches=[i for i in issues if not i.get('pull_request') and marker in (i.get('body') or '')
             and str(i.get('author_association') or '').upper() in TRUSTED_ASSOCIATIONS]
    if len(matches)>1 or (matches and matches[0].get('state')!='open'):
        raise ValueError('Multiple or closed bootstrap issues found; refusing to assume a previous bootstrap was verified')
    if matches: return int(matches[0]['number'])
    body=(marker+'\n\nFirst required engineering task for this repository. Adapt repository-specific agent contracts, reproducible mise tooling, required PR/default-branch CI, native release qualification and Renovate to the packaged golden standard. Preserve existing features and protections. No unrelated changes, direct merges, release publication or fabricated evidence. Finish through one independently reviewed PR with genuine tests. The controller supplies the authoritative versioned standard in its work prompt.')
    i=gh('api','--method','POST',f'repos/{repo}/issues','-f',f'title={title}','-f',f'body={body}')
    return int(i['number'])


def import_issues(project):
    """Import only issues written by trusted repository members.

    Public repositories accept issues from anyone; an outsider's issue must never
    be auto-scheduled. Missing or unknown author_association fails closed.
    """
    seen={i['number'] for i in project.items()}
    issues=c.gh_api_pages(f'repos/{project.repo}/issues?state=open&per_page=100',max_items=10000)
    for issue in issues:
        if issue.get('pull_request') or issue['number'] in seen or releases.is_renovate_identity(issue.get('user')): continue
        if str(issue.get('author_association') or '').upper() not in TRUSTED_ASSOCIATIONS: continue
        run('gh','project','item-add',str(project.number),'--owner',project.owner,'--url',issue['html_url'])


def onboard(url=None):
    binary=opencode_binary(); runtime_preflight(binary)
    user=authorize()
    requested=repo_url(url or input('GitHub repository URL: '))
    config=c.json_read(CONFIG,c.json_read(ROOT/'config.example.json'))
    existing=next((x for x in config['projects'] if x['repo'].lower()==requested.lower()),None)
    if existing:
        if not existing.get('enabled',True): raise ValueError('Repository already configured but disabled; refusing to change its state.')
        print('Repository already configured. Resuming service.'); run('systemctl','--user','enable','--now','autocontroller.service'); return
    # Show the material authorization once, before any remote mutation.
    print(f'AutoController will clone {requested}, create a dedicated GitHub Project, import open issues authored by the owner, members or collaborators, create a bootstrap issue, and autonomously run AI coding sessions and merge reviewed green PRs. GitHub branch protection remains enforced. An empty repository will receive a minimal first commit. AI and CI can incur costs.')
    if input('Enable autonomous development? [y/N] ').strip().lower() not in ('y','yes'): raise ValueError('Setup cancelled; no repository mutation performed.')
    info,repo,branch,method=repo_details(requested)
    if any(x['repo'].lower()==repo.lower() for x in config['projects']): raise ValueError('Repository already configured under canonical identity')
    name=repo_key(repo); directory=Path.home()/'repositories'/name
    create_checkout(repo,branch,directory,user)
    owner=info['owner']['login']
    project,project_url=project_for(repo,owner,config)
    bootstrap=find_or_create_bootstrap(repo,project.number)
    # Onboarding created or verified this issue itself, so it admits it explicitly
    # instead of depending on the author_association import rule. Idempotent on resume.
    if not any(i['number']==bootstrap for i in project.items()):
        run('gh','project','item-add',str(project.number),'--owner',project.owner,'--url',f'https://github.com/{repo}/issues/{bootstrap}')
    import_issues(project)
    if not any(i['number']==bootstrap for i in project.items()): raise ValueError('Bootstrap is not present in the dedicated Project')
    cfg={'name':name,'enabled':True,'repo':repo,'directory':str(directory),'base_branch':branch,
         'project_owner':owner,'project_number':project.number,'required_check':'required',
         'required_workflow':'required.yml','merge_method':method,'bootstrap_issue':bootstrap,'standard_version':VERSION,
         'release':{'enabled':True,'approver':user['login'],'workflow':'release.yml','strategy':'tag_workflow','maintenance_days':14,'maintenance_min_renovate_prs':3,'required_assets':[]}}
    config.setdefault('state_dir',str(ROOT/'state'));config.setdefault('wrapper',str(ROOT/'opencode-project-safe.py'));config.setdefault('opencode',binary)
    config['projects'].append(cfg)
    c.validate_config(config)
    # Adding a repository restarts the service via its graceful checkpoint logic.
    # Existing production configuration is never overwritten by install.sh.
    c.json_atomic(CONFIG,config)
    run('systemctl','--user','enable','autocontroller.service')
    run('systemctl','--user','restart','autocontroller.service')
    print(f'\nOnboarded {repo}. Bootstrap issue: https://github.com/{repo}/issues/{bootstrap}\nBacklog: {project_url}\nEnable Renovate for this repository: https://github.com/apps/renovate\nUse autocontroller status and autocontroller logs to monitor startup. Add future real GitHub Issues to this Project; no manual Ready state is needed.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',default='add',choices=['add','status','logs','doctor','memory','start','stop'])
    parser.add_argument('url',nargs='?')
    args=parser.parse_args()
    os.umask(0o077)
    if args.action=='status': run('systemctl','--user','status','autocontroller.service','--no-pager');return
    if args.action=='logs': run('journalctl','--user','-u','autocontroller.service','-n','50','-f');return
    if args.action in ('start','stop'): run('systemctl','--user',args.action,'autocontroller.service');return
    if args.action=='memory': run(sys.executable,str(ROOT/'memory.py'),'--health');return
    if args.action=='doctor':
        runtime_preflight(opencode_binary());run(sys.executable,str(ROOT/'autonomy-controller.py'),'--check');return
    with (ROOT/'onboarding.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        onboard(args.url)


if __name__=='__main__':
    try: main()
    except (ValueError, OSError, RuntimeError, c.ControllerError, subprocess.CalledProcessError) as exc:
        print(f'AutoController: {exc}',file=sys.stderr);sys.exit(1)
