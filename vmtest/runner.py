"""Generic scenario runner: named steps, recorded outcomes, report.json.

Semantics:
- Steps run in order. The first failing step stops all later steps except
  collection steps (they are recorded as `skipped`).
- Steps flagged `collect=True` always run, even after a failure.
- Every step's name, status, duration and detail is recorded.
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

INTEGRATION = {
    'real': [
        'APT distribution packages (git, python3, sudo, systemd user manager)',
        'GitHub CLI from the vendor APT repository (cli.github.com)',
        'mise from its vendor APT repository (mise.jdx.dev) enabled via extrepo',
        'systemd user manager with lingering, logout and reboot',
        'Engram release binary downloaded from GitHub by memory.py (checksum verified)',
    ],
    'stand_in': [
        'OpenCode V2 API: deterministic fake, no model calls',
        'GitHub CLI for controller runtime: deterministic fake, no GitHub API calls',
        'GitHub authentication and onboarding against live GitHub: not exercised',
    ],
}


class ScenarioAbort(Exception):
    """Raised by a step that cannot continue; recorded as a failure."""


@dataclass
class Step:
    name: str
    func: Callable[['Context'], str | None]
    collect: bool = False  # runs even after an earlier failure


@dataclass
class StepResult:
    name: str
    status: str  # passed | failed | skipped
    duration: float = 0.0
    detail: str = ''


@dataclass
class Context:
    distro: str
    repo_root: Path
    artifacts: Path
    ssh: object = None       # vmtest.ssh.GuestSsh, set by harness.py
    guest: object = None     # vmtest.vm.Guest, set by harness.py
    data: dict = field(default_factory=dict)


@dataclass
class Report:
    distro: str
    accel: str | None = None          # resolved accelerator: kvm or tcg
    qemu_version: str | None = None   # first line of `qemu-system-x86_64 --version`
    results: list[StepResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'distro': self.distro,
            'accel': self.accel,
            'qemu_version': self.qemu_version,
            'integration': INTEGRATION,
            'overall': 'passed' if self.results and all(r.status == 'passed' for r in self.results) else 'failed',
            'steps': [r.__dict__ for r in self.results],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
        tmp.replace(path)


def execute(steps: list[Step], ctx: Context, report: Report, report_path: Path) -> bool:
    """Run steps; return True only if every step passed. Writes report.json after each step."""
    failed = False
    for step in steps:
        if failed and not step.collect:
            report.results.append(StepResult(step.name, 'skipped', 0.0, 'earlier step failed'))
            report.write(report_path)
            continue
        started = time.monotonic()
        try:
            detail = step.func(ctx) or ''
            status = 'passed'
        except Exception as exc:  # a step failure never aborts collection
            status = 'failed'
            detail = f'{type(exc).__name__}: {exc}'
            (ctx.artifacts / 'failures').mkdir(parents=True, exist_ok=True)
            (ctx.artifacts / 'failures' / f'{len(report.results):02d}-{_slug(step.name)}.txt').write_text(
                detail + '\n\n' + traceback.format_exc(), encoding='utf-8')
        report.results.append(StepResult(step.name, status, round(time.monotonic() - started, 2), detail[:2000]))
        report.write(report_path)
        if status == 'failed':
            failed = True
    return not failed and all(r.status == 'passed' for r in report.results)


def _slug(name: str) -> str:
    return ''.join(c if c.isalnum() else '-' for c in name.lower()).strip('-')[:60]
