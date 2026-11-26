#!/usr/bin/env python3
"""Controller-authorized follow-up Issue admission for the active Issue task.

An agent working on an admitted Issue may create NECESSARY follow-up Issues in that
task's repository and dedicated Project, through this CLI only. The controller issues a
token-bound grant (`followup-grant.json`) for the active task; the CLI refuses unless the
live task is still owned by a live wrapper session. Creation, admission into Todo and
native dependency links are performed by controller-owned code, without an extra AI
session. Every step is journaled (`followups.json`) and idempotent, and an unfinished
entry is completed by the controller without AI (see autonomy-controller reconciliation).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
GRANT_FILE = "followup-grant.json"
JOURNAL_FILE = "followups.json"
LOCK_FILE = "followups.lock"
MARKER = "<!-- autocontroller-followup parent={parent} key={key} -->"
MARKER_WORD = "autocontroller-followup"
MAX_PER_PARENT = 5
MAX_LINKS = 5
MAX_TITLE = 200
MAX_BODY_BYTES = 8000
MAX_REASON = 500
MAX_STDIN_BYTES = 16 * 1024
MAX_ERROR = 400
EXIT_REFUSED = 2   # refused before journaling; nothing was created
EXIT_PENDING = 3   # journaled; the controller completes it
KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# Same terminal set as autonomy-controller.TERMINAL_PROJECT_STATUSES.
TERMINAL_STATUSES = {"done", "completed", "closed", "cancelled", "canceled", "archived"}
# Mirrors the controller's authority rule (.agents/SECURITY.md "Work authorization").
ACTIVE_CYCLE_KINDS = ("issue",)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Ambient tokens would make gh act as another identity; the controller account's stored login is used.
GH_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


class FollowupError(ValueError):
    """Refusal or blocker with one concrete reason."""


# ---------------------------------------------------------------- persistence

def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FollowupError(f"invalid persistent state {path.name}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    """Atomic 0600 JSON write with the same contract as autonomy-controller.json_atomic."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- grant

def grant(state: Path, pdir: Path, repo: str, project_owner: str, project_number: int,
          kind: str, number: int, pending: bool) -> str:
    """Return the task's token. A pending task reuses its grant; another task's grant is refused.

    Raises FollowupError only. The controller converts it to ControllerError at its boundary.
    """
    pdir = Path(pdir)
    path = pdir / GRANT_FILE
    if pending and path.exists():
        obj = read_json(path, {})
        if (obj.get("repo"), obj.get("kind"), obj.get("parent")) != (repo, kind, int(number)):
            raise FollowupError("follow-up grant belongs to another task")
        return str(obj["token"])
    obj = {"token": secrets.token_hex(24), "repo": repo, "project_owner": project_owner,
           "project_number": int(project_number), "parent": int(number), "kind": kind,
           "project_dir": pdir.name}
    write_json(path, obj)
    return obj["token"]


def instruction(pdir: Path, token: str) -> str:
    base = (f"python3 {shlex.quote(str(ROOT / 'followup.py'))} --grant "
            f"{shlex.quote(str(Path(pdir) / GRANT_FILE))} --token {token}")
    return (
        "\nFollow-up Issues: only for NECESSARY work discovered while doing this Issue that is "
        "outside its scope. Never split this Issue's own acceptance criteria into follow-ups; "
        f"at most {MAX_PER_PARENT} per task. Create each one ONLY with: {base} create, passing a "
        "JSON object on stdin with keys key, title, body, reason and optionally blocks and "
        "blocked_by (lists of existing Issue numbers in this repository). Never use gh issue "
        "create or gh project item-add for follow-ups. The command prints the created Issue "
        "number and URL as JSON; list every follow-up in your final report. If the result is "
        "pending (exit 3, \"pending\": true), do NOT re-create with a different key: the controller "
        "completes it, and you may retry the SAME key only. Do not start additional AI sessions "
        "for this."
    )


