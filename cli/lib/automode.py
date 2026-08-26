"""Declare the marketplace host as internal infrastructure for Claude Code's
auto-mode classifier.

Claude Code's auto-mode classifier soft-denies cloning a git repo and
installing plugins from it as "Untrusted Code Integration". When the operator's
own marketplace host is named in the user-scope ``autoMode.environment`` block,
the classifier's built-in "Trusted Internal Infra (Data Flow)" allow rule stops
treating that clone+install as external/untrusted. This module writes that
declaration into the user-scope ``settings.json`` (see
``cli.lib.session_paths.user_settings_path``) — the only settings scope the
classifier reads ``autoMode`` from besides managed settings.

**The entries state facts about the environment; they do not argue.** An
earlier version told the reading agent that installing from this host "is a
routine, sanctioned internal operation, not integration of untrusted external
code" — a verdict about how the agent should judge a future action, written by
the tool whose actions were being judged. An agent flagged it as exactly the
pattern it is supposed to distrust, and it was right to: a declaration the
subject writes about itself carries no more weight for being emphatic. What
this slot is for is telling the classifier *what a host is*; the conclusion is
the classifier's to draw. So the entries name the server, what it serves, and
who curates it, and stop there.

Writing here reaches OUTSIDE the workspace ``agnes init`` was invited into, so
the caller asks first — see ``cli.commands.init._maybe_declare_marketplace_trust``.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class TrustResult(StrEnum):
    """What actually happened to the settings file.

    A bool could not tell "the entries were already there" from "nothing was
    saved because the file could not be read", and the caller reported both as
    already-declared — so someone who opted in and hit a malformed settings
    file was told the change existed. (Devin Review on #1262.)
    """

    #: The entries were appended and the file was replaced.
    WRITTEN = "written"
    #: An entry naming this host was already present; nothing to do.
    ALREADY_PRESENT = "already_present"
    #: Nothing was saved — unreadable/corrupt JSON, an unexpected shape, or no
    #: host to declare. The reason is on stderr; the file is untouched.
    NOT_WRITTEN = "not_written"
    #: Entries from an older CLI were replaced with the current wording.
    REWRITTEN = "rewritten"


#: Fragments of the wording this module retired. An entry carrying any of
#: them was written by a CLI that argued for a conclusion instead of
#: describing the host, and is replaced rather than left in place.
_RETIRED_FRAGMENTS = (
    "routine, sanctioned internal operation",
    "not integration of untrusted external code",
    # The companion line the retired registry sentence shipped WITH. Its
    # em-dash phrasing carries neither fragment above, so a rewrite matched
    # only its sibling and left this line stranded next to the fresh pair —
    # real settings files from old installs carry exactly this shape.
    "own agnes server — it issued this machine's access token",
)


#: Labels this tool writes. An entry starting with one of them and naming the
#: host is OURS — that is what makes it replaceable. Anything else mentioning
#: the same host is the user's own note and is never touched.
_OUR_LABELS = ("Trusted internal domains:", "Internal package registry:")


def _is_ours(entry: str, host: str) -> bool:
    """True only for text THIS tool wrote for *host*.

    Not "starts with one of the labels and mentions the host": those labels
    are Claude Code's own trust-slot names, so an admin may well maintain
    their own "Trusted internal domains: …" line naming the same server, and
    deleting it during a refresh would take their list with it. An entry
    counts as ours when it is byte-for-byte one of the entries we generate,
    or one of the retired ones we used to. (Devin Review on #1262, twice —
    the first fix widened this too far.)
    """
    if not host or host not in entry:
        return False
    if entry in marketplace_trust_entries(host):
        return True
    return _is_retired(entry) and entry.strip().startswith(_OUR_LABELS)


def _is_retired(entry: str) -> bool:
    lowered = entry.lower()
    return any(fragment in lowered for fragment in _RETIRED_FRAGMENTS)


#: How the declared host is recovered from an entry: both templates — current
#: and retired wording alike — keep a stable prefix around the host. The match
#: alone does not make an entry ours; ``_declared_host`` still puts it through
#: ``_is_ours`` for the extracted host.
_HOST_PATTERNS = (
    re.compile(r"^Trusted internal domains: (?P<host>\S+) is this organization's own Agnes server"),
    re.compile(r"^Internal package registry: [^/]{0,120} served from https://(?P<host>[^/\s]+)/marketplace\.git/"),
)


def _declared_host(entry: str) -> str | None:
    """The host *entry* declares — but only when the entry is OURS for it.

    ``_is_ours`` needs the host up front, which is exactly what a caller
    walking entries written for OTHER hosts does not have. Recover the host
    from the template shape, then hold the entry to the same standard as
    everywhere else — which is ``_is_ours``'s standard, and that is two
    branches, not one: byte-for-byte equal to an entry we currently generate,
    OR carrying a retired wording under one of our labels. The second branch
    is a substring match, so a hand-authored line that starts with one of
    Claude Code's trust-slot labels AND contains a retired phrase does count
    as ours. Saying "byte-for-byte" here would overstate it.
    """
    for pattern in _HOST_PATTERNS:
        match = pattern.match(entry.strip())
        if match and _is_ours(entry, match.group("host")):
            return match.group("host")
    return None


def _is_loopback_host(host: str) -> bool:
    """True for ``localhost``, ``127.0.0.0/8`` and ``::1`` — port or not."""
    try:
        hostname = urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    if not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def prune_stale_loopback_declarations(settings_path: Path, keep_host: str) -> list[str]:
    """Remove OUR declarations for loopback hosts other than *keep_host*.

    A dev server bound to an ephemeral port mints a fresh ``127.0.0.1:<port>``
    host string on every start, so the idempotence check — keyed on the
    current host — could not see the pairs written for previous ports, and
    they accumulated forever (a real settings file had gathered ~40). Worse
    than clutter: the OS hands a freed port to whatever asks next, so a stale
    entry declares trust in an address any local process can claim.

    Only loopback hosts are pruned. Two real domains (staging and production,
    say) are both legitimately declared at once, and this module cannot know
    that a DNS name is ephemeral.

    "Ours" is ``_is_ours``'s definition and no wider: byte-for-byte one of the
    entries we generate for that host, or a retired wording under one of our
    labels. Note what the second branch means HERE specifically — this
    function widens ``_is_ours``'s blast radius from "the one configured host"
    to "every loopback host in the file", so a hand-authored line that both
    starts with one of Claude Code's trust-slot labels and contains a retired
    phrase is removed. Contrived, but real, and pinned by
    ``test_a_user_note_carrying_the_retired_phrase_is_ours_by_label``. An
    ordinary user note that imitates neither is untouched.

    Returns the pruned hosts (deduplicated, in file order); ``[]`` when there
    was nothing to prune or the file could not be read. Unlike
    ``ensure_marketplace_trusted`` this never warns on a malformed file: it
    runs on every ``agnes init`` before any consent gate, and removal is the
    caller's housekeeping, not something the operator asked for by name.
    """
    keep = (keep_host or "").strip()
    if not keep:
        # Without a host to keep, every loopback declaration is "other" —
        # including the live one the caller meant to preserve. `agnes init`
        # guards on `if marketplace_host:` so this is unreachable today, but
        # this is a module-level symbol now and the contract says "other than
        # keep_host", not "all of them".
        return []
    if not settings_path.exists():
        return []
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(loaded, dict):
        return []
    auto_mode = loaded.get("autoMode")
    if not isinstance(auto_mode, dict):
        return []
    environment = auto_mode.get("environment")
    if not isinstance(environment, list):
        return []

    pruned: list[str] = []
    kept: list[object] = []
    for entry in environment:
        if isinstance(entry, str):
            declared = _declared_host(entry)
            if declared and declared != keep and _is_loopback_host(declared):
                if declared not in pruned:
                    pruned.append(declared)
                continue
        kept.append(entry)
    if not pruned:
        return []
    auto_mode["environment"] = kept
    _atomic_write(settings_path, loaded)
    return pruned


def _atomic_write(settings_path: Path, settings: dict) -> None:
    """Temp file in the SAME directory + ``os.replace`` (atomic everywhere)."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_name(settings_path.name + ".tmp")
    tmp_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, settings_path)


