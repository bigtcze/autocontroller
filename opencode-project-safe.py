#!/usr/bin/env python3

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(os.environ["AUTONOMY_PROJECT_ROOT"]).expanduser().resolve()
PROJECT_NAME = os.environ["AUTONOMY_PROJECT_NAME"]
REAL = os.environ.get("AUTONOMY_OPENCODE", str(Path.home() / ".opencode/bin/opencode"))
STATE = Path(os.environ["AUTONOMY_PENDING_MARKER"]).expanduser()

QUIET_SECONDS = 60
# How long --interrupt-owned waits for the owned sessions to drain before failing closed.
INTERRUPT_DRAIN_SECONDS = 30


def log(message):
    print("[autonomy-guard]", message, flush=True)


def opencode_api(method, path):
    """
    Use OpenCode V2's own CLI for service discovery/authentication.
    Returns the unwrapped `data` payload when present.
    """
    result = subprocess.run(
        [REAL, "api", method, path],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"OpenCode API failed: {method} {path}: {detail}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON from OpenCode API: {method} {path}"
        ) from exc

    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]

    return payload


def verify_v2_service():
    info = opencode_api("GET", "/api/info")

    if not isinstance(info, dict):
        raise RuntimeError("Invalid /api/info response")

    version = str(info.get("version", ""))

    if not version.startswith("2."):
        raise RuntimeError(
            f"Expected OpenCode V2 service, got version {version!r}"
        )


def project_paths():
    # Never adopt foreign worktrees or ephemeral clone directories.
    return [str(ROOT)]


def inside_project(directory, roots):
    directory = str(Path(directory).resolve())

    return any(
        directory == root or directory.startswith(root + os.sep)
        for root in roots
    )


def active_sessions():
    """
    V2 /api/session/active is already an ACTIVE-only snapshot.
    Therefore every returned session is considered busy regardless
    of its exact state name. Unknown states cannot accidentally be
    interpreted as idle.
    """
    roots = project_paths()
    active = opencode_api("GET", "/api/session/active")

    if not isinstance(active, dict):
        raise RuntimeError("Invalid /api/session/active response")

    found = set()

    for sid in active:
        if not isinstance(sid, str) or not sid:
            raise RuntimeError("Invalid active session ID")

        quoted = urllib.parse.quote(sid, safe="")
        session = opencode_api("GET", f"/api/session/{quoted}")

        if not isinstance(session, dict):
            raise RuntimeError(f"Invalid session response for {sid}")

        location = session.get("location")

        if isinstance(location, dict):
            directory = location.get("directory")
        else:
            # Defensive compatibility path.
            directory = session.get("directory")

        if not directory:
            raise RuntimeError(
                f"Cannot determine directory for active session {sid}"
            )

        if inside_project(directory, roots):
            found.add(sid)

    return sorted(found)


def repo_ready():
    try:
        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(ROOT), *args],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()

        if git("rev-parse", "--show-toplevel") != str(ROOT.resolve()):
            return False

        # Detached HEAD is not a safe autonomous working state.
        if not git("branch", "--show-current"):
            return False

        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
        ):
            path = Path(git("rev-parse", "--git-path", name))

            if not path.is_absolute():
                path = ROOT / path

            if path.exists():
                return False

        return True

    except (OSError, subprocess.CalledProcessError):
        return False


def session_directory(session):
    location = session.get("location")

    if isinstance(location, dict) and location.get("directory"):
        return location["directory"]

    return session.get("directory")


def get_session(session_id):
    quoted = urllib.parse.quote(session_id, safe="")
    session = opencode_api("GET", f"/api/session/{quoted}")

    if not isinstance(session, dict):
        raise RuntimeError(
            f"Invalid session response for {session_id}"
        )

    return session


def get_session_optional(session_id):
    try:
        return get_session(session_id)
    except RuntimeError as exc:
        if "404" in str(exc):
            return None
        raise


def list_sessions():
    sessions = opencode_api("GET", "/api/session")

    if not isinstance(sessions, list):
        raise RuntimeError("Invalid /api/session response")

    return sessions