def check_grant_path(path: Path, state: Path) -> Path:
    """Shape of the grant path, like memory.py: <STATE>/projects/<name>/followup-grant.json."""
    resolved = Path(path).resolve()
    if resolved.name != GRANT_FILE or resolved.parent.parent.name != "projects":
        raise FollowupError("invalid controller grant path")
    if resolved.parent.parent.parent != Path(state).resolve():
        raise FollowupError("grant path is outside this controller's state root")
    return resolved


# ---------------------------------------------------------------- input

def parse_input(raw: str) -> dict:
    if len(raw.encode("utf-8")) > MAX_STDIN_BYTES:
        raise FollowupError("input too large")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FollowupError(f"input is not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise FollowupError("JSON object required")
    allowed = {"key", "title", "body", "reason", "blocks", "blocked_by"}
    unknown = set(doc) - allowed
    if unknown:
        # Repository, Project and parent are never accepted from the agent.
        raise FollowupError("unknown field(s): " + ", ".join(sorted(unknown)))
    key = doc.get("key")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise FollowupError("key must match [a-z0-9][a-z0-9-]{0,47}")
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE or CONTROL.search(title):
        raise FollowupError("title must be non-empty, at most 200 characters, without control characters")
    body = doc.get("body", "")
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise FollowupError("body must be a string of at most 8000 bytes")
    reason = doc.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise FollowupError("reason must be non-empty and at most 500 characters")
    # Agent text must never carry the controller's marker: it would make a foreign body look controller-made.
    for name, text in (("title", title), ("body", body), ("reason", reason)):
        if MARKER_WORD in text.lower():
            raise FollowupError(f"{name} must not contain the reserved word {MARKER_WORD!r}")
    links = {}
    for field in ("blocks", "blocked_by"):
        values = doc.get(field, [])
        if not isinstance(values, list) or len(values) > MAX_LINKS:
            raise FollowupError(f"{field} must be a list of at most 5 Issue numbers")
        if any(type(v) is not int or v <= 0 for v in values):
            raise FollowupError(f"{field} must contain positive integers")
        links[field] = sorted(set(values))
    return {"key": key, "title": title.strip(), "body": body, "reason": reason.strip(), **links}


# ---------------------------------------------------------------- live GitHub

def gh_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in GH_TOKEN_ENV}
    return env


