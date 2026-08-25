"""Layout ratchet for `.claude/skills/` — the dev-kit skill directory.

The skill loader only discovers skills in directory form
(`.claude/skills/<name>/SKILL.md`); a bare `.claude/skills/<name>.md` file is
silently invisible to it. Four knowledge skills shipped as flat files for a
while (`agnes-connectors.md`, `agnes-orchestrator.md`, `agnes-rbac.md`,
`agnes-release-process.md`) and never actually loaded in a session — see the
2026-08-24 remediation program, Track B, package B7.

This is a static ratchet, not a one-off fix: it forbids ANY flat
`.claude/skills/*.md` file going forward, so a new knowledge skill can't
silently regress into the same invisible shape.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"


def test_no_flat_skill_files() -> None:
    """Every entry directly under `.claude/skills/` must be a directory.

    A `.md` (or any other file) sitting directly in `.claude/skills/` is a
    skill the loader will never discover — only `<name>/SKILL.md` is real.
    """
    flat_files = sorted(p.relative_to(REPO_ROOT).as_posix() for p in SKILLS_DIR.iterdir() if p.is_file())
    assert not flat_files, (
        "Flat files directly under .claude/skills/ are invisible to the "
        "skill loader (which only discovers <name>/SKILL.md). Move each "
        f"into its own directory as SKILL.md: {flat_files}"
    )


def test_every_skill_directory_has_skill_md() -> None:
    """Every directory under `.claude/skills/` must carry a `SKILL.md`."""
    missing = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in SKILLS_DIR.iterdir()
        if p.is_dir() and not (p / "SKILL.md").is_file()
    )
    assert not missing, f"Skill directories missing SKILL.md: {missing}"


def test_every_skill_md_has_name_and_description_frontmatter() -> None:
    """Every `SKILL.md` must declare `name:` and `description:` frontmatter.

    The loader keys discovery off this frontmatter; a directory-form skill
    with a malformed or missing header is just as invisible as a flat file.
    """
    broken = []
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            broken.append(skill_md.relative_to(REPO_ROOT).as_posix())
            continue
        end = text.find("\n---", 4)
        frontmatter = text[4:end] if end != -1 else ""
        if "name:" not in frontmatter or "description:" not in frontmatter:
            broken.append(skill_md.relative_to(REPO_ROOT).as_posix())
    assert not broken, f"SKILL.md missing name/description frontmatter: {broken}"
