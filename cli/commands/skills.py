"""Skills command — agnes skills. Knowledge base for AI agents."""

import json
from pathlib import Path

import typer

skills_app = typer.Typer(help="Built-in knowledge base for AI agents")

SKILLS_DIR = Path(__file__).parent.parent / "skills"


@skills_app.command("list")
def list_skills(as_json: bool = typer.Option(False, "--json", help="Output as JSON")):
    """List available skills."""
    if not SKILLS_DIR.exists():
        if as_json:
            typer.echo(json.dumps([]))
            return
        typer.echo("No skills directory found.")
        return
    rows = []
    for f in sorted(SKILLS_DIR.glob("*.md")):
        name = f.stem
        # Read first line as description
        first_line = f.read_text(encoding="utf-8").split("\n")[0].strip("# ").strip()
        rows.append({"name": name, "description": first_line})
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        typer.echo(f"  {row['name']:25s} {row['description']}")


@skills_app.command("show")
def show_skill(name: str = typer.Argument(..., help="Skill name to display")):
    """Display a skill's content."""
    skill_file = SKILLS_DIR / f"{name}.md"
    if not skill_file.exists():
        typer.echo(f"Skill '{name}' not found. Run: agnes skills list", err=True)
        raise typer.Exit(1)
    typer.echo(skill_file.read_text(encoding="utf-8"))