def gh_command(argv: list[str], *, stdin: str | None = None, timeout=120) -> str:
    """Run gh with the controller account's stored login; never shell=True."""
    try:
        proc = subprocess.run(["gh", *argv], env=gh_env(), input=stdin, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FollowupError(f"gh failed: {exc}") from exc
    if proc.returncode:
        raise FollowupError(f"gh rc={proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def gh_json(argv: list[str], stdin: str | None = None) -> Any:
    raw = gh_command(argv, stdin=stdin)
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise FollowupError(f"gh produced invalid JSON for {argv[:2]}") from exc


def marker(parent: int, key: str) -> str:
    return MARKER.format(parent=parent, key=key)


def issue_number_from_url(url: str, repo: str) -> int:
    match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/issues/(\d+)", url.strip())
    if not match:
        raise FollowupError(f"gh did not return an Issue URL in {repo}: {url[:120]!r}")
    return int(match.group(1))


# ---------------------------------------------------------------- journal

STATES = ("intent", "created", "added", "todo", "done")
READY_STATUSES = ("todo", "to do")


class Journal:
    """followups.json in the project's state directory. Each step is persisted before the next."""

    def __init__(self, pdir: Path):
        self.pdir = Path(pdir)
        self.path = self.pdir / JOURNAL_FILE

    def load(self) -> list[dict]:
        data = read_json(self.path, {"entries": []})
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise FollowupError("invalid persistent state followups.json")
        return entries

    def save(self, entries: list[dict]) -> None:
        write_json(self.path, {"entries": entries})


def journal_lock(pdir: Path):
    """Exclusive flock on followups.lock; the controller and the CLI serialize on it."""
    pdir = Path(pdir)
    pdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = os.fdopen(os.open(pdir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600), "r+")

    class _Held:
        def __enter__(self):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            return False

    return _Held()


# ---------------------------------------------------------------- live steps (all gh, all idempotent)

def viewer_identity() -> tuple[str, int]:
    """Login and numeric account id of the controller account. Logins are text; ids are identity."""
    obj = gh_json(["api", "user"]) or {}
    login = str(obj.get("login") or "")
    viewer_id = obj.get("id")
    if not login or type(viewer_id) is not int or viewer_id <= 0:
        raise FollowupError("cannot determine the controller account identity")
    return login, viewer_id


MAX_LIST_PAGES = 50
LIST_SLACK = timedelta(hours=24)  # clock-skew margin; identity is still checked by numeric id


def list_since(repo: str, since_iso: str) -> list[dict]:
    """Issues updated since `since_iso`, all pages, bounded by MAX_LIST_PAGES.

    Identity is the numeric id checked on each row, never the login, so the server-side
    creator filter is not relied on. `since` uses a generous margin (24h) because the
    intent timestamp comes from this host's clock and GitHub's clock may differ; the
    margin is a clock-skew allowance, not a correctness boundary. The page bound is a
    safety ceiling for a runaway response, not a normal-path refusal.
    """
    path = f"repos/{repo}/issues?state=all&since={since_iso}&per_page=100"
    raw = gh_command(["api", "--method", "GET", path, "--paginate", "--slurp"], timeout=240)
    try:
        pages = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise FollowupError("gh produced invalid JSON for issue listing") from exc
    if not isinstance(pages, list) or len(pages) > MAX_LIST_PAGES:
        raise FollowupError("issue listing exceeded its safety bound; refusing to guess")
    rows: list[dict] = []
    for page in pages:
        if not isinstance(page, list):
            raise FollowupError("gh returned a non-array issue page")
        rows.extend(page)
    return rows


def since_iso(intent_at: str) -> str:
    when = datetime.fromisoformat(intent_at) - LIST_SLACK
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_controller_issue(obj: dict, repo: str, viewer_id: int) -> bool:
    """Authorship by numeric id, never by login text; a follow-up is never a pull request."""
    user = obj.get("user") if isinstance(obj.get("user"), dict) else {}
    return (type(user.get("id")) is int and user.get("id") == viewer_id
            and "pull_request" not in obj
            and str(obj.get("html_url") or "").startswith(f"https://github.com/{repo}/issues/"))


def find_marked(entry: dict) -> dict | None:
    """Adopt a follow-up created by an earlier interrupted attempt.

    Match: exact marker in the body, the controller's numeric id, not a pull request.
    Identity was recorded at intent time, so a login rename cannot change who owns it.
    """
    needle = marker(entry["parent"], entry["key"])
    rows = list_since(entry["repo"], since_iso(entry["intent_at"]))
    # Exact marker at the start of the body, and the controller's numeric id; never login text.
    hits = [r for r in rows
            if str(r.get("body") or "").startswith(needle)
            and is_controller_issue(r, entry["repo"], entry["viewer_id"])]
    if len(hits) > 1:
        raise FollowupError("several Issues carry this follow-up marker; resolve in GitHub")
    return hits[0] if hits else None


def project_items(entry: dict) -> list[dict]:
    raw = gh_json(["project", "item-list", str(entry["project"]), "--owner", entry["owner"],
                   "--format", "json", "--limit", "1000"])
    data = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(data, list) or int(raw.get("totalCount", len(data))) > len(data):
        raise FollowupError("Project item list is invalid or truncated")
    return data


def item_for(entry: dict, items: list[dict]) -> dict | None:
    return next((i for i in items if str((i.get("content") or {}).get("url") or "") == entry["url"]), None)


def todo_option(entry: dict) -> tuple[str, str, str]:
    view = gh_json(["project", "view", str(entry["project"]), "--owner", entry["owner"], "--format", "json"])
    fields = gh_json(["project", "field-list", str(entry["project"]), "--owner", entry["owner"], "--format", "json"])
    field = next((f for f in (fields or {}).get("fields", []) if f.get("name") == "Status"), None)
    if not (view or {}).get("id") or not field or not field.get("id"):
        raise FollowupError(f"Project {entry['owner']}/{entry['project']}: missing ID or Status field")
    options = {normalize_status(o.get("name")): o.get("id") for o in field.get("options", [])
               if o.get("name") and o.get("id")}
    todo = options.get("todo") or options.get("to do")
    if not todo:
        raise FollowupError(f"Project {entry['owner']}/{entry['project']} has no Todo or To do Status option; "
                            "owner action required before follow-ups can be admitted")
    return view["id"], field["id"], todo


def issue_database_id(repo: str, number: int) -> int:
    obj = gh_json(["api", f"repos/{repo}/issues/{number}"])
    if not isinstance(obj, dict) or "id" not in obj:
        raise FollowupError(f"Issue #{number} is not readable in {repo}")
    return int(obj["id"])


def blocked_by_numbers(repo: str, issue: int) -> set[int]:
    """All blockers of `issue`, every page, bounded by MAX_LIST_PAGES (same ceiling as issue listing)."""
    raw = gh_command(["api", "--method", "GET", f"repos/{repo}/issues/{issue}/dependencies/blocked_by",
                      "--paginate", "--slurp"], timeout=240)
    try:
        pages = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as exc:
        raise FollowupError("gh produced invalid JSON for dependency listing") from exc
    if not isinstance(pages, list) or len(pages) > MAX_LIST_PAGES:
        raise FollowupError("dependency listing exceeded its safety bound; refusing to guess")
    numbers: set[int] = set()
    for page in pages:
        if not isinstance(page, list):
            raise FollowupError("gh returned a non-array dependency page")
        numbers.update(int(row.get("number", 0)) for row in page)
    return numbers


def add_blocker(repo: str, issue: int, blocker: int) -> None:
    """Native GitHub dependency: `issue` is blocked by `blocker`. Skipped when already present."""
    if blocker in blocked_by_numbers(repo, issue):
        return
    gh_command(["api", "--method", "POST", f"repos/{repo}/issues/{issue}/dependencies/blocked_by", "--input", "-"],
               stdin=json.dumps({"issue_id": issue_database_id(repo, blocker)}))


def live_issue(repo: str, number: int) -> dict:
    obj = gh_json(["api", f"repos/{repo}/issues/{number}"])
    if not isinstance(obj, dict) or "id" not in obj:
        raise FollowupError(f"#{number} is not readable as an Issue in {repo}")
    if "pull_request" in obj:
        raise FollowupError(f"#{number} in {repo} is a pull request, not an Issue")
    return obj


def project_row_for_issue(repo: str, number: int, owner: str, project: int) -> dict | None:
    """Live Project membership by exact URL in this repository, or None when absent."""
    raw = gh_json(["project", "item-list", str(project), "--owner", owner, "--format", "json", "--limit", "1000"])
    data = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(data, list) or int(raw.get("totalCount", len(data))) > len(data):
        raise FollowupError("Project item list is invalid or truncated")
    url = f"https://github.com/{repo}/issues/{number}"
    rows = [i for i in data if str((i.get("content") or {}).get("url") or "") == url]
    return rows[0] if rows else None


def require_admitted_context(grant_obj: dict, number: int, role: str) -> None:
    """The parent and every link target: open Issue of grant.repo, in the grant's Project, nonterminal.

    Called before journaling so nothing is recorded that the controller could not later
    schedule. Blocks and blocked_by targets are checked the same way as the parent.
    """
    repo, owner, project = grant_obj["repo"], grant_obj["project_owner"], int(grant_obj["project_number"])
    obj = live_issue(repo, number)
    if obj.get("state") != "open":
        raise FollowupError(f"{role} #{number} is not open in {repo}")
    row = project_row_for_issue(repo, number, owner, project)
    if row is None:
        raise FollowupError(f"{role} #{number} is not a member of Project {owner}/{project}")
    status = str(row.get("status") or "").strip()
    if not status or status.lower() in TERMINAL_STATUSES:
        # Spec: a parent or target must have a nonterminal status. A blank status is refused too,
        # because a blank row is not yet an admitted Issue.
        raise FollowupError(f"{role} #{number} has terminal or blank Project status {status or '(blank)'!r}")


def validate_links(grant_obj: dict, payload: dict) -> None:
    parent = int(grant_obj["parent"])
    targets = payload["blocks"] + payload["blocked_by"]
    if parent in targets:
        raise FollowupError("a follow-up cannot be linked to its own parent Issue")
    require_admitted_context(grant_obj, parent, "parent")
    for number in targets:
        require_admitted_context(grant_obj, number, "link target")


def step_intent(entry: dict) -> None:
    hit = find_marked(entry)
    if hit is None:
        body = (f"{marker(entry['parent'], entry['key'])}\n\n{entry['body']}\n\n---\n"
                f"Follow-up of #{entry['parent']}: {entry['reason']}")
        out = gh_command(["issue", "create", f"--repo={entry['repo']}", f"--title={entry['title']}",
                          "--body-file", "-"], stdin=body).strip()
        url = out.splitlines()[-1].strip() if out else ""
        number = issue_number_from_url(url, entry["repo"])
        hit = live_issue(entry["repo"], number)
        if not is_controller_issue(hit, entry["repo"], entry["viewer_id"]):
            raise FollowupError(f"created #{number} is not authored by the controller account")
        hit = {"number": number, "html_url": url}
    # REST rows carry the API URL in `url`; the web URL is `html_url`. Derive the number from it
    # and cross-check against the row's own number, so a mismatched row cannot be journaled.
    url = str(hit.get("html_url") or "")
    number = issue_number_from_url(url, entry["repo"])
    if number != int(hit.get("number", -1)):
        raise FollowupError(f"Issue row #{hit.get('number')} does not match its URL {url[:120]!r}")
    entry["number"] = number
    entry["url"] = url
    entry["state"] = "created"


def step_created(entry: dict) -> None:
    # Re-verify authorship and repository before admitting anything into the Project.
    issue = live_issue(entry["repo"], entry["number"])
    if not is_controller_issue(issue, entry["repo"], entry["viewer_id"]):
        raise FollowupError(f"follow-up #{entry['number']} is not authored by the controller account")
    if str(issue.get("html_url") or "") != entry["url"]:
        raise FollowupError(f"follow-up #{entry['number']} URL does not match its repository")
    items = project_items(entry)
    if item_for(entry, items) is None:
        gh_command(["project", "item-add", str(entry["project"]), "--owner", entry["owner"], "--url", entry["url"]])
        items = project_items(entry)
    row = item_for(entry, items)
    if row is None or not row.get("id"):
        raise FollowupError(f"follow-up #{entry['number']} is not in the Project after item-add")
    entry["item_id"] = str(row["id"])
    entry["state"] = "added"


def normalize_status(status) -> str:
    """Same normalizer as autonomy-controller.normalize_status: lower, trim, collapse, to-do -> to do."""
    return re.sub(r"\s+", " ", str(status or "").strip().lower().replace("to-do", "to do"))


def step_added(entry: dict) -> None:
    row = item_for(entry, project_items(entry))
    if row is None:
        raise FollowupError(f"follow-up #{entry['number']} vanished from the Project")
    status = normalize_status(row.get("status"))
    if status not in READY_STATUSES:
        # A controller-created item can start blank or in another lane (for example a Project
        # workflow sets Backlog). Only a blank or non-Todo status is corrected here; a status a
        # maintainer set is still moved to Todo because the follow-up must be schedulable.
        project_id, field_id, option_id = todo_option(entry)
        gh_command(["project", "item-edit", "--id", entry["item_id"], "--project-id", project_id,
                    "--field-id", field_id, "--single-select-option-id", option_id])
        # Re-read: the edit must stick. Never mark done unless the live row is Todo.
        after = item_for(entry, project_items(entry))
        if after is None or normalize_status(after.get("status")) not in READY_STATUSES:
            got = normalize_status((after or {}).get("status")) or "(blank)"
            raise FollowupError(f"follow-up #{entry['number']} is not Todo after the status edit (status {got!r})")
    entry["state"] = "todo"


def step_todo(entry: dict) -> None:
    for other in entry["blocks"]:
        add_blocker(entry["repo"], other, entry["number"])
    for other in entry["blocked_by"]:
        add_blocker(entry["repo"], entry["number"], other)
    entry["state"] = "done"


STEPS = {"intent": step_intent, "created": step_created, "added": step_added, "todo": step_todo}


def verify_forgery_guards(entry: dict) -> None:
    """Forged-journal hardening: refuse before any admission step if the entry is not ours.

    The journaled viewer_id must equal the live controller account id, and a created Issue must
    carry the exact marker at the start of its body. Otherwise the entry is foreign; nothing is mutated.
    """
    login, live_id = viewer_identity()
    if entry.get("viewer_id") != live_id:
        raise FollowupError("journal entry was not written by this controller account; refused")
    if entry.get("number") is not None:
        issue = live_issue(entry["repo"], int(entry["number"]))
        if not str(issue.get("body") or "").startswith(marker(entry["parent"], entry["key"])):
            raise FollowupError(f"follow-up #{entry['number']} does not carry this entry's marker; refused")
        if not is_controller_issue(issue, entry["repo"], live_id):
            raise FollowupError(f"follow-up #{entry['number']} is not authored by the controller account; refused")


def advance(journal: Journal, entries: list[dict], entry: dict) -> None:
    """Drive one entry to done. Every completed step is saved before the next one starts."""
    verify_forgery_guards(entry)
    while entry["state"] != "done":
        step = STEPS.get(entry["state"])
        if step is None:
            raise FollowupError(f"unknown follow-up state {entry['state']!r}")
        step(entry)
        journal.save(entries)


# ---------------------------------------------------------------- controller reconciliation

def same_target(entry: dict, repo: str, owner: str, project: int) -> bool:
    """Case-insensitive repo and owner, exact Project number. Anything else is a forged or foreign entry."""
    try:
        project_ok = int(entry.get("project")) == int(project)
    except (TypeError, ValueError):
        return False
    return (str(entry.get("repo") or "").lower() == str(repo).lower()
            and str(entry.get("owner") or "").lower() == str(owner).lower() and project_ok)


def reconcile(pdir: Path, repo: str, owner: str, project: int, due=None) -> list[dict]:
    """Complete unfinished follow-ups for this worker's repo and Project, without AI.

    Returns one result per unfinished entry: {parent, key, ok, error, waiting}. `due(parent, key)`
    lets the controller skip entries still in retry backoff; those report waiting=True and are
    not attempted. Any unfinished entry whose repo, owner or project differs from the worker's
    is refused as a blocker before any gh call. Callers defer selection whenever a result is not ok.
    """
    journal = Journal(pdir)
    results = []
    with journal_lock(pdir):
        entries = journal.load()
        for entry in entries:
            if entry.get("state") == "done":
                continue
            base = {"parent": entry.get("parent"), "key": entry.get("key")}
            if not same_target(entry, repo, owner, project):
                results.append({**base, "ok": False, "waiting": False,
                                "error": "journal entry does not belong to this repository and Project; refused"})
                continue
            if due is not None and not due(entry["parent"], entry["key"]):
                results.append({**base, "ok": False, "error": None, "waiting": True})
                continue
            try:
                advance(journal, entries, entry)
                results.append({**base, "ok": True, "error": None, "waiting": False})
            except FollowupError as exc:
                results.append({**base, "ok": False, "error": str(exc)[:MAX_ERROR], "waiting": False})
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                # A malformed entry or reply must block only itself, with a concrete reason.
                reason = f"malformed follow-up entry ({type(exc).__name__}: {exc})"
                results.append({**base, "ok": False, "error": reason[:MAX_ERROR], "waiting": False})
    return results


# ---------------------------------------------------------------- CLI create

def same_request(entry: dict, payload: dict) -> bool:
    return all(entry.get(field) == payload[field] for field in ("title", "body", "reason", "blocks", "blocked_by"))


def public(entry: dict) -> dict:
    return {"key": entry["key"], "number": entry.get("number"), "url": entry.get("url"), "state": entry["state"]}


def active_task(pdir: Path, obj: dict) -> None:
    """The granted task must be the live, pending Issue checkpoint of this same project.

    A grant for parent 12 is refused while cycle.json names 13, a selection checkpoint is not an
    Issue task, and a missing cycle.json is refused. The pdir name must match the grant's repo.
    """
    if not (pdir / "cycle.pending").exists():
        raise FollowupError("the granted task is no longer pending; follow-up refused")
    checkpoint = read_json(pdir / "cycle.json", None)
    if not isinstance(checkpoint, dict):
        raise FollowupError("no active task checkpoint; follow-up refused")
    if checkpoint.get("kind") not in ACTIVE_CYCLE_KINDS or checkpoint.get("kind") != obj.get("kind"):
        raise FollowupError("the active checkpoint is not an Issue task; follow-up refused")
    try:
        number = int(checkpoint.get("number"))
    except (TypeError, ValueError):
        raise FollowupError("active checkpoint has no valid number; follow-up refused")
    if number != int(obj["parent"]):
        raise FollowupError(f"the active task is #{number}, not the granted #{obj['parent']}; follow-up refused")


class PendingFollowup(FollowupError):
    """The journal entry exists; a later step failed. The controller completes it; retry the same key only."""

    def __init__(self, entry: dict, error: str):
        super().__init__(error)
        self.entry = entry
        self.error = error


def create(grant_path: Path, token: str, raw: str, state: Path) -> dict:
    grant_path = check_grant_path(grant_path, state)
    pdir = grant_path.parent
    obj = read_json(grant_path, None)
    if not isinstance(obj, dict) or not secrets.compare_digest(str(obj.get("token", "")), str(token)):
        raise FollowupError("invalid or stale follow-up token")
    if obj.get("kind") != "issue":
        raise FollowupError("follow-ups are granted only for Issue tasks")
    # Pdir name must be the grant's project directory: a copied grant cannot be used elsewhere.
    if pdir.name != obj.get("project_dir"):
        raise FollowupError("grant does not belong to this project directory")
    active_task(pdir, obj)
    payload = parse_input(raw)
    journal = Journal(pdir)
    with journal_lock(pdir):
        entries = journal.load()
        mine = [e for e in entries if e.get("repo") == obj["repo"] and e.get("parent") == obj["parent"]]
        entry = next((e for e in mine if e.get("key") == payload["key"]), None)
        if entry is not None:
            if not same_request(entry, payload):
                raise FollowupError(f"key {payload['key']!r} was already used for a different follow-up")
        else:
            if len(mine) >= MAX_PER_PARENT:
                raise FollowupError(f"at most {MAX_PER_PARENT} follow-ups per task")
            validate_links(obj, payload)
            login, viewer_id = viewer_identity()
            entry = {"parent": obj["parent"], "key": payload["key"], "repo": obj["repo"],
                     "owner": obj["project_owner"], "project": int(obj["project_number"]),
                     "title": payload["title"], "body": payload["body"], "reason": payload["reason"],
                     "blocks": payload["blocks"], "blocked_by": payload["blocked_by"],
                     "viewer_login": login, "viewer_id": viewer_id,
                     "intent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "state": "intent"}
            entries.append(entry)
            journal.save(entries)
        try:
            advance(journal, entries, entry)
        except FollowupError as exc:
            # Journaled: the follow-up exists in the state file, so this is pending, not refused.
            raise PendingFollowup(public(entry), str(exc)[:MAX_ERROR]) from exc
        return public(entry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a controller-authorized follow-up Issue.")
    parser.add_argument("--grant", required=True, help="path to the task's followup-grant.json")
    parser.add_argument("--token", required=True, help="token from the controller instruction")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="read a JSON object on stdin and create the follow-up")
    args = parser.parse_args(argv)
    raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    try:
        if args.command == "create":
            print(json.dumps(create(Path(args.grant), args.token, raw, ROOT / "state"), sort_keys=True))
    except PendingFollowup as exc:
        # Exit 3: the journal holds the follow-up; the controller completes it. Retry only the same key.
        print(json.dumps({**exc.entry, "pending": True, "error": exc.error}, sort_keys=True))
        return EXIT_PENDING
    except FollowupError as exc:
        # Exit 2: refused before anything was journaled.
        print(f"follow-up refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main())
