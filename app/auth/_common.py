"""Shared helpers for auth providers (Google OAuth, password, email link).

Kept out of `dependencies.py` so it doesn't pull FastAPI auth machinery into
thin provider modules that only need these stdlib-only helpers.
"""

import os
from typing import Optional


def smtp_from_address() -> str:
    """Sender address for outgoing auth mail (magic link, reset, setup).

    ``SMTP_FROM`` is the canonical key. ``EMAIL_FROM_ADDRESS`` is honored as a
    backward-compatible fallback — it was the removed SendGrid-SDK branch's
    sender key, so a deployment that configured its sender through it keeps
    that sender when it switches to the SMTP relay.

    ``email.from_address`` in ``instance.yaml`` is honored third. The config
    template ships that key and ``docs/CONFIGURATION.md`` documents it, but
    nothing read it — so an operator who configured only the YAML kept sending
    as ``noreply@example.com`` with no error to notice. Env stays ahead of it so
    no existing deployment's sender changes; this only makes a knob that was
    already advertised start working.

    Imported inside the call rather than at module scope: this module is on the
    auth import path and ``app.instance_config`` is not, so a top-level import
    would put config loading back on it.
    """
    from_env = os.environ.get("SMTP_FROM") or os.environ.get("EMAIL_FROM_ADDRESS")
    if from_env:
        return from_env
    try:
        from app.instance_config import get_value

        configured = get_value("email", "from_address")
    except Exception:
        configured = None
    # The template's own placeholder is not a configured sender.
    if configured and configured != "noreply@example.com":
        return str(configured)
    return "noreply@example.com"


def send_smtp_email(to_email: str, subject: str, body_text: str) -> None:
    """Deliver a plaintext mail via the configured SMTP relay; raises on failure.

    SMTP is the only mail transport. Providers with an HTTP API (SendGrid,
    Mailgun, …) are used through their SMTP relay (e.g.
    ``SMTP_HOST=smtp.sendgrid.net``). The former SendGrid SDK branch was
    removed: the ``sendgrid`` package was never a declared dependency, so that
    path always died on import — while the endpoint still answered success.
    """
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = smtp_from_address()
    msg["To"] = to_email
    with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", "587"))) as s:
        if os.environ.get("SMTP_USE_TLS", "true").lower() == "true":
            s.starttls()
        smtp_user = os.environ.get("SMTP_USER")
        if smtp_user:
            s.login(smtp_user, os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)


def safe_next_path(candidate: Optional[str], default: Optional[str] = None) -> str:
    """Return `candidate` if it's a same-origin absolute path, else `default`.

    Open-redirect guard: must start with a single `/` and must NOT start with
    `//` (which browsers treat as protocol-relative, i.e. cross-origin).
    Accepts plain paths like `/catalog` or `/foo?bar=baz`. Rejects
    `javascript:...`, `http://...`, `//evil/`, bare `dashboard`, empty/None, etc.

    When `default` is None, resolves to the operator-configured home route
    (`AGNES_HOME_ROUTE` env > `instance.home_route` YAML > `/dashboard`) so an
    instance with `AGNES_HOME_ROUTE=/home` lands users on /home after OAuth /
    magic-link / password login instead of the legacy /dashboard.

    Lazy-imported to keep this module dependency-free for thin provider
    modules that don't otherwise need `app.instance_config`.
    """
    if default is None:
        from app.instance_config import get_home_route

        default = get_home_route()
    if not candidate or not isinstance(candidate, str):
        return default
    if not candidate.startswith("/"):
        # One exception to "same-origin absolute path only": this deployment's
        # OWN data-app origins. Signing in from an app subdomain has to bounce
        # to the main host (a relative `/login` there loops — see
        # `app/data_apps_subdomain.py`), so returning the caller to the app
        # afterwards needs a cross-host `next`.
        return candidate if _is_own_data_app_origin(candidate) else default
    if candidate.startswith("//"):
        return default
    return candidate


def _is_own_data_app_origin(candidate: str) -> bool:
    """Is ``candidate`` an absolute URL on one of THIS deployment's app origins?

    True only for ``<single-label>.<data_apps.subdomain_base>`` over http(s),
    and only while data apps are enabled AND a base is configured — so a
    deployment that never turned the feature on keeps the old behaviour exactly.

    Deliberately strict about the shapes that merely *look* like an app origin:
    a bare ``@`` anywhere in the netloc is refused outright (``https://
    evil.com@s.apps.example.com/`` reaches the right host but renders as the
    wrong one, and ``https://s.apps.example.com@evil.test/`` reaches the wrong
    host entirely), backslashes are refused because browsers normalize them to
    slashes while ``urlsplit`` does not, and the label check mirrors
    ``DataAppSubdomainMiddleware``'s ``"." not in slug`` — a name this
    deployment cannot route is not a name it should redirect to.

    Not verified: that the slug is a REAL app. That would put a database lookup
    inside a helper every login calls, to close a gap that is not a general open
    redirect — the worst case is that someone who can already create an app
    makes their own app the landing page, on our own infrastructure, behind the
    same RBAC as any other app.
    """
    if "\\" in candidate:
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return False

    from app.instance_config import get_data_apps_config

    cfg = get_data_apps_config()
    if not cfg.get("enabled"):
        return False
    base = (cfg.get("subdomain_base") or "").strip().strip(".").lower()
    if not base:
        return False

    host = (parts.hostname or "").rstrip(".").lower()
    suffix = "." + base
    if not host.endswith(suffix):
        return False
    slug = host[: -len(suffix)]
    return bool(slug) and "." not in slug
