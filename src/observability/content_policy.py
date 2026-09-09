"""The one content-export policy — placement and consent, decided once.

Token counts, sizes, latencies and costs are operational telemetry and are
always exported when export is on. Prompt and completion **text** is not: in
this product it routinely carries customer data, so it leaves the instance
only under a decision somebody recorded. That decision lives in
``config/instance.yaml``::

    observability:
      content_export:
        mode: off            # off | pseudonymized | full
        placement: operator  # operator | third_party  (who runs the collector)
        basis: ""            # contract clause, DPA reference, "internal dev instance"
        approved_by: ""      # a person
        approved_at: ""      # ISO date

Two questions, deliberately separate: *what* may be exported (``mode``) and
*where it lands* (``placement`` — an endpoint the instance's own operator
runs, or a third party's). The code cannot verify which is which; the record
makes the operator say it, and the effective policy is logged and audited
(``observability.content_export``) at startup so the answer is in the trail
rather than in somebody's memory.

**A mode without a basis is `off`.** ``mode: full`` with a blank ``basis``,
``approved_by`` or ``placement`` is an unfinished decision, not a decision;
it exports sizes only and warns. An unknown ``mode``/``placement`` is `off`
for the same reason.

**``AGNES_OTEL_CAPTURE_CONTENT`` is a deprecated alias.** It used to be the
whole switch. It no longer enables anything on its own — set while no policy
exists, it is ignored with a warning. This is a behaviour change for a
deployment that relied on the variable.

Every export path reads this module: the app's own spans
(:mod:`src.observability.otel`) and the embedded engine's telemetry relay
(``app/api/broker.py::otlp_proxy``, via :mod:`src.observability.otlp_scrub`).
:func:`export_text` fails **closed** — when pseudonymisation cannot run, it
returns the withheld placeholder, never the raw text.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from src.anonymization import anonymize_markdown, rules_from_config
from src.anonymization_key import resolve_or_provision_key
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)

MODES = ("off", "pseudonymized", "full")
PLACEMENTS = ("operator", "third_party")
DEPRECATED_ENV_VAR = "AGNES_OTEL_CAPTURE_CONTENT"

#: What an exported text becomes when the policy says `pseudonymized` but the
#: anonymizer could not run. Never the raw text — a missing key is a reason to
#: export less, not more.
WITHHELD = "[content withheld: pseudonymisation unavailable]"

NO_BASIS_WARNING = "content export requested without a recorded basis; exporting sizes only"
DEPRECATED_ENV_WARNING = f"{DEPRECATED_ENV_VAR} is deprecated and no longer enables content export on its own"

#: One announce per process (the lifespan can run more than once in a test
#: run or a reload) — the audit row records a decision, not a startup count.
_announced = False
#: One WARNING per process for a failed pseudonymisation: a broken key would
#: otherwise log once per exported event.
_warned_pseudonym_failure = False


@dataclass(frozen=True)
class ContentExportPolicy:
    """The effective policy. ``mode`` is what actually happens;
    ``requested_mode`` is what the config asked for, so the difference is
    visible in the log line and in the audit row."""

    mode: str
    placement: str
    basis: str
    approved_by: str
    approved_at: str
    requested_mode: str
    warnings: tuple[str, ...]


def _text(value: Any) -> str:
    """Config scalar → stripped text.

    ``mode: off`` in YAML 1.1 is the BOOLEAN ``False``, not the string
    ``"off"`` — the most likely thing an operator writes has to work, so the
    booleans map onto their YAML spellings before the vocabulary check.
    """
    if value is None:
        return ""
    if value is False:
        return "off"
    if value is True:
        return "on"
    return str(value).strip()


def load_content_export_policy(config: Optional[Mapping[str, Any]] = None) -> ContentExportPolicy:
    """Read ``observability.content_export`` and decide the effective mode.

    ``config`` is a whole instance-config mapping (the tests pass one); when
    absent the block is read through :func:`app.instance_config.get_value`.
    An unreadable config yields `off`, which is the safe direction.
    """
    block: Mapping[str, Any] = {}
    if config is not None:
        raw = (config.get("observability") or {}) if isinstance(config, Mapping) else {}
        candidate = raw.get("content_export") if isinstance(raw, Mapping) else None
        block = candidate if isinstance(candidate, Mapping) else {}
    else:
        try:
            from app.instance_config import get_value

            candidate = get_value("observability", "content_export", default=None)
            block = candidate if isinstance(candidate, Mapping) else {}
        except Exception:  # noqa: BLE001 - no config package / unreadable overlay
            logger.debug("content policy: instance config unreadable, defaulting to off", exc_info=True)
            block = {}

    requested = _text(block.get("mode")) or "off"
    placement = _text(block.get("placement"))
    basis = _text(block.get("basis"))
    approved_by = _text(block.get("approved_by"))
    approved_at = _text(block.get("approved_at"))
    warnings: list[str] = []

    mode = requested
    if mode not in MODES:
        warnings.append(f"unknown observability.content_export.mode {mode!r}; exporting sizes only")
        mode = "off"
    # A BLANK consent field is an unfinished decision (the no-basis warning);
    # a filled-in one the vocabulary does not know is a different mistake and
    # says so, so an operator can tell a typo from an omission.
    if mode != "off" and not (basis and approved_by and placement):
        warnings.append(NO_BASIS_WARNING)
        mode = "off"
    if mode != "off" and placement not in PLACEMENTS:
        warnings.append(f"unknown observability.content_export.placement {placement!r}; exporting sizes only")
        mode = "off"

    if mode == "off" and os.environ.get(DEPRECATED_ENV_VAR, "").strip():
        warnings.append(DEPRECATED_ENV_WARNING)
        if requested == "off":
            warnings.append(NO_BASIS_WARNING)

    return ContentExportPolicy(
        mode=mode,
        placement=placement,
        basis=basis,
        approved_by=approved_by,
        approved_at=approved_at,
        requested_mode=requested if requested in MODES else "off",
        warnings=tuple(warnings),
    )


def content_export_mode() -> str:
    """The effective mode, read per call.

    No cache on purpose: the relay asks once per batch and the span emitters
    once per span, so an operator who completes the record is obeyed by the
    next export rather than by the next restart. The read is a dict lookup
    over the already-loaded instance config.
    """
    return load_content_export_policy().mode


def _pseudonym_key() -> bytes:
    """This instance's HMAC key (seam for tests). Never logged."""
    return resolve_or_provision_key()


