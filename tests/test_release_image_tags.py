"""The image tag list is assembled ONCE, and the push that consumes it retries.

`build-and-push` had the tag list inline on the push step. Pushing twice (see
below) would have meant two copies of it, and the day someone edits one copy
an image quietly loses its `dev-<slug>` tag — the VMs pinned to that floating
tag stop moving, and nothing is red. So the list moved into one step whose
output both attempts read, and these tests are what make that single source
trustworthy: the shell that builds it is driven here across every shape the
workflow can produce.

Why the push retries at all: GHCR has answered `unknown blob` on a fully
SUCCESSFUL build more than once (`09ce1cf3` on main; PR #2172 twice in a row
on a diff of two comment edits and two docs files). Every layer uploads, then
the registry rejects a blob it should already hold. The root cause is not
known and it is not reproducible on demand — other branches pushed fine in the
same minutes — so the retry is resilience, not a fix. What must stay true is
that it cannot launder a genuinely broken push into a green check, which is
the last test here.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"

#: Every tag the workflow can emit, as (env var, value) — the three
#: unconditional GHCR ones first, then the four that are empty unless their
#: condition holds.
REQUIRED = ("GHCR_CHANNEL", "GHCR_VERSIONED", "GHCR_SHA")
OPTIONAL = ("GHCR_DEV_SLUG", "GHCR_DEV_PREFIX_LATEST", "AR_CHANNEL", "AR_VERSIONED")


@pytest.fixture(scope="module")
def job() -> dict:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return spec["jobs"]["build-and-push"]


@pytest.fixture(scope="module")
def steps(job: dict) -> list[dict]:
    return job["steps"]


def _step(steps: list[dict], step_id: str) -> dict:
    for s in steps:
        if s.get("id") == step_id:
            return s
    raise AssertionError(f"no step with id={step_id!r} in build-and-push")


def _run_assembly(steps: list[dict], tmp_path: Path, **env: str) -> tuple[int, list[str]]:
    """Run the assembly step's real shell body and return (exit, tags)."""
    if not shutil.which("bash"):
        pytest.skip("bash unavailable")
    out = tmp_path / "gh_output"
    out.write_text("")
    full = {v: "" for v in REQUIRED + OPTIONAL}
    full.update(env)
    full["GITHUB_OUTPUT"] = str(out)
    proc = subprocess.run(
        ["bash", "-c", _step(steps, "tags")["run"]],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    body = out.read_text().splitlines()
    tags: list[str] = []
    if "list<<TAGS_EOF" in body:
        i = body.index("list<<TAGS_EOF") + 1
        while i < len(body) and body[i] != "TAGS_EOF":
            tags.append(body[i])
            i += 1
    return proc.returncode, tags


class TestTheListIsAssembledOnce:
    def test_both_push_attempts_read_the_same_output(self, steps: list[dict]) -> None:
        """The whole point. Two literal lists would drift; one output cannot."""
        first = _step(steps, "push")["with"]["tags"].strip()
        retry = _step(steps, "push_retry")["with"]["tags"].strip()
        assert first == retry == "${{ steps.tags.outputs.list }}", (
            "a push attempt no longer reads the assembled list — if the tags were "
            "inlined again, the two attempts can now disagree about what to publish"
        )

    def test_no_push_step_carries_an_inline_tag_list(self, steps: list[dict]) -> None:
        """Belt to the brace above: a second `tags:` holding real registry
        references would satisfy nothing here but re-create the drift."""
        for step_id in ("push", "push_retry"):
            tags = _step(steps, step_id)["with"]["tags"]
            assert "ghcr.io" not in tags and "pkg.dev" not in tags, (
                f"{step_id} names a registry inline again: {tags[:120]}"
            )


class TestTheAssembledListIsRight:
    def test_main_publishes_the_three_unconditional_tags(self, steps, tmp_path) -> None:
        rc, tags = _run_assembly(
            steps,
            tmp_path,
            GHCR_CHANNEL="ghcr.io/o/r:stable",
            GHCR_VERSIONED="ghcr.io/o/r:stable-2026.09.765",
            GHCR_SHA="ghcr.io/o/r:sha-abc1234",
        )
        assert rc == 0
        assert tags == [
            "ghcr.io/o/r:stable",
            "ghcr.io/o/r:stable-2026.09.765",
            "ghcr.io/o/r:sha-abc1234",
        ]

    def test_a_dev_branch_adds_its_slug_and_prefix_alias(self, steps, tmp_path) -> None:
        """`dev-<slug>` and `dev-<prefix>-latest` are what a developer's VM
        pins to — the tags whose silent loss this suite exists to prevent."""
        rc, tags = _run_assembly(
            steps,
            tmp_path,
            GHCR_CHANNEL="ghcr.io/o/r:dev",
            GHCR_VERSIONED="ghcr.io/o/r:dev-2026.09.765",
            GHCR_SHA="ghcr.io/o/r:sha-1bf5bfb",
            GHCR_DEV_SLUG="ghcr.io/o/r:dev-my-branch",
            GHCR_DEV_PREFIX_LATEST="ghcr.io/o/r:dev-me-latest",
        )
        assert rc == 0
        assert "ghcr.io/o/r:dev-my-branch" in tags
        assert "ghcr.io/o/r:dev-me-latest" in tags
        assert len(tags) == 5

    def test_the_ar_mirror_tags_ride_along_when_configured(self, steps, tmp_path) -> None:
        rc, tags = _run_assembly(
            steps,
            tmp_path,
            GHCR_CHANNEL="ghcr.io/o/r:dev",
            GHCR_VERSIONED="ghcr.io/o/r:dev-2026.09.765",
            GHCR_SHA="ghcr.io/o/r:sha-deadbee",
            AR_CHANNEL="eu-docker.pkg.dev/p/repo/agnes:dev",
            AR_VERSIONED="eu-docker.pkg.dev/p/repo/agnes:dev-2026.09.765",
        )
        assert rc == 0
        assert tags[-2:] == [
            "eu-docker.pkg.dev/p/repo/agnes:dev",
            "eu-docker.pkg.dev/p/repo/agnes:dev-2026.09.765",
        ]

    def test_an_unset_optional_tag_is_dropped_not_passed_as_a_blank(self, steps, tmp_path) -> None:
        """The inline version relied on `docker/build-push-action` tolerating
        blank lines. Nothing should rely on that once we build the list."""
        rc, tags = _run_assembly(
            steps,
            tmp_path,
            GHCR_CHANNEL="ghcr.io/o/r:stable",
            GHCR_VERSIONED="ghcr.io/o/r:stable-2026.09.765",
            GHCR_SHA="ghcr.io/o/r:sha-abc1234",
        )
        assert rc == 0
        assert all(t.strip() for t in tags), f"a blank tag reached the list: {tags!r}"

    @pytest.mark.parametrize("missing", REQUIRED)
    def test_an_empty_required_tag_stops_the_push(self, steps, tmp_path, missing) -> None:
        """A tag that computed to a bare `repo:` would publish an image no
        consumer can find. Refusing is the only safe answer — and it must be
        the assembly step that refuses, before anything is pushed."""
        env = {
            "GHCR_CHANNEL": "ghcr.io/o/r:stable",
            "GHCR_VERSIONED": "ghcr.io/o/r:stable-2026.09.765",
            "GHCR_SHA": "ghcr.io/o/r:sha-abc1234",
        }
        env[missing] = "ghcr.io/o/r:"
        rc, _ = _run_assembly(steps, tmp_path, **env)
        assert rc != 0, f"an empty {missing} was accepted"


class TestTheRetryCannotLaunderARealFailure:
    def test_the_first_attempt_is_allowed_to_fail_and_the_second_is_gated_on_it(self, steps) -> None:
        first = _step(steps, "push")
        retry = _step(steps, "push_retry")
        assert first.get("continue-on-error") is True, (
            "without continue-on-error the job dies on the first attempt and the retry never runs"
        )
        assert retry["if"].strip() == "steps.push.outcome == 'failure'", (
            "the retry must fire ONLY after a failure — an unconditional second push "
            "doubles every build's registry traffic"
        )
        assert "continue-on-error" not in retry, "a failing retry must fail the job"

    def test_a_double_failure_is_red(self, steps) -> None:
        """`continue-on-error` on the first attempt means the job's status no
        longer reflects it. Without this gate the job would report SUCCESS
        having published nothing — a worse failure than the flake, because it
        is silent."""
        gate = [s for s in steps if "outcome != 'success'" in str(s.get("if", ""))]
        assert gate, "no step fails the job when both push attempts fail"
        cond = gate[0]["if"]
        assert "steps.push.outcome == 'failure'" in cond
        assert "steps.push_retry.outcome != 'success'" in cond, (
            "gating on == 'failure' would let a SKIPPED or cancelled retry pass as success"
        )
        assert "exit 1" in gate[0]["run"]

    def test_the_gate_runs_after_both_attempts(self, steps) -> None:
        order = [s.get("id") or s.get("name") for s in steps]
        gate_name = "Fail if neither push attempt landed the image"
        assert order.index("push") < order.index("push_retry") < order.index(gate_name)


def test_provenance_is_off_on_every_push_attempt(steps: list[dict]) -> None:
    """Turned off deliberately (the one named suspicion behind the `unknown
    blob` failures: v6+ attaches an attestation manifest by default, and
    nothing here consumes one). If it is ever re-enabled, both attempts must
    agree — a retry that pushes a different artifact shape than the first is
    not a retry."""
    for step_id in ("push", "push_retry"):
        assert _step(steps, step_id)["with"]["provenance"] is False, (
            f"{step_id} re-enabled provenance; if that is intended, change both attempts "
            "and re-read the note on the push step"
        )


def test_the_assembly_step_precedes_the_pushes(steps: list[dict]) -> None:
    order = [s.get("id") for s in steps]
    assert order.index("tags") < order.index("push")


def test_the_docstring_claim_about_bash_is_actually_exercised(steps: list[dict]) -> None:
    """Guards the harness itself: if the assembly step stopped being shell
    (moved to an action, say), every test above would silently assert nothing
    about the real behaviour."""
    step = _step(steps, "tags")
    assert "run" in step and "uses" not in step
    assert "GITHUB_OUTPUT" in step["run"]
    body = textwrap.dedent(step["run"])
    assert "list<<TAGS_EOF" in body, "the output is no longer the heredoc the harness parses"
