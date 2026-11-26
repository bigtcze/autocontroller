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
from urllib.parse import quote

from bootstrap_policy import VERSION
import release_policy as releases
import lifecycle

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
        # Exactly one origin: `--get` would silently take the last of several values.
        origins=[v.strip() for v in c.command(['git','-C',str(directory),'config','--get-all','remote.origin.url']).splitlines() if v.strip()]
        if len(origins)!=1: raise ValueError('Checkout must have exactly one origin URL; refusing ambiguous origin')
        if repo_url(origins[0]).lower()!=repo.lower(): raise ValueError('Checkout origin does not match repository')
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


REQUIRED_CHECK='required'
RULESET_NAME='AutoController required check'
# GitHub Actions app: the only check source onboarding asks a ruleset to accept.
GITHUB_ACTIONS_INTEGRATION_ID=15368


def _unverified(branch):
    return ValueError(f"Default branch '{branch}' protection could not be verified unambiguously. Owner action: check branch protection and rulesets for '{branch}' in GitHub so status check 'required' is required, then rerun onboarding. Nothing was changed.")


def _classic_enforces(summary, branch, check, admin):
    if summary['protected'] is False:
        return False
    protection=summary.get('protection')
    rsc=protection.get('required_status_checks') if isinstance(protection,dict) else None
    if not isinstance(rsc,dict): raise _unverified(branch)
    contexts=rsc.get('contexts',[]); checks=rsc.get('checks',[]); level=rsc.get('enforcement_level')
    if not isinstance(contexts,list) or not isinstance(checks,list) or not isinstance(level,str): raise _unverified(branch)
    names=set()
    for name in contexts:
        if not isinstance(name,str): raise _unverified(branch)
        names.add(name)
    for entry in checks:
        if not isinstance(entry,dict) or not isinstance(entry.get('context'),str): raise _unverified(branch)
        names.add(entry['context'])
    if check not in names: return False
    # Classic 'non_admins' exempts admins, so it cannot be relied on for an admin account.
    return level=='everyone' or (level=='non_admins' and not admin)


def _rulesets_enforce(repo, branch, check, enc):
    enforcing=False
    for rule in c.gh_api_pages(f'repos/{repo}/rules/branches/{enc}?per_page=100',max_items=1000):
        if not isinstance(rule,dict) or not isinstance(rule.get('type'),str): raise _unverified(branch)
        if rule['type']!='required_status_checks': continue
        # Rule objects carry the ruleset they came from (GitHub's rules/branches response shape).
        rid=rule.get('ruleset_id')
        if not isinstance(rid,int) or isinstance(rid,bool): raise _unverified(branch)
        params=rule.get('parameters')
        rows=params.get('required_status_checks') if isinstance(params,dict) else None
        if not isinstance(rows,list): raise _unverified(branch)
        names=[]
        for entry in rows:
            if not isinstance(entry,dict) or not isinstance(entry.get('context'),str): raise _unverified(branch)
            names.append(entry['context'])
        if check not in names: continue
        rs=gh('api',f'repos/{repo}/rulesets/{rid}')
        bypass=rs.get('current_user_can_bypass') if isinstance(rs,dict) else None
        if not isinstance(bypass,str): raise _unverified(branch)
        # 'always', 'pull_requests_only' and 'exempt' let this account skip the check.
        if rs.get('enforcement')=='active' and bypass=='never': enforcing=True
    return enforcing


def protection_state(repo, branch, check, admin):
    """Read-only: 'enforced' when classic protection or an active ruleset requires `check` for this account."""
    enc=quote(branch,safe='')
    summary=gh('api',f'repos/{repo}/branches/{enc}')
    if not isinstance(summary,dict) or not isinstance(summary.get('protected'),bool): raise _unverified(branch)
    classic=_classic_enforces(summary,branch,check,admin)
    rulesets=_rulesets_enforce(repo,branch,check,enc)
    return 'enforced' if classic or rulesets else 'missing'


def add_required_check_ruleset(repo, branch, check):
    """The only mutation onboarding makes to protection: one additive POST. Existing rules are never edited."""
    body={'name':RULESET_NAME,'target':'branch','enforcement':'active','bypass_actors':[],
          'conditions':{'ref_name':{'include':[f'refs/heads/{branch}'],'exclude':[]}},
          'rules':[{'type':'required_status_checks','parameters':{'strict_required_status_checks_policy':False,
                    'do_not_enforce_on_create':False,
                    'required_status_checks':[{'context':check,'integration_id':GITHUB_ACTIONS_INTEGRATION_ID}]}}]}
    proc=subprocess.run(['gh','api','--method','POST',f'repos/{repo}/rulesets','--input','-'],
                        input=json.dumps(body),text=True,capture_output=True,timeout=120)
    if proc.returncode:
        detail=(proc.stderr or '').strip()[:400]
        raise ValueError(f"GitHub refused to add ruleset '{RULESET_NAME}' on '{branch}' ({detail}). Owner action: enable rulesets for this repository (plan or visibility may not allow them), or require status check '{check}' manually, then rerun onboarding.")


