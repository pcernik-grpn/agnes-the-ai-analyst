"""Persistent bare git repos for internal-mode data apps.

One bare repo per app slug at ``${DATA_DIR}/apps/git/<slug>.git``, served
over git smart-HTTP by ``app/api/data_apps_git.py`` and pushed to by
analysts. Deploys promote a commit to the ``agnes-live`` branch —
``fast_forward_live`` is what the deploy pipeline (Task 7) calls after a
push lands on the default branch, so the runtime container always clones a
pinned, deploy-gated ref rather than whatever the analyst last pushed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from src.data_apps.spec import SLUG_RE

LIVE_REF = "refs/heads/agnes-live"


def repo_path(slug: str) -> Path:
    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid data app slug: {slug!r}")
    return Path(os.environ.get("DATA_DIR", "/data")) / "apps" / "git" / f"{slug}.git"


def init_app_repo(slug: str) -> Path:
    p = repo_path(slug)
    if not (p / "HEAD").exists():
        p.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-b", "main", str(p)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(p), "config", "http.receivepack", "true"], check=True, capture_output=True)
    return p


def resolve_ref(slug: str, ref: str = "HEAD") -> Optional[str]:
    # `--verify <ref>^{commit}` fails (non-zero exit) for an unborn/unresolvable
    # ref instead of `rev-parse`'s lenient bare-name echo (e.g. a fresh bare
    # repo's `HEAD` symbolic-refs to a branch with no commits yet — plain
    # `git rev-parse HEAD` there prints the literal string "HEAD" with exit 0,
    # which would otherwise look like a valid (but bogus) resolved sha).
    r = subprocess.run(
        ["git", "-C", str(repo_path(slug)), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def read_tree(slug: str, ref: str, *, max_bytes: int) -> dict[str, str]:
    """Every text blob at `ref` in app `slug`'s bare repo, keyed by its
    repo-relative POSIX path — the read side of the deploy-time exposure
    scan (``src/data_apps/deploy_check.py``).

    ``git ls-tree -r -l`` lists every blob (recursing into subtrees) with
    its declared byte size, so oversized blobs are skipped WITHOUT ever
    reading their content; the survivors are fetched in one
    ``git cat-file --batch`` round trip (its output arrives in the same
    order objects were requested, so no sha->path map is needed to line the
    two back up). A blob that isn't valid UTF-8 (a binary asset) is
    silently dropped — this feeds a line-oriented text scan, never a
    generic file dump. Returns ``{}`` for any git failure or an empty tree;
    never raises (callers are expected to have already resolved `ref` via
    `resolve_ref`, but this stays defensive regardless).
    """
    p = repo_path(slug)  # validates slug
    ls = subprocess.run(
        ["git", "-C", str(p), "ls-tree", "-r", "-l", ref],
        capture_output=True,
        text=True,
    )
    if ls.returncode != 0 or not ls.stdout:
        return {}

    # Each line: "<mode> <type> <sha> <size>\t<path>".
    wanted: list[tuple[str, str]] = []  # (sha, path), in ls-tree's own order
    for line in ls.stdout.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) != 4 or fields[1] != "blob":
            continue
        sha, size_s = fields[2], fields[3]
        try:
            size = int(size_s)
        except ValueError:
            continue
        if size <= max_bytes:
            wanted.append((sha, path))
    if not wanted:
        return {}

    cat = subprocess.run(
        ["git", "-C", str(p), "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha, _ in wanted).encode("utf-8"),
        capture_output=True,
    )
    raw = cat.stdout
    files: dict[str, str] = {}
    pos = 0
    for _sha, path in wanted:
        nl = raw.find(b"\n", pos)
        if nl == -1:
            break
        header = raw[pos:nl].decode("ascii", errors="replace").split(" ")
        if len(header) != 3:
            break
        try:
            size = int(header[2])
        except ValueError:
            break
        content_start = nl + 1
        content = raw[content_start : content_start + size]
        pos = content_start + size + 1  # skip the record's trailing "\n"
        try:
            files[path] = content.decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary content — not a text file the scan can read
    return files


def fast_forward_live(slug: str, sha: Optional[str] = None) -> str:
    target = sha or resolve_ref(slug, "main") or resolve_ref(slug, "HEAD")
    if not target:
        raise ValueError(f"app repo {slug} has no commits to deploy")
    if sha and not resolve_ref(slug, sha):
        raise ValueError(f"commit {sha!r} not found in app repo {slug}")
    subprocess.run(["git", "-C", str(repo_path(slug)), "update-ref", LIVE_REF, target], check=True, capture_output=True)
    return target


def ensure_branch(slug: str, branch: str, base: str = "main") -> None:
    p = repo_path(slug)  # validates slug
    if resolve_ref(slug, branch) is not None:
        return
    target = resolve_ref(slug, base)
    if not target:
        raise ValueError(f"base ref {base!r} not found in app repo {slug}")
    subprocess.run(["git", "-C", str(p), "update-ref", f"refs/heads/{branch}", target], check=True, capture_output=True)


def delete_branch(slug: str, branch: str) -> None:
    if branch in ("main", "agnes-live"):
        raise ValueError(f"refusing to delete protected branch {branch!r}")
    p = repo_path(slug)  # validates slug
    if resolve_ref(slug, branch) is None:
        return
    subprocess.run(["git", "-C", str(p), "update-ref", "-d", f"refs/heads/{branch}"], check=True, capture_output=True)
