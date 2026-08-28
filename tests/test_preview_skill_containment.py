"""A draft skill is untrusted input being written to disk.

Previewing a skill means making it invokable, and `app/chat/skills_catalog.py`
only reports what is actually on disk in the session's project scope — so a
preview has to write the author's draft into a session workspace. The draft's
NAME becomes a directory name and its BODY becomes a file, both straight from
a browser.

The security playbook's rule for this is "validate **and** realpath-contain
filesystem paths built from untrusted names". These are the tests for that,
plus the one property that is not about paths at all: the write must never
reach the user's shared workspace, or a half-finished draft would follow them
into every future session.
"""

from __future__ import annotations

import pytest

from app.chat.workdir import WorkdirManager


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """A WorkdirManager over a throwaway DATA_DIR. Only the workspace paths
    matter here — nothing in these tests spawns a runner."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("rails")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=None,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.0.0",
        get_marketplace_sha=lambda: "sha",
        get_template_status=lambda: None,
    )


class TestTheNameIsReplacedNotSanitized:
    """Everything outside [a-z0-9-] is dropped rather than escaped. An
    escaping scheme is a thing to get subtly wrong; a whitelist is not."""

    @pytest.mark.parametrize(
        "raw",
        [
            "../../etc/passwd",
            "..",
            "../..",
            "/etc/passwd",
            "~/.ssh/authorized_keys",
            "x/../../y",
            "..\\..\\windows",
            "C:\\Windows\\System32",
            "sk\x00ill",
            ".hidden",
            "-leading-dash",
        ],
    )
    def test_no_traversal_survives(self, raw):
        out = WorkdirManager.safe_skill_dirname(raw)
        assert "/" not in out and "\\" not in out
        assert ".." not in out
        assert "\x00" not in out
        assert not out.startswith("-") and not out.startswith(".")
        assert out == out.lower()

    def test_an_unnamed_draft_falls_back_rather_than_raising(self):
        """The author is mid-draft and has every right not to have named it."""
        for raw in ("", "   ", "!!!", "☕"):
            assert WorkdirManager.safe_skill_dirname(raw) == "draft-skill"

    def test_the_name_is_bounded(self):
        assert len(WorkdirManager.safe_skill_dirname("a" * 5000)) <= 64

    def test_a_usable_name_survives_recognisably(self):
        """Containment that mangles every legitimate name is a bug too — the
        author has to recognise their own skill in the preview."""
        assert WorkdirManager.safe_skill_dirname("Quarterly Report Recipe") == "quarterly-report-recipe"


class TestTheWriteIsContained:
    def _sdir(self, mgr, tmp_path):
        sdir = tmp_path / "session"
        (sdir / ".claude").mkdir(parents=True)
        return sdir

    def test_it_lands_under_the_sessions_skills_dir(self, mgr, tmp_path):
        sdir = self._sdir(mgr, tmp_path)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", "My Recipe", "## Steps\n")
        written = sdir / ".claude" / "skills" / name / "SKILL.md"
        assert written.is_file()
        assert written.read_text() == "## Steps\n"

    @pytest.mark.parametrize("hostile", ["../../../../etc/cron.d/x", "/etc/passwd", "../escape"])
    def test_a_hostile_name_cannot_escape(self, mgr, tmp_path, hostile):
        sdir = self._sdir(mgr, tmp_path)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", hostile, "body")
        resolved = (sdir / ".claude" / "skills" / name).resolve()
        root = (sdir / ".claude" / "skills").resolve()
        assert root in resolved.parents, f"{hostile!r} escaped to {resolved}"

    def test_the_body_is_capped(self, mgr, tmp_path):
        sdir = self._sdir(mgr, tmp_path)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", "big", "y" * 200_000)
        written = (sdir / ".claude" / "skills" / name / "SKILL.md").read_text()
        assert len(written) == WorkdirManager.MAX_PREVIEW_BODY_CHARS

    def test_nothing_else_is_written(self, mgr, tmp_path):
        """One skill, one file. A preview that also dropped a hook or a
        settings file would be running more than the author wrote."""
        sdir = self._sdir(mgr, tmp_path)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", "only", "body")
        skill_dir = sdir / ".claude" / "skills" / name
        assert [p.name for p in skill_dir.iterdir()] == ["SKILL.md"]


class TestItNeverWritesThroughToTheSharedWorkspace:
    def test_a_symlinked_claude_is_replaced_by_a_copy(self, mgr, tmp_path):
        """This is the property that matters most. In a plain session
        `.claude` is a SYMLINK into the user's shared workspace; writing a
        draft through it would leave the half-finished skill in every future
        session they open."""
        ws = mgr.user_workspace("a@b.com")
        (ws / ".claude" / "skills" / "real-skill").mkdir(parents=True)
        (ws / ".claude" / "skills" / "real-skill" / "SKILL.md").write_text("theirs")

        sdir = tmp_path / "session"
        sdir.mkdir(parents=True)
        (sdir / ".claude").symlink_to(ws / ".claude")
        assert (sdir / ".claude").is_symlink()

        mgr.materialize_preview_skill(sdir, "a@b.com", "draft", "mine")

        assert not (sdir / ".claude").is_symlink(), ".claude is still a symlink — the draft wrote through"
        assert not (ws / ".claude" / "skills" / "draft").exists(), "the draft reached the shared workspace"
        # ...and the user's real skills survived the copy.
        assert (sdir / ".claude" / "skills" / "real-skill" / "SKILL.md").read_text() == "theirs"

    def test_the_users_own_skills_are_not_clobbered(self, mgr, tmp_path):
        ws = mgr.user_workspace("a@b.com")
        (ws / ".claude").mkdir(parents=True, exist_ok=True)
        sdir = tmp_path / "session"
        sdir.mkdir(parents=True)
        (sdir / ".claude").symlink_to(ws / ".claude")
        mgr.materialize_preview_skill(sdir, "a@b.com", "draft", "mine")
        before = sorted(p.name for p in (ws / ".claude").iterdir())
        assert "skills" not in before or not (ws / ".claude" / "skills" / "draft").exists()


class TestBothDeliveryPathsCarryIt:
    """A preview that works on one provider and silently does nothing on the
    other is worse than one that is honestly unavailable — that asymmetry is
    exactly what #1552 was (a skill the menu advertised and the sandbox had
    never heard of).

    The native providers mount the session directory, so writing the file is
    the whole delivery. The kai-agent provider mounts nothing — its tarball IS
    the project scope — so it has to pack the same bytes, and it finds them
    through the marker written here.
    """

    def test_the_marker_names_the_draft(self, mgr, tmp_path):
        sdir = tmp_path / "session"
        (sdir / ".claude").mkdir(parents=True)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", "Weekly Revenue", "body")
        marker = sdir / ".claude" / "skills" / ".preview"
        assert marker.read_text() == name

    def test_the_marker_is_a_file_not_a_naming_convention(self, mgr, tmp_path):
        """The author's own skills live in the same directory. Telling the
        draft apart by prefix would either collide with a real skill or force
        a reserved-name rule on people naming their own work."""
        sdir = tmp_path / "session"
        (sdir / ".claude" / "skills" / "their-own").mkdir(parents=True)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", "draft", "body")
        assert name != "their-own"
        assert (sdir / ".claude" / "skills" / "their-own").is_dir(), "the author's skill was disturbed"

    def test_kai_finds_exactly_the_draft(self, mgr, tmp_path, monkeypatch):
        from app.api.kai import _preview_skill_dir

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        sdir = mgr.user_sessions_root("a@b.com") / "sess-1"
        (sdir / ".claude" / "skills" / "not-the-draft").mkdir(parents=True)
        name = mgr.materialize_preview_skill(sdir, "a@b.com", "The Draft", "body")

        class _S:
            id = "sess-1"
            user_email = "a@b.com"

        found = _preview_skill_dir(_S())
        assert found is not None and found.name == name

    def test_kai_finds_nothing_for_an_ordinary_session(self, mgr, tmp_path, monkeypatch):
        """No marker, no overlay — a normal chat must not pick up a stray
        directory as a 'draft'."""
        from app.api.kai import _preview_skill_dir

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        sdir = mgr.user_sessions_root("a@b.com") / "sess-2"
        (sdir / ".claude" / "skills" / "ordinary").mkdir(parents=True)

        class _S:
            id = "sess-2"
            user_email = "a@b.com"

        assert _preview_skill_dir(_S()) is None

    def test_a_tampered_marker_cannot_point_outside(self, mgr, tmp_path, monkeypatch):
        """The marker is read off disk, so it is input too."""
        from app.api.kai import _preview_skill_dir

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
        sdir = mgr.user_sessions_root("a@b.com") / "sess-3"
        skills = sdir / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / ".preview").write_text("../../../../etc")

        class _S:
            id = "sess-3"
            user_email = "a@b.com"

        assert _preview_skill_dir(_S()) is None

    def test_it_survives_a_session_that_has_no_workspace(self, tmp_path, monkeypatch):
        from app.api.kai import _preview_skill_dir

        monkeypatch.setenv("DATA_DIR", str(tmp_path / "nothing"))

        class _S:
            id = "gone"
            user_email = "a@b.com"

        assert _preview_skill_dir(_S()) is None
        assert _preview_skill_dir(object()) is None