def ensure_required_check(repo, branch, check, permissions, ask=input):
    """Fail closed unless the default branch requires `check`. Adds one ruleset only with explicit consent and admin rights."""
    admin=bool(permissions.get('admin'))
    if protection_state(repo,branch,check,admin)=='enforced':
        print(f"Default branch '{branch}' enforces required check '{check}'.")
        return
    if not admin:
        raise ValueError(f"Default branch '{branch}' does not enforce status check '{check}' for this account. Owner action: ask a repository admin to require it (branch protection that includes admins, or an active ruleset without bypass for this account), then rerun onboarding. Nothing was changed.")
    # A same-named ruleset that is not enforcing was disabled or changed by the owner; never edit it.
    owned=[r for r in c.gh_api_pages(f'repos/{repo}/rulesets?includes_parents=false&per_page=100') if isinstance(r,dict) and r.get('name')==RULESET_NAME]
    if owned: raise ValueError(f"Ruleset '{RULESET_NAME}' exists but does not enforce '{check}' on '{branch}'. Onboarding will not modify it. Owner action: re-enable it with no bypass for this account, or delete it, then rerun onboarding.")
    prompt=(f"Default branch '{branch}' does not require status check '{check}'. Onboarding can add a new repository ruleset "
            f"named '{RULESET_NAME}' on refs/heads/{branch} that requires '{check}' from GitHub Actions, with no bypass actors. "
            "Existing branch protection and rulesets stay unchanged. Add it? [y/N] ")
    try:
        answer=ask(prompt)
    except EOFError:
        answer=''
    if str(answer).strip().lower() not in ('y','yes'):
        raise ValueError(f"Default branch '{branch}' is not enforced; owner declined adding protection. Nothing was changed. Owner action: require status check '{check}' on '{branch}' in GitHub, then rerun onboarding.")
    add_required_check_ruleset(repo,branch,check)
    if protection_state(repo,branch,check,admin)!='enforced':
        raise ValueError(f"Ruleset '{RULESET_NAME}' was submitted but GitHub does not show '{branch}' enforcing '{check}'. Owner action: inspect rulesets for this repository in GitHub, then rerun onboarding.")
    print(f"Default branch '{branch}' enforces required check '{check}'.")


TRUSTED_ASSOCIATIONS={'OWNER','MEMBER','COLLABORATOR'}


