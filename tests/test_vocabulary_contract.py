"""One concept, one word — enforced on the rendered text, not the source.

Two nouns named the same thing: ``bundle`` on the admin Access page and
``stack`` everywhere else, roughly a hundred and thirty member-facing strings
between them. Neither named an entity — there is no bundle table and no stack
table, only grants and subscriptions — so a first-time reader had to infer
both from the shape of the rows around them.

Both are retired in favour of saying what the thing does. This file is what
stops the third spelling arriving: the words live in ``app/web/vocabulary``
and a template that retypes a retired phrase fails here.

Deliberately scoped to what a person READS. Comments explaining the history
are allowed to name the old words — they have to, to be useful — and so are
CSS class names and JS identifiers (``lib-instack``, ``stack_endpoint``,
``viewMode``), which are wire values nobody sees. The CLI keeps ``agnes
stack``: a command name is a stable interface with its own compatibility
promise, and that split is a decision, not drift.
"""

import re
from pathlib import Path

import pytest

from app.web import vocabulary

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"

#: Sites that legitimately keep an old word in text a person can read.
ALLOWED = {
    # The Access page still ANSWERS to ?by=bundle so shared links survive; the
    # normalizer names the old value in order to translate it.
    ("admin_access.html", "bundle"),
}


def _renderable_text(src: str) -> str:
    """The template with every comment form stripped.

    Jinja, HTML and JS comments all carry the history of these renames and
    must keep naming the old words to explain them. What is left is what a
    person can end up reading.
    """
    # Blank the comment OUT rather than deleting it, keeping one newline per
    # newline removed, so a reported line number still points at the file. A
    # guard that names the wrong line sends the reader hunting, which is how a
    # true finding gets dismissed as stale.
    def _blank(m):
        return "\n" * m.group(0).count("\n")

    src = re.sub(r"\{#.*?#\}", _blank, src, flags=re.S)
    src = re.sub(r"<!--.*?-->", _blank, src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", _blank, src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    # Wire values: CSS classes, data-attributes and JS identifiers are not
    # read. Only IDENTIFIER-SHAPED tokens are stripped — ones carrying a `_`
    # or `-`, like `lib-instack`, `stack_endpoint`, `data-stack-badge`. The
    # bare word must survive, and this is not a nicety: the first version of
    # this helper stripped `\w*_?stack_?\w*`, which matches "stack" itself,
    # so every retired phrase containing it was unfindable and all thirteen
    # tests in this file passed against a template full of violations. A guard
    # that cannot fail is worse than no guard, because it is also a claim.
    src = re.sub(r'class="[^"]*"', "", src)
    src = re.sub(r'\bdata-[a-z-]+="[^"]*"', "", src)
    src = re.sub(r"\b\w+[-_]\w*stack\w*\b|\b\w*stack[-_]\w+\b", "", src, flags=re.I)
    src = re.sub(r"\bviewMode\b", "", src)
    return src


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


@pytest.mark.parametrize("phrase", vocabulary.RETIRED)
def test_no_template_renders_a_retired_phrase(phrase):
    offenders = []
    for path in _templates():
        if (path.name, phrase.split()[-1].lower()) in ALLOWED:
            continue
        text = _renderable_text(path.read_text())
        if phrase.lower() in text.lower():
            line = next(
                (i for i, ln in enumerate(text.splitlines(), 1) if phrase.lower() in ln.lower()),
                0,
            )
            offenders.append(f"{path.relative_to(TEMPLATES)}:{line}")
    assert not offenders, (
        f'"{phrase}" is retired but still renders in: {", ".join(offenders)}. '
        f"Use app.web.vocabulary (exposed to templates as `words`) instead of a new literal."
    )


def test_the_words_reach_templates_as_one_global():
    """A template must be able to say the word without retyping it — otherwise
    the module is documentation and the literals go on multiplying."""
    from app.web.router import templates as jinja

    words = jinja.env.globals.get("words")
    assert words, "`words` is not registered as a Jinja global"
    assert words["add"] == vocabulary.ADD
    assert words["keep_local"] == vocabulary.KEEP_LOCAL


def test_router_verbs_come_from_the_module_not_from_literals():
    """The Library row is where the four spellings accumulated, so its
    constants are the ones that must not drift back into literals."""
    from app.web import router

    assert router._AGENT_ADD is vocabulary.ADD
    assert router._AGENT_HAS is vocabulary.HAS
    assert router._AGENT_CAN_QUERY is vocabulary.CAN_QUERY


def test_granted_data_does_not_borrow_the_agents_verb():
    """The two verbs answer different questions and must stay different.

    Under auto-membership a grant already put a package in reach; the only
    thing left to decide is whether `agnes pull` keeps a local copy. "Add to
    my agents" there would claim to grant access the admin's grant already
    gave — which is exactly the bug the single label used to have.
    """
    assert vocabulary.KEEP_LOCAL != vocabulary.ADD
    for name in ("catalog_package_detail.html", "memory_domain_detail.html"):
        text = _renderable_text((TEMPLATES / name).read_text())
        assert vocabulary.ADD not in text, f"{name} offers the agents verb for a local copy"


def test_the_helper_can_actually_see_a_violation():
    """The guard above is only worth its runtime if it can fail.

    Its first version stripped every token matching `\\w*_?stack_?\\w*` — which
    matches the bare word — so the retired phrases were invisible to it and
    the whole file passed against templates that were full of them. This pins
    the distinction the helper has to make: an identifier is noise, the word a
    person reads is not.
    """
    visible = _renderable_text('<button>Add to stack</button>')
    assert "Add to stack" in visible

    noise = _renderable_text('<div class="lib-instack" data-stack-badge="x">ok</div>')
    assert "instack" not in noise
    assert "stack-badge" not in noise