def root_session_id(session_id):
    seen = set()
    current = session_id

    while True:
        if current in seen:
            raise RuntimeError(
                f"Session parent cycle detected at {current}"
            )

        seen.add(current)
        session = get_session(current)
        parent = session.get("parentID")

        if not parent:
            return current

        if not isinstance(parent, str):
            raise RuntimeError(
                f"Invalid parentID for session {current}"
            )

        current = parent


def active_root_sessions(session_ids):
    return sorted(
        {
            root_session_id(session_id)
            for session_id in session_ids
        }
    )


def current_boot_id():
    try:
        return Path(
            "/proc/sys/kernel/random/boot_id"
        ).read_text().strip()
    except OSError:
        return None


def load_marker():
    try:
        marker = json.loads(STATE.read_text())
    except OSError as exc:
        raise RuntimeError(
            "Cannot read pending cycle marker"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Pending cycle marker contains invalid JSON"
        ) from exc

    if not isinstance(marker, dict):
        raise RuntimeError(
            "Pending cycle marker is not an object"
        )

    return marker


def save_marker(marker, exclusive=False):
    STATE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = (
        json.dumps(
            marker,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )

    if exclusive:
        fd = os.open(
            STATE,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )

        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        return

    temporary = STATE.with_name(
        f".{STATE.name}.{os.getpid()}.tmp"
    )

    fd = os.open(
        temporary,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )

    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(
            temporary,
            STATE,
        )

    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def previous_guard_alive(marker):
    pid = marker.get("pid")

    if not isinstance(pid, int) or pid <= 0:
        return False

    marker_boot = marker.get("boot_id")
    this_boot = current_boot_id()

    if (
        marker_boot
        and this_boot
        and marker_boot != this_boot
    ):
        return False

    cmdline = Path(
        f"/proc/{pid}/cmdline"
    )

    if not cmdline.exists():
        return False

    try:
        text = (
            cmdline.read_bytes()
            .replace(b"\0", b" ")
            .decode(errors="replace")
        )
    except OSError:
        return False

    return "opencode-project-safe" in text


def model_arg_from_session(session):
    model = session.get("model")

    if not isinstance(model, dict):
        return None

    provider = model.get("providerID")
    model_id = (
        model.get("id")
        or model.get("modelID")
    )
    variant = model.get("variant")

    if not provider or not model_id:
        return None

    value = f"{provider}/{model_id}"

    if variant:
        value += f"#{variant}"

    return value


def enrich_marker(marker, session_id):
    session = get_session(session_id)

    marker["session_id"] = session_id

    if session.get("title"):
        marker["title"] = session["title"]

    if (
        session.get("agent")
        and not marker.get("agent")
    ):
        marker["agent"] = session["agent"]

    if not marker.get("model"):
        model = model_arg_from_session(
            session
        )

        if model:
            marker["model"] = model

    return marker


def find_root_session_by_title(title):
    matches = []

    for item in list_sessions():
        if not isinstance(item, dict):
            continue

        if item.get("title") != title:
            continue

        if item.get("parentID"):
            continue

        session_id = item.get("id")

        if (
            not isinstance(session_id, str)
            or not session_id
        ):
            continue

        session = get_session(session_id)
        directory = session_directory(
            session
        )

        if directory and inside_project(
            directory,
            [str(ROOT.resolve())],
        ):
            matches.append(session_id)

    matches = sorted(set(matches))

    if len(matches) > 1:
        raise RuntimeError(
            "Pending cycle title is ambiguous: "
            + ", ".join(matches)
        )

    return matches[0] if matches else None


def extract_option(args, *names):
    for index, value in enumerate(args):
        for name in names:
            if value == name:
                if index + 1 >= len(args):
                    raise RuntimeError(
                        f"Missing value for {name}"
                    )

                return args[index + 1]

            if (
                name.startswith("--")
                and value.startswith(name + "=")
            ):
                return value.split("=", 1)[1]

    return None


def strip_option(args, name):
    result = []
    index = 0

    while index < len(args):
        value = args[index]

        if value == name:
            if index + 1 >= len(args):
                raise RuntimeError(
                    f"Missing value for {name}"
                )

            index += 2
            continue

        if value.startswith(name + "="):
            index += 1
            continue

        result.append(value)
        index += 1

    return result


def wait_for_sessions_to_settle(root_id=None):
    quiet_since = None
    saw_descendant = False

    while True:
        active = active_sessions()

        if active:
            if root_id:
                roots = active_root_sessions(
                    active
                )

                foreign = [
                    root
                    for root in roots
                    if root != root_id
                ]

                if foreign:
                    raise RuntimeError(
                        "Unexpected concurrent project "
                        "root session(s): "
                        + ", ".join(foreign)
                    )

                if any(
                    session_id != root_id
                    for session_id in active
                ):
                    saw_descendant = True

            quiet_since = None

            log(
                "Still active: "
                + ", ".join(active)
            )

        elif quiet_since is None:
            quiet_since = time.monotonic()

            log(
                "No active project sessions; "
                "starting settle period"
            )

        elif (
            time.monotonic() - quiet_since
            >= QUIET_SECONDS
        ):
            return saw_descendant

        time.sleep(5)


def run_capture(command):
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    output = []
    output_length = 0

    if process.stdout is None:
        raise RuntimeError(
            "Cannot capture OpenCode recovery output"
        )

    for line in process.stdout:
        print(
            line,
            end="",
            flush=True,
        )
        output.append(line)
        output_length += len(line)
        while output_length > 256_000 and len(output) > 1:
            output_length -= len(output.pop(0))

    return (
        process.wait(),
        "".join(output),
    )


def recovery_prompt(nonce):
    return f"""You are Mr. Orchestrator. Resume and reconcile the interrupted autonomous task IN THIS EXISTING SESSION.

Repository: {ROOT} (existing checkout only; no clone or git worktree).
Controller phase: {os.environ.get("AUTONOMY_PHASE", "code")}.
- Read AGENTS.md and relevant .agents contracts; ignore legacy autonomous orchestration policies.
- Before changing anything, inspect git status, branch, local changes, this session's GitHub Issue/PR and CI. Preserve uncommitted changes. Resume the SAME task; never choose new work or adopt unrelated Issues.
- Recover only the interrupted phase:
  - For phase 'code' or 'repair': finish and push the PR through the existing fixer/independent oracle workflow; DO NOT merge. The controller verifies CI and merges.
  - For phase 'selection': this was READ-ONLY OMO backlog triage, NOT an assigned coding Issue. Never modify code, git state, Issues, Project fields or PRs. Never start coding during selection. If AUTOCONTROLLER_SELECT_ISSUE or AUTOCONTROLLER_NO_ACTION was already emitted in this session, keep that decision unchanged; otherwise finish triage from the existing Project candidates and emit exactly one such marker before the recovery completion nonce.
- Never claim merge/completion without checking live GitHub state. If externally blocked, report exact evidence; no blind retries, no branch-protection bypass, no controller changes.

{os.environ.get("AUTOCONTROLLER_LEARNING_INSTRUCTIONS", "")}

Only once reconciliation is complete, emit this as the final line:
{nonce}
"""


def resume_turn(session_id, marker):
    if get_session_optional(
        session_id
    ) is None:
        raise RuntimeError(
            "Recorded pending session no longer exists: "
            + session_id
        )

    nonce = (
        "PROJECT_RECOVERY_COMPLETE_"
        + secrets.token_hex(12)
    )

    marker["pid"] = os.getpid()
    marker["boot_id"] = current_boot_id()
    marker["status"] = "resuming"
    marker["resume_attempts"] = (
        int(
            marker.get(
                "resume_attempts",
                0,
            )
        )
        + 1
    )
    marker["last_resume"] = time.time()

    save_marker(marker)

    command = [
        REAL,
        "run",
        "--session",
        session_id,
    ]

    if marker.get("agent"):
        command += [
            "--agent",
            str(marker["agent"]),
        ]

    if marker.get("model"):
        command += [
            "--model",
            str(marker["model"]),
        ]

    command.append(
        recovery_prompt(nonce)
    )

    log(
        "Resuming interrupted root session: "
        + session_id
    )

    rc, output = run_capture(
        command
    )

    if rc:
        raise RuntimeError(
            "OpenCode recovery continuation "
            f"exited with {rc}"
        )

    if nonce not in output:
        raise RuntimeError(
            "Recovery continuation returned "
            "without its completion token; "
            "pending cycle preserved"
        )

    log(
        "Recovery turn returned; waiting "
        "for project sessions to settle"
    )

    return wait_for_sessions_to_settle(
        session_id
    )


def finish_recovery(session_id, marker):
    while True:
        saw_descendant = resume_turn(
            session_id,
            marker,
        )

        if not saw_descendant:
            break

        log(
            "Background subagent work settled; "
            "running another reconciliation turn"
        )

    if not repo_ready():
        raise RuntimeError(
            "Repository is not ready after recovery"
        )

    STATE.unlink()

    log(
        "Recovered cycle completed; "
        "next cycle permitted"
    )


def resolve_pending_root(marker):
    recorded = marker.get(
        "session_id"
    )

    if recorded:
        if get_session_optional(
            recorded
        ) is None:
            raise RuntimeError(
                "Recorded pending root session "
                "no longer exists: "
                + str(recorded)
            )

        return recorded

    title = marker.get("title")

    if title:
        session_id = (
            find_root_session_by_title(
                title
            )
        )

        if session_id:
            enrich_marker(
                marker,
                session_id,
            )
            save_marker(marker)

            log(
                "Resolved pending root session "
                "by title: "
                + session_id
            )

            return session_id

    active = active_sessions()

    # Compatibility path for the old marker format that did not yet
    # contain a session ID or unique title.
    if active and not title:
        roots = active_root_sessions(
            active
        )

        if len(roots) != 1:
            raise RuntimeError(
                "Legacy pending marker is ambiguous; "
                "active roots: "
                + ", ".join(roots)
            )

        session_id = roots[0]

        enrich_marker(
            marker,
            session_id,
        )

        marker["checkpointed_legacy"] = (
            time.time()
        )

        save_marker(marker)

        log(
            "Adopted legacy pending root session: "
            + session_id
        )

        return session_id

    return None


def recover_pending_cycle():
    marker = load_marker()

    if previous_guard_alive(
        marker
    ):
        raise RuntimeError(
            "Previous guard process is still running: "
            "PID "
            + str(marker.get("pid"))
        )

    session_id = resolve_pending_root(
        marker
    )

    active = active_sessions()

    if active:
        roots = active_root_sessions(
            active
        )

        if not session_id:
            raise RuntimeError(
                "Pending marker cannot be associated "
                "with active project work"
            )

        if session_id not in roots:
            raise RuntimeError(
                "Active project work belongs to "
                "different root session(s): "
                + ", ".join(roots)
            )

        marker["pid"] = os.getpid()
        marker["boot_id"] = (
            current_boot_id()
        )
        marker["status"] = (
            "adopted-active"
        )
        marker["adopted_at"] = (
            time.time()
        )

        save_marker(marker)

        log(
            "Adopted interrupted active cycle: root "
            + session_id
        )

        log(
            "Waiting for surviving execution "
            "before reconciliation"
        )

        wait_for_sessions_to_settle(
            session_id
        )

    if not session_id:
        raise RuntimeError(
            "Pending cycle exists but its root "
            "session cannot be resolved; "
            "marker preserved"
        )

    finish_recovery(
        session_id,
        marker,
    )

    return True


def checkpoint_pending_marker():
    verify_v2_service()

    if not STATE.exists():
        print("No pending marker.")
        return 0

    marker = load_marker()

    recorded = marker.get(
        "session_id"
    )

    if recorded:
        if get_session_optional(
            recorded
        ) is None:
            print(
                "Recorded pending session "
                "no longer exists:",
                recorded,
            )
            return 2

        enrich_marker(
            marker,
            recorded,
        )

        marker["boot_id"] = (
            marker.get("boot_id")
            or current_boot_id()
        )

        save_marker(marker)

        print(
            "Pending marker already has "
            "root session:",
            recorded,
        )

        return 0

    active = active_sessions()

    if not active:
        title = marker.get(
            "title"
        )

        if title:
            session_id = (
                find_root_session_by_title(
                    title
                )
            )

            if session_id:
                enrich_marker(
                    marker,
                    session_id,
                )
                save_marker(marker)

                print(
                    "Checkpointed pending root "
                    "session:",
                    session_id,
                )

                return 0

        print(
            "Pending marker exists, but no "
            "active project session can be "
            "checkpointed."
        )

        return 2

    roots = active_root_sessions(
        active
    )

    if len(roots) != 1:
        print(
            "Cannot checkpoint; active roots "
            "are ambiguous:"
        )

        for root in roots:
            print("  ", root)

        return 2

    session_id = roots[0]

    enrich_marker(
        marker,
        session_id,
    )

    marker["boot_id"] = (
        marker.get("boot_id")
        or current_boot_id()
    )
    marker["checkpointed_at"] = (
        time.time()
    )

    save_marker(marker)

    print(
        "Checkpointed pending root session:",
        session_id,
    )

    return 0

def guard_check():
    verify_v2_service()

    sessions = active_sessions()
    ready = repo_ready()
    pending = STATE.exists()

    print("Repository ready:", ready)
    print("Pending marker:", pending)
    print("Active project sessions:", len(sessions))

    for sid in sessions:
        print("  ", sid)

    if sessions or pending or not ready:
        return 2

    print("GUARD CHECK PASSED")
    return 0


def clear_unstarted_quota():
    """Remove only a pre-session marker after the controller observed quota.

    A missing API response or an ambiguous session ALWAYS preserves the marker.
    The controller will retry the same task (never pick unrelated work).
    """
    verify_v2_service()
    if not STATE.exists():
        raise RuntimeError("No quota marker to clear")
    marker=load_marker()
    if marker.get("session_id") or marker.get("status")!="starting":
        raise RuntimeError("Marker may belong to a real session; refusing removal")
    title=marker.get("title")
    if not isinstance(title,str) or not title:
        raise RuntimeError("Marker lacks its unique title")
    if active_sessions() or find_root_session_by_title(title):
        raise RuntimeError("Session may exist; refusing marker removal")
    if not repo_ready():
        raise RuntimeError("Checkout not safe to retry")
    STATE.unlink()
    log("Cleared verified unstarted quota marker; same task can retry after cooldown")
    return 0


def interrupt_owned():
    """Interrupt only the root and descendants proven by this guard's marker."""
    verify_v2_service()
    if not STATE.exists(): return 0
    marker=load_marker()
    root=resolve_pending_root(marker)
    if root is None:
        if active_sessions(): raise RuntimeError("Pending root unresolved; cannot safely interrupt")
        return 0
    deadline=time.monotonic()+INTERRUPT_DRAIN_SECONDS
    while True:
        active=active_sessions()
        owned=[sid for sid in active if root_session_id(sid)==root]
        if not owned: return 0
        for sid in sorted(owned,key=lambda sid:sid!=root):
            opencode_api("POST", f"/api/session/{urllib.parse.quote(sid,safe='')}/interrupt")
        if time.monotonic()>=deadline: raise RuntimeError("Owned OpenCode sessions did not drain after interrupt")
        time.sleep(1)


def main():
    if sys.argv[1:] == ["--check"]:
        try:
            sys.exit(guard_check())
        except Exception as exc:
            print("GUARD CHECK FAILED:", exc)
            sys.exit(2)



    if sys.argv[1:] == ["--checkpoint"]:
        try:
            sys.exit(
                checkpoint_pending_marker()
            )
        except Exception as exc:
            print(
                "CHECKPOINT FAILED:",
                exc,
            )
            sys.exit(2)


    if sys.argv[1:] == ["--interrupt-owned"]:
        try: sys.exit(interrupt_owned())
        except Exception as exc:
            log("INTERRUPT NOT VERIFIED: " + str(exc))
            sys.exit(2)

    if sys.argv[1:] == ["--clear-unstarted-quota"]:
        try:
            sys.exit(clear_unstarted_quota())
        except Exception as exc:
            print("UNSTARTED QUOTA MARKER PRESERVED:",exc)
            sys.exit(2)


    # Preserve normal OpenCode behaviour for everything except guarded `run`.
    if len(sys.argv) < 2 or sys.argv[1] != "run":
        os.execv(REAL, [REAL] + sys.argv[1:])


    try:
        verify_v2_service()

        # A previous cycle always wins over fresh roadmap work.
        if STATE.exists():
            recover_pending_cycle()
            sys.exit(0)

        if not repo_ready():
            raise RuntimeError(
                "Repository is not ready"
            )

        existing = active_sessions()

        if existing:
            raise RuntimeError(
                "Active project sessions: "
                + ", ".join(existing)
            )

        run_args = list(
            sys.argv[2:]
        )

        forbidden = (
            "--continue",
            "-c",
            "--session",
            "-s",
            "--fork",
        )

        for value in run_args:
            if (
                value in forbidden
                or value.startswith("--session=")
            ):
                raise RuntimeError(
                    "Guard owns session lifecycle; "
                    "do not pass --continue, "
                    "--session or --fork"
                )

        requested_title = (
            extract_option(
                run_args,
                "--title",
            )
            or "project autonomous cycle"
        )

        # The guard owns the real title so it can recover the exact session
        # even if it dies before session_id is checkpointed.
        run_args = strip_option(
            run_args,
            "--title",
        )

        unique_title = (
            requested_title
            + " [guard:"
            + str(int(time.time()))
            + "-"
            + str(os.getpid())
            + "]"
        )

        agent = extract_option(
            run_args,
            "--agent",
        )

        model = extract_option(
            run_args,
            "--model",
            "-m",
        )

        marker = {
            "started": time.time(),
            "pid": os.getpid(),
            "boot_id": current_boot_id(),
            "opencode": "v2",
            "status": "starting",
            "title": unique_title,
            "requested_title": requested_title,
        }

        if agent:
            marker["agent"] = agent

        if model:
            marker["model"] = model

        save_marker(
            marker,
            exclusive=True,
        )

        command = [
            REAL,
            "run",
            "--title",
            unique_title,
        ] + run_args

        log(
            "Starting guarded V2 cycle: "
            + unique_title
        )

        process = subprocess.Popen(
            command,
            cwd=ROOT,
        )

        session_id = None
        lookup_error = None

        # Checkpoint the actual OpenCode-created root session as soon as it
        # appears. Until then the unique title in the durable marker is enough
        # to recover it after a controller crash.
        while (
            process.poll() is None
            and session_id is None
        ):
            try:
                session_id = (
                    find_root_session_by_title(
                        unique_title
                    )
                )

                if session_id:
                    enrich_marker(
                        marker,
                        session_id,
                    )
                    marker["status"] = (
                        "running"
                    )
                    marker[
                        "session_checkpointed_at"
                    ] = time.time()
                    save_marker(marker)

                    log(
                        "Checkpointed root session: "
                        + session_id
                    )

            except Exception as exc:
                lookup_error = str(exc)

            if session_id is None:
                time.sleep(1)

        rc = process.wait()

        if session_id is None:
            try:
                session_id = (
                    find_root_session_by_title(
                        unique_title
                    )
                )

                if session_id:
                    enrich_marker(
                        marker,
                        session_id,
                    )
                    marker["status"] = (
                        "running"
                    )
                    save_marker(marker)

                    log(
                        "Checkpointed root session "
                        "after CLI return: "
                        + session_id
                    )

            except Exception as exc:
                lookup_error = str(exc)

        if rc:
            raise RuntimeError(
                f"OpenCode exited with {rc}"
            )

        if not session_id:
            detail = (
                f": {lookup_error}"
                if lookup_error
                else ""
            )

            raise RuntimeError(
                "OpenCode cycle finished but its "
                "root session could not be "
                "checkpointed"
                + detail
            )

        log(
            "CLI returned; waiting for all "
            "project V2 sessions to settle"
        )
        wait_for_sessions_to_settle(session_id)

        log(
            "Final reconciliation in existing root session"
        )
        finish_recovery(session_id, marker)

    except Exception as exc:
        log(
            "BLOCKED: "
            + str(exc)
        )
        log(
            "Pending cycle is preserved for recovery"
        )
        sys.exit(78)

if __name__ == "__main__":
    main()
