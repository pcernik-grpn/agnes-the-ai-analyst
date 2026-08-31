"""Shared HTTP + reporting helpers for the unstructured-data-pipeline E2E pack.

Every script in this directory (``0N_*.py``) is a small, dependency-light
Python program meant to run directly against a LIVE Agnes instance — never
mocked, never in-process — to prove (or catch a regression in) the
fact-graph-over-Collections gates described in
``docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md``
§15.1 (security), §15.4 (anonymization) and §15.5 (end-to-end runs). See
``README.md`` in this directory for prerequisites, run order, and a triage
table for common failures.

Uses ``httpx`` (a base project dependency — see ``pyproject.toml``) rather
than ``requests`` so the pack needs nothing beyond what ``uv pip install
".[dev,server]"`` already provides.
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0


class ConfigError(RuntimeError):
    """A required environment variable is missing or invalid."""


def env(name: str, *, required: bool = False, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip() or None
    if required and not value:
        raise ConfigError(f"required environment variable {name} is not set")
    return value


@dataclass
class Config:
    """Every env var this pack reads. Optional fields are None when unset —
    each script decides for itself whether that means SKIP or just "fewer
    checks run"."""

    base_url: str
    admin_token: str
    token_a: Optional[str] = None
    token_b: Optional[str] = None
    planted: Optional[str] = None
    sp_connection_id: Optional[str] = None
    group_a: Optional[str] = None
    anon_collection_id: Optional[str] = None
    anon_sample_limit: int = 300
    extract_timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "Config":
        base_url = env("AGNES_BASE_URL", required=True)
        admin_token = env("AGNES_ADMIN_TOKEN", required=True)
        assert base_url is not None and admin_token is not None  # for mypy/ruff; `required=True` already enforced it
        sample_limit_raw = env("AGNES_E2E_ANON_SAMPLE_LIMIT", default="300")
        timeout_raw = env("AGNES_E2E_EXTRACT_TIMEOUT_SECONDS", default="120")
        return cls(
            base_url=base_url.rstrip("/"),
            admin_token=admin_token,
            token_a=env("AGNES_E2E_TOKEN_A"),
            token_b=env("AGNES_E2E_TOKEN_B"),
            planted=env("AGNES_E2E_PLANTED"),
            sp_connection_id=env("AGNES_E2E_SP_CONNECTION_ID"),
            group_a=env("AGNES_E2E_GROUP_A"),
            anon_collection_id=env("AGNES_E2E_ANON_COLLECTION_ID"),
            anon_sample_limit=int(sample_limit_raw or "300"),
            extract_timeout_seconds=int(timeout_raw or "120"),
        )


def client_for(cfg: Config, token: Optional[str] = None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> httpx.Client:
    """A fresh ``httpx.Client`` bound to the instance, bearer-authenticated
    when a token is supplied. One per caller identity — never share a client
    across personas, so a header can't leak across accidentally."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=cfg.base_url, headers=headers, timeout=timeout)


def unique_suffix() -> str:
    """A short, collision-safe suffix for throwaway names/paths/ids this run
    creates — keeps repeated runs from colliding on unique constraints
    (collection slugs, corpus_files paths) without needing any cleanup
    ordering guarantees."""
    return uuid.uuid4().hex[:10]


@dataclass
class Check:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""


class Report:
    """Accumulates named PASS/FAIL/SKIP checks and renders them as a table.
    ``exit_code`` is 1 iff at least one check FAILed — a SKIP never fails the
    run, since every script degrades gracefully when its optional
    prerequisites (persona tokens, a group id, a live SharePoint connection)
    are not supplied."""

    def __init__(self, title: str):
        self.title = title
        self.checks: list[Check] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, "PASS" if ok else "FAIL", detail))
        return ok

    def skip(self, name: str, reason: str) -> None:
        self.checks.append(Check(name, "SKIP", reason))
        print(f"  SKIP  {name}: {reason}")

    def note(self, text: str) -> None:
        """A printed, non-scored line — for framing what a script does and
        does not prove (e.g. AN1's "content anonymization happens in the
        producer" disclaimer)."""
        print(f"  note  {text}")

    def print_table(self) -> None:
        print()
        print(f"== {self.title} ==")
        if not self.checks:
            print("(no checks ran)")
            return
        name_w = max([len("check")] + [len(c.name) for c in self.checks])
        status_w = 4
        print(f"{'check'.ljust(name_w)}  {'stat'.ljust(status_w)}  detail")
        print(f"{'-' * name_w}  {'-' * status_w}  {'-' * 40}")
        for c in self.checks:
            print(f"{c.name.ljust(name_w)}  {c.status.ljust(status_w)}  {c.detail}")
        n_pass = sum(1 for c in self.checks if c.status == "PASS")
        n_fail = sum(1 for c in self.checks if c.status == "FAIL")
        n_skip = sum(1 for c in self.checks if c.status == "SKIP")
        print()
        print(f"{n_pass} passed, {n_fail} failed, {n_skip} skipped")

    @property
    def exit_code(self) -> int:
        return 1 if any(c.status == "FAIL" for c in self.checks) else 0


def fail_and_exit(msg: str) -> None:
    """For a configuration problem discovered before any check can even be
    attempted (e.g. a missing required env var) — distinct from a FAIL row
    in the table, which means a real check ran and did not pass."""
    print(f"CONFIG ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def short(text: str, limit: int = 160) -> str:
    """Trim a detail string for the table — full bodies belong in -v/stderr,
    not blowing out the column width."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