def find_or_create_bootstrap(repo, number, user):
    title='Repository bootstrap: Golden Standard v'+VERSION
    marker=f'<!-- autocontroller-bootstrap:v{VERSION} -->'
    issues=c.gh_api_pages(f'repos/{repo}/issues?state=all&per_page=100',max_items=10000)
    viewer_id=user.get('id')
    def own(issue):
        # The account that onboarding authenticated as may have a non-trusted association
        # (for example a private-org member shown as CONTRIBUTOR). Its own marker issue is
        # matched by numeric account id, which an outsider cannot reproduce.
        author=issue.get('user') or {}
        return viewer_id is not None and author.get('id') is not None and str(author['id'])==str(viewer_id)
    # Marker text is attacker-controlled on a public repository. Only issues written
    # by trusted members, or by this exact authenticated account, may be adopted.
    matches=[i for i in issues if not i.get('pull_request') and marker in (i.get('body') or '')
             and (str(i.get('author_association') or '').upper() in TRUSTED_ASSOCIATIONS or own(i))]
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
    print(f'AutoController will clone {requested}, create a dedicated GitHub Project, import open issues authored by the owner, members or collaborators, create a bootstrap issue, and autonomously run AI coding sessions and merge reviewed green PRs. Onboarding verifies that the default branch requires the \'required\' status check and adds a ruleset only with separate consent. Existing branch protection is never weakened. An empty repository will receive a minimal first commit. AI and CI can incur costs.')
    if input('Enable autonomous development? [y/N] ').strip().lower() not in ('y','yes'): raise ValueError('Setup cancelled; no repository mutation performed.')
    info,repo,branch,method=repo_details(requested)
    if any(x['repo'].lower()==repo.lower() for x in config['projects']): raise ValueError('Repository already configured under canonical identity')
    name=repo_key(repo); directory=Path.home()/'repositories'/name
    create_checkout(repo,branch,directory,user)
    # Controller-owned gate: work is unblocked only once the default branch requires the check.
    ensure_required_check(repo,branch,REQUIRED_CHECK,info.get('permissions') or {})
    owner=info['owner']['login']
    project,project_url=project_for(repo,owner,config)
    bootstrap=find_or_create_bootstrap(repo,project.number,user)
    # Onboarding created or verified this issue itself, so it admits it explicitly
    # instead of depending on the author_association import rule. Idempotent on resume.
    if not any(i['number']==bootstrap for i in project.items()):
        run('gh','project','item-add',str(project.number),'--owner',project.owner,'--url',f'https://github.com/{repo}/issues/{bootstrap}')
    import_issues(project)
    if not any(i['number']==bootstrap for i in project.items()): raise ValueError('Bootstrap is not present in the dedicated Project')
    cfg={'name':name,'enabled':True,'repo':repo,'directory':str(directory),'base_branch':branch,
         'project_owner':owner,'project_number':project.number,'required_check':REQUIRED_CHECK,
         'required_workflow':'required.yml','merge_method':method,'bootstrap_issue':bootstrap,'standard_version':VERSION,
         'release':{'enabled':True,'approver':user['login'],'workflow':'release.yml','strategy':'tag_workflow','maintenance_days':14,'maintenance_min_renovate_prs':3,'required_assets':[],'blocker_label':'release-blocker'}}
    config.setdefault('state_dir',str(ROOT/'state'));config.setdefault('wrapper',str(ROOT/'opencode-project-safe.py'));config.setdefault('opencode',binary)
    config['projects'].append(cfg)
    c.validate_config(config)
    # Adding a repository restarts the service via its graceful checkpoint logic.
    # Existing production configuration is never overwritten by install.sh.
    c.json_atomic(CONFIG,config)
    run('systemctl','--user','enable','autocontroller.service')
    run('systemctl','--user','restart','autocontroller.service')
    print(f'\nOnboarded {repo}. Bootstrap issue: https://github.com/{repo}/issues/{bootstrap}\nBacklog: {project_url}\nEnable Renovate for this repository: https://github.com/apps/renovate\nUse autocontroller status and autocontroller logs to monitor startup. Add future real GitHub Issues to this Project; no manual Ready state is needed.')