def export_text(text: str) -> str:
    """One text, as the policy allows it to leave the instance.

    ``full`` → unchanged. ``pseudonymized`` → through the instance anonymizer
    (stable ``EMAIL_<hmac>``/``PERSON_<hmac>``… tokens, so a collector can
    still tell two mentions apart without holding the value). ``off`` → the
    empty string; a caller that asks under `off` gets nothing, not a shorter
    version of the content.

    Fails closed: any failure of the anonymizer or of key resolution yields
    :data:`WITHHELD`, never the raw text.
    """
    global _warned_pseudonym_failure
    mode = content_export_mode()
    if mode == "full":
        return text
    if mode != "pseudonymized":
        return ""
    try:
        return anonymize_markdown(text, key=_pseudonym_key(), rules=rules_from_config()).text
    except Exception:  # noqa: BLE001 - fail closed, whatever went wrong
        if not _warned_pseudonym_failure:
            _warned_pseudonym_failure = True
            logger.warning("content policy: pseudonymisation unavailable, withholding exported content", exc_info=True)
        return WITHHELD


def announce_content_export_policy() -> None:
    """Log and audit the effective policy once per process.

    Called from the app lifespan after ``configure_otel``. Runs no anonymizer
    and provisions no key — announcing a policy must not have side effects on
    the pseudonym space. Never raises: a startup does not fail because the
    trail is unavailable.
    """
    global _announced
    if _announced:
        return
    _announced = True
    try:
        policy = load_content_export_policy()
    except Exception:  # noqa: BLE001 - instrumentation never fails the process
        logger.debug("content policy: could not resolve the policy at startup", exc_info=True)
        return
    for warning in policy.warnings:
        logger.warning("content policy: %s", warning)
    logger.info(
        "content export policy: mode=%s placement=%s approved_by=%s",
        policy.mode,
        policy.placement or "-",
        policy.approved_by or "-",
        extra={
            "event": "content_export_policy",
            "mode": policy.mode,
            "requested_mode": policy.requested_mode,
            "placement": policy.placement,
        },
    )
    try:
        log_safe(
            user_id=None,
            action="observability.content_export",
            resource="observability:content_export",
            params={
                "mode": policy.mode,
                "requested_mode": policy.requested_mode,
                "placement": policy.placement,
                "approved_by": policy.approved_by,
                "approved_at": policy.approved_at,
                # The basis TEXT stays in the config file — the trail records
                # only that one exists (content never enters `params`).
                "basis_recorded": bool(policy.basis),
            },
            result="success",
            client_kind="system",
        )
    except Exception:  # noqa: BLE001 - log_safe already swallows; belt and braces
        logger.debug("content policy: could not write the policy audit row", exc_info=True)


__all__ = [
    "DEPRECATED_ENV_VAR",
    "MODES",
    "PLACEMENTS",
    "WITHHELD",
    "ContentExportPolicy",
    "announce_content_export_policy",
    "content_export_mode",
    "export_text",
    "load_content_export_policy",
]
