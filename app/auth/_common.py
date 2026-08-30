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


def _smtp_from_header() -> str:
    """``From:`` header value — the instance name as display name when one is
    configured, so recipients see "Acme Analyst" instead of a bare relay
    address. ``formataddr`` RFC2047-encodes a non-ASCII name itself.

    Lazy import for the same reason as :func:`smtp_from_address`.
    """
    from email.utils import formataddr

    address = smtp_from_address()
    try:
        from app.instance_config import get_instance_name

        name = get_instance_name()
    except Exception:
        name = ""
    return formataddr((name, address)) if name else address


def send_smtp_email(to_email: str, subject: str, body_text: str, body_html: Optional[str] = None) -> None:
    """Deliver a mail via the configured SMTP relay; raises on failure.

    Plaintext by default; with ``body_html`` the message goes out as
    ``multipart/alternative`` (plaintext part first, per RFC 2046 — clients
    render the last part they support).

    SMTP is the only mail transport. Providers with an HTTP API (SendGrid,
    Mailgun, …) are used through their SMTP relay (e.g.
    ``SMTP_HOST=smtp.sendgrid.net``). The former SendGrid SDK branch was
    removed: the ``sendgrid`` package was never a declared dependency, so that
    path always died on import — while the endpoint still answered success.
    """
    import smtplib
    from email.message import EmailMessage

    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    msg = EmailMessage()
    msg.set_content(body_text)
    if body_html is not None:
        msg.add_alternative(body_html, subtype="html")
    msg["Subject"] = subject
    msg["From"] = _smtp_from_header()
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
    # Refuse a base too short to be a plausible app origin. This is operator
    # config, not user input — but it is one unvalidated string, and a wrong
    # one turns this narrow exception into a general open redirect with no
    # other symptom: a single-label base makes `session_cookie_domain()` return
    # None (no cookie breakage) and `DataAppSubdomainMiddleware` still refuses
    # to route the deployment's own host (no routing breakage), so the instance
    # looks healthy while login redirects anywhere. `base="com"` would accept
    # every `https://<anything>.com/`.
    #
    # Three labels is the documented shape's own minimum (`apps.<agnes-host>`,
    # and a host is itself at least two labels), not an arbitrary bar. It does
    # not make this a public-suffix check — `apps.co.uk` would still pass — so
    # the base-selection guidance in `config/instance.yaml.example` still
    # carries the real rule. Failing here only ever NARROWS to
    # same-origin-paths-only, never widens.
    if base.count(".") < 2:
        return False

    host = (parts.hostname or "").rstrip(".").lower()
    suffix = "." + base
    if not host.endswith(suffix):
        return False
    slug = host[: -len(suffix)]
    return bool(slug) and "." not in slug