def lifecycle_action(action, args):
    """update/rollback: agent refusal first, then confirmation, then locks inside lifecycle.operation."""
    lifecycle.refuse_agent(os.environ, action)
    if action=='rollback':
        if args.tag: raise ValueError('--tag applies to update only')
        if args.recover:
            return lifecycle.recover(ROOT,yes=args.yes,environ=os.environ,drain_timeout=args.drain_timeout,
                                     health_seconds=args.health_seconds)
        return lifecycle.rollback(ROOT,yes=args.yes,environ=os.environ,health_seconds=args.health_seconds,
                                  drain_timeout=args.drain_timeout)
    tag=args.tag
    if tag is not None and not releases.version(tag): raise ValueError('--tag must be a stable vX.Y.Z release')
    config=c.json_read(CONFIG,c.json_read(ROOT/'config.example.json'))
    repo=config.get('update_repository','bigtcze/autocontroller')
    if not isinstance(repo,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',repo):
        raise ValueError('update_repository must be OWNER/REPOSITORY')
    return lifecycle.update(ROOT,repo,gh=lifecycle.LiveGitHub(),tag=tag,yes=args.yes,environ=os.environ,
                            health_seconds=args.health_seconds,drain_timeout=args.drain_timeout)


def unit_and_bin_dirs():
    home=Path.home()
    return home/'.config/systemd/user', home/'.local/bin'


def uninstall_action(args):
    """Guard first (inside lifecycle.uninstall, before any prompt), then the operation."""
    unit_dir,bin_dir=unit_and_bin_dirs()
    return lifecycle.uninstall(ROOT,unit_dir=unit_dir,bin_dir=bin_dir,environ=os.environ,yes=args.yes,purge=args.purge,
                               drain_timeout=args.drain_timeout)


def remove_action(args):
    """Guard first (inside lifecycle.remove, before any prompt), then the operation."""
    if not args.url: raise ValueError('remove needs a repository URL or OWNER/REPOSITORY')
    return lifecycle.remove(ROOT,args.url,environ=os.environ,yes=args.yes,force=args.force,
                            delete_local_data=args.delete_local_data,drain_timeout=args.drain_timeout)


def memory_inspect_action(args):
    """Read-only: state/memory metrics and version; --query performs one GET /search. Never a write."""
    import memory as learning
    if not args.url: raise ValueError('memory inspect needs OWNER/REPOSITORY')
    repo=lifecycle.canonical_repo(args.url)
    try:
        lines=learning.inspect(ROOT/'state',repo,query=args.query,shared=args.shared)
    except (learning.MemoryError,ValueError,OSError,KeyError,TypeError) as exc:
        raise lifecycle.LifecycleError(f'memory inspect could not reach Engram: {exc}') from exc
    print('\n'.join(lines))


def status_action():
    """Read-only summary, then the existing systemctl status output.

    An unreadable unit state prints `service: unknown` and does not raise: status is a report, and the
    systemctl status output below still shows what the unit is doing. Only the summary line changes.
    """
    try:
        active=lifecycle.service_active()
    except lifecycle.LifecycleError:
        active=None
    for line in lifecycle.status_lines(ROOT,active=active): print(line)
    run('systemctl','--user','status','autocontroller.service','--no-pager')


def doctor_action():
    """Existing preflight and controller check, plus install integrity. Exits nonzero on any failure."""
    unit_dir,bin_dir=unit_and_bin_dirs()
    failures,warnings=lifecycle.diagnose(ROOT,unit_dir=unit_dir,bin_dir=bin_dir)
    for line in warnings: print(f'warning: {line}')
    for line in failures: print(f'FAILED: {line}')
    runtime_preflight(opencode_binary()); run(sys.executable,str(ROOT/'autonomy-controller.py'),'--check')
    if failures: raise lifecycle.LifecycleError(f'doctor found {len(failures)} failure(s)')


def _positive_seconds(text: str) -> float:
    """argparse type for timeouts: a finite number greater than zero. NaN and inf would never end a wait."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number of seconds: {text!r}')
    if not (value == value and value not in (float('inf'), float('-inf'))) or value <= 0:
        raise argparse.ArgumentTypeError(f'must be finite and greater than 0: {text!r}')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',default='add',choices=['add','status','logs','doctor','memory','start','stop','update','rollback','uninstall','remove'])
    parser.add_argument('url',nargs='?')
    parser.add_argument('subject',nargs='?',help='memory: "inspect" (read-only)')
    parser.add_argument('--query',help='memory inspect: one read-only search of the project memory')
    parser.add_argument('--shared',action='store_true',help='memory inspect: search the shared scope instead')
    parser.add_argument('--force',action='store_true',help='remove: allow removing a project with an in-flight cycle (state files are kept)')
    parser.add_argument('--delete-local-data',action='store_true',help='remove: also delete the clean, fully pushed checkout and its state/projects entry')
    parser.add_argument('--yes',action='store_true',help='skip the confirmation prompt (update/rollback/uninstall)')
    parser.add_argument('--tag',help='update to this stable vX.Y.Z release instead of the latest')
    parser.add_argument('--health-seconds',type=_positive_seconds,default=30,help='seconds the service must stay active after update/rollback (finite, > 0)')
    parser.add_argument('--purge',action='store_true',help='uninstall: also delete config.json and state/ (requires typing "purge")')
    parser.add_argument('--recover',action='store_true',help='rollback: resolve an interrupted rollback by restoring the release it replaced (rollback/swap)')
    parser.add_argument('--drain-timeout',type=_positive_seconds,default=lifecycle.DEFAULT_DRAIN_TIMEOUT,help='update/rollback/remove/uninstall: seconds to wait for workers to drain before stopping (default 3600; finite, > 0)')
    args=parser.parse_args()
    os.umask(0o077)
    if args.recover and args.action!='rollback':
        raise ValueError('--recover applies to rollback only')
    if args.action in ('update','rollback'):
        print(lifecycle_action(args.action,args)); return
    if args.action=='uninstall':
        print(uninstall_action(args)); return
    if args.action=='remove':
        print(remove_action(args)); return
    if args.action=='memory' and args.url=='inspect':
        # `memory inspect OWNER/REPO`: the subcommand takes the url slot, the repo takes subject.
        args.url=args.subject
        memory_inspect_action(args); return
    if args.action=='memory' and args.url is not None:
        raise ValueError('memory subcommand must be "inspect"')
    if args.action=='status': status_action();return
    if args.action=='logs': run('journalctl','--user','-u','autocontroller.service','-n','50','-f');return
    if args.action in ('start','stop'): run('systemctl','--user',args.action,'autocontroller.service');return
    if args.action=='memory': run(sys.executable,str(ROOT/'memory.py'),'--health');return
    if args.action=='doctor': doctor_action();return
    with (ROOT/'onboarding.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        onboard(args.url)


if __name__=='__main__':
    try: main()
    except (ValueError, OSError, RuntimeError, c.ControllerError, subprocess.CalledProcessError, lifecycle.LifecycleError) as exc:
        print(f'AutoController: {exc}',file=sys.stderr);sys.exit(1)