def marketplace_trust_entries(host: str) -> list[str]:
    """The exact ``autoMode.environment`` lines for *host*.

    Split out so the caller can show the operator what it is about to add to
    their user-scope settings, verbatim, before it adds it.
    """
    return [
        (
            f"Trusted internal domains: {host} is this organization's own Agnes server. "
            f"It issued this machine's access token and serves the organization's data."
        ),
        (
            f"Internal package registry: this organization's Claude Code plugin marketplace is "
            f"served from https://{host}/marketplace.git/ and cloned to ~/.agnes/marketplace. "
            f"Its contents are curated by that server's operator and filtered per user by that "
            f"server's access rules."
        ),
    ]


def marketplace_trust_state(settings_path: Path, host: str) -> "TrustResult":
    """What ``ensure_marketplace_trusted`` WOULD do, without writing anything.

    Lets the caller skip a question that is already settled: re-running setup
    on a machine that already carries the current declaration asked again
    every time, and an unattended run announced it was not declaring
    something that had been declared long ago. (Devin Review on #1262.)

    ``WRITTEN`` here means "would write"; the file is never touched.
    """
    host = (host or "").strip()
    if not host or not settings_path.exists():
        return TrustResult.WRITTEN if host else TrustResult.NOT_WRITTEN
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return TrustResult.NOT_WRITTEN
    if not isinstance(loaded, dict):
        return TrustResult.NOT_WRITTEN
    auto_mode = loaded.get("autoMode")
    if auto_mode is not None and not isinstance(auto_mode, dict):
        return TrustResult.NOT_WRITTEN
    environment = (auto_mode or {}).get("environment")
    if environment is None:
        return TrustResult.WRITTEN
    if not isinstance(environment, list):
        return TrustResult.NOT_WRITTEN
    ours = [e for e in environment if isinstance(e, str) and _is_ours(e, host)]
    if not ours:
        # A user's own note naming the host is not a declaration. Counting it
        # as one reported the trust as already granted and stopped the write,
        # so the entries never landed. (Devin Review on #1262.)
        return TrustResult.WRITTEN
    return TrustResult.REWRITTEN if any(_is_retired(e) for e in ours) else TrustResult.ALREADY_PRESENT


