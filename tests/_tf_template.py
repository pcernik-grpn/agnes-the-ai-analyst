"""A faithful-enough `templatefile()` for tests that must render an infra template.

The repo's infra tests are text assertions against `.tpl` sources, which is
cheap but blind to the one thing a template can get wrong on its own: what it
actually renders for a given set of inputs. Two of this module's callers need
the rendered text — a startup script has to survive `bash -n`, and a check
config has to parse as YAML — so they need a renderer.

Scope is deliberately the subset Terraform templates in this repo use:

* `${name}` interpolation of a plain variable (no expressions),
* `%{ if ... ~}` / `%{ else ~}` / `%{ endif ~}` over the guard subset the
  repo actually uses (a bool, `!bool`, `name == \"lit\"`, `name != \"lit\"`,
  `name >|>=|<|<= <int literal>`, and `&&` / `||` chains of those),
* `%{ for x in list ~}` and `%{ for k, v in map ~}` / `%{ endfor ~}`,
* the `$${` and `%%{` escapes, and the `~` whitespace-trim markers.

Anything richer (function calls, comparisons, nested attribute access) raises
rather than silently rendering something the real boot would never see — the
same posture the existing marker-block executors take when they refuse a block
carrying template directives.

Verified against the real `terraform console` renderer by
`tests/test_datadog_module_files.py`; see `render_matches_terraform` there.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["TemplateError", "render_template"]


class TemplateError(AssertionError):
    """The template uses a construct this mini-renderer refuses to guess at."""


_DOLLAR = "\x00__TF_DOLLAR__\x00"
_PERCENT = "\x00__TF_PERCENT__\x00"

# A directive or an interpolation, with the optional `~` trim markers.
_TOKEN = re.compile(r"%\{(~?)\s*(.*?)\s*(~?)\}|\$\{(~?)\s*(.*?)\s*(~?)\}", re.S)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _lex(text: str) -> list:
    """[(kind, payload, trim_left, trim_right)] with literals as ('lit', str)."""
    out: list = []
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            out.append(("lit", text[pos : m.start()], False, False))
        if m.group(2) is not None:
            out.append(("dir", m.group(2), m.group(1) == "~", m.group(3) == "~"))
        else:
            out.append(("var", m.group(5), m.group(4) == "~", m.group(6) == "~"))
        pos = m.end()
    if pos < len(text):
        out.append(("lit", text[pos:], False, False))
    return out


# `~}` eats the spaces/tabs that follow the marker plus AT MOST ONE newline —
# not the whole whitespace run. That distinction is what keeps a loop body's
# indentation and a blank line after `%{ endif ~}`, and it is verified against
# the real renderer in tests/test_datadog_module_files.py.
_TRIM_RIGHT = re.compile(r"^[ \t]*\r?\n?")
# `\Z`, not `$`: Python's `$` also matches just before a trailing newline, so
# `re.sub` fired twice on a run of blank lines and stripped two newlines where
# Terraform strips one. (`_TRIM_RIGHT` is safe — `^` has only one match point.)
_TRIM_LEFT = re.compile(r"\r?\n?[ \t]*\Z")


def _apply_trims(tokens: list) -> list:
    toks = [list(t) for t in tokens]
    for i, tok in enumerate(toks):
        if tok[0] == "lit":
            continue
        if tok[2] and i > 0 and toks[i - 1][0] == "lit":
            toks[i - 1][1] = _TRIM_LEFT.sub("", toks[i - 1][1])
        if tok[3] and i + 1 < len(toks) and toks[i + 1][0] == "lit":
            toks[i + 1][1] = _TRIM_RIGHT.sub("", toks[i + 1][1])
    return [tuple(t) for t in toks]


def _lookup(name: str, scope: Mapping[str, Any]) -> Any:
    if not _IDENT.match(name):
        raise TemplateError(
            f"only a bare variable name is supported, got {name!r} — keep the "
            "policy in Terraform and the template a dumb renderer"
        )
    if name not in scope:
        raise TemplateError(f"template references undefined variable {name!r}")
    return scope[name]


_COMPARE = re.compile(r'^([A-Za-z_]\w*)\s*(==|!=)\s*"([^"]*)"$')
_COMPARE_NUM = re.compile(r"^([A-Za-z_]\w*)\s*(>|>=|<|<=)\s*(-?\d+)$")


def _truth(expr: str, scope: Mapping[str, Any]) -> bool:
    """Evaluate the guard subset the repo's templates use.

    `a && b`, `a || b`, `!a`, `a == "lit"`, `a != "lit"`,
    `a >|>=|<|<= <int literal>`, and a bare bool. Left-to-right with no
    precedence between && and ||, which is enough because no template here
    mixes them; a template that did would be too clever for a dumb renderer
    to guess at, and raises instead.
    """
    expr = expr.strip()
    for op, combine in (("&&", all), ("||", any)):
        if op in expr:
            other = "||" if op == "&&" else "&&"
            if other in expr:
                raise TemplateError(f"mixed && and || in `if {expr}` — split the guard")
            return combine(_truth(part, scope) for part in expr.split(op))

    m = _COMPARE.match(expr)
    if m:
        name, op, literal = m.groups()
        value = _stringify(_lookup(name, scope))
        return value == literal if op == "==" else value != literal

    m = _COMPARE_NUM.match(expr)
    if m:
        name, op, literal = m.groups()
        value = _lookup(name, scope)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TemplateError(f"`if {expr}` needs an int variable, got {type(value).__name__}")
        threshold = int(literal)
        return {
            ">": value > threshold,
            ">=": value >= threshold,
            "<": value < threshold,
            "<=": value <= threshold,
        }[op]

    negate = expr.startswith("!")
    value = _lookup(expr[1:].strip() if negate else expr, scope)
    if not isinstance(value, bool):
        raise TemplateError(f"`if {expr}` needs a bool, got {type(value).__name__}")
    return (not value) if negate else value


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _render_tokens(toks: list, i: int, scope: Mapping[str, Any], out: list) -> int:
    """Render from index `i` until an unmatched end directive; return its index."""
    while i < len(toks):
        kind, payload, _, _ = toks[i]
        if kind == "lit":
            out.append(payload)
            i += 1
        elif kind == "var":
            out.append(_stringify(_lookup(payload, scope)))
            i += 1
        elif payload.startswith(("endif", "endfor", "else")):
            return i
        elif payload.startswith("if "):
            i = _render_if(toks, i, scope, out)
        elif payload.startswith("for "):
            i = _render_for(toks, i, scope, out)
        else:
            raise TemplateError(f"unsupported directive %{{ {payload} }}")
    return i


def _render_if(toks: list, i: int, scope: Mapping[str, Any], out: list) -> int:
    taken = _truth(toks[i][1][len("if ") :].strip(), scope)

    # Both branches are rendered, only one is kept. Rendering the untaken side
    # into a throwaway sink costs nothing and means a typo'd variable in a
    # currently-disabled branch fails here rather than on some future boot.
    then_out: list = []
    j = _render_tokens(toks, i + 1, scope, then_out)
    if j >= len(toks):
        raise TemplateError("unterminated %{ if }")

    else_out: list = []
    if toks[j][1].startswith("else"):
        j = _render_tokens(toks, j + 1, scope, else_out)

    if j >= len(toks) or not toks[j][1].startswith("endif"):
        raise TemplateError("unterminated %{ if }")

    out.extend(then_out if taken else else_out)
    return j + 1


def _render_for(toks: list, i: int, scope: Mapping[str, Any], out: list) -> int:
    m = re.match(r"for\s+([A-Za-z_]\w*)(?:\s*,\s*([A-Za-z_]\w*))?\s+in\s+(\S+)$", toks[i][1])
    if not m:
        raise TemplateError(f"unsupported for directive %{{ {toks[i][1]} }}")
    key_var, val_var, coll_name = m.group(1), m.group(2), m.group(3)
    collection = _lookup(coll_name, scope)

    if val_var is None:
        items = [(None, v) for v in collection]
    elif isinstance(collection, Mapping):
        # Terraform iterates a map in key order.
        items = [(k, collection[k]) for k in sorted(collection)]
    else:
        raise TemplateError(f"`for k, v in {coll_name}` needs a map")

    end = None
    for key, value in items:
        inner = dict(scope)
        if val_var is None:
            inner[key_var] = value
        else:
            inner[key_var] = key
            inner[val_var] = value
        end = _render_tokens(toks, i + 1, inner, out)
    if end is None:
        # Empty collection: walk the body once, into a discarded sink, purely
        # to find the matching endfor. The loop variables are bound to a
        # placeholder so the walk does not trip over its own body.
        skip = dict(scope)
        skip[key_var] = ""
        if val_var is not None:
            skip[val_var] = ""
        end = _render_tokens(toks, i + 1, skip, [])
    if end >= len(toks) or not toks[end][1].startswith("endfor"):
        raise TemplateError("unterminated %{ for }")
    return end + 1


def render_template(text: str, variables: Mapping[str, Any]) -> str:
    """Render `text` the way Terraform's `templatefile()` would."""
    text = text.replace("$${", _DOLLAR).replace("%%{", _PERCENT)
    toks = _apply_trims(_lex(text))
    out: list = []
    end = _render_tokens(toks, 0, variables, out)
    if end != len(toks):
        raise TemplateError(f"unbalanced directive: %{{ {toks[end][1]} }}")
    rendered = "".join(out)
    return rendered.replace(_DOLLAR, "${").replace(_PERCENT, "%{")