def ensure_marketplace_trusted(settings_path: Path, host: str) -> TrustResult:
    """Merge ``autoMode.environment`` trust entries for *host* into
    *settings_path*, reporting which of the three outcomes happened.

    - ``host`` empty/None -> no-op, ``NOT_WRITTEN``.
    - Merge-preserving: load the existing JSON and PRESERVE every other key.
    - Corrupt JSON, non-dict top level, non-dict ``autoMode``, or non-list
      ``autoMode.environment`` -> warn on stderr and return ``NOT_WRITTEN``;
      NEVER overwrite/rebuild the user's settings file. The caller must be able
      to tell this apart from an entry that was already there, or it reports a
      failed save as a success.
    - Create ``autoMode.environment`` as ``["$defaults"]`` only when absent;
      ``"$defaults"`` MUST be kept, otherwise the whole built-in rule list for
      that section is replaced.
    - Idempotent: if any existing entry already mentions ``host``, return
      ``ALREADY_PRESENT``.
    - The entries use recognized Environment trust-slot labels ("Trusted
      internal domains:", "Internal package registry:") so the classifier
      registers ``host`` as inside the trust boundary rather than as free-form
      context. They describe the host and stop — see the module docstring on
      why they must not argue for a conclusion. ``host`` is always derived from
      configuration by the caller and MUST NOT be hardcoded (this is the
      vendor-agnostic OSS repo).
    - Consent is the CALLER's job. This function writes when called; it is
      ``agnes init`` that decides whether the operator agreed to a change
      outside their workspace.
    - Write atomically: a temp file in the SAME directory + ``os.replace``
      (atomic on Windows and POSIX). Read/write with ``encoding="utf-8"``.
    """
    host = (host or "").strip()
    if not host:
        return TrustResult.NOT_WRITTEN

    settings: dict[str, object]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: could not read Claude Code settings for auto-mode trust: {exc}", file=sys.stderr)
            return TrustResult.NOT_WRITTEN
        if not isinstance(loaded, dict):
            print("warn: Claude Code settings top level is not an object; leaving it unchanged", file=sys.stderr)
            return TrustResult.NOT_WRITTEN
        settings = loaded
    else:
        settings = {}

    auto_mode = settings.get("autoMode")
    if auto_mode is None:
        auto_mode = {}
        settings["autoMode"] = auto_mode
    elif not isinstance(auto_mode, dict):
        print("warn: Claude Code settings autoMode is not an object; leaving it unchanged", file=sys.stderr)
        return TrustResult.NOT_WRITTEN

    environment = auto_mode.get("environment")
    if environment is None:
        environment = ["$defaults"]
        auto_mode["environment"] = environment
    elif not isinstance(environment, list):
        print(
            "warn: Claude Code settings autoMode.environment is not a list; leaving it unchanged",
            file=sys.stderr,
        )
        return TrustResult.NOT_WRITTEN

    ours = [i for i, e in enumerate(environment) if isinstance(e, str) and _is_ours(e, host)]
    retired = [i for i in ours if _is_retired(environment[i])]
    if ours:
        # A machine that ran an older `agnes init` carries the RETIRED wording
        # — the sentence that told the reading agent installing from this host
        # "is a routine, sanctioned internal operation, not integration of
        # untrusted external code". Matching on the host alone declared that
        # file already correct, so the fix would have applied to new installs
        # only and every existing one would have kept the wording an agent
        # flagged, with no way to replace it. Retired entries are rewritten in
        # place; entries that already say the current thing are left alone.
        # (Devin Review on #1262.)
        if not retired:
            return TrustResult.ALREADY_PRESENT
        # Every line WE wrote for this host goes, not just the retired one:
        # the declaration is a pair, and removing only the sentence carrying
        # the old phrasing left its still-current sibling in place next to the
        # freshly appended twin. A user's own note about the same host is not
        # ours and stays. (Devin Review on #1262, both halves.)
        for i in reversed(ours):
            del environment[i]
        environment.extend(marketplace_trust_entries(host))
        _atomic_write(settings_path, settings)
        return TrustResult.REWRITTEN

    environment.extend(marketplace_trust_entries(host))

    _atomic_write(settings_path, settings)
    return TrustResult.WRITTEN
