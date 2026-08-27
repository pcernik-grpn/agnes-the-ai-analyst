"""Copy + rendering for the three auth emails (invite, password reset, magic link).

One shared layout, parameterized per flow, so every auth mail carries the same
shape: instance-named sender context, a heading, one sentence of context, a
single CTA button, the link validity, a plain-URL fallback, and a "didn't
expect this?" footer. Each builder returns ``(subject, body_text, body_html)``
— the plaintext part carries the same copy as the HTML part, and
``app.auth._common.send_smtp_email`` sends them as ``multipart/alternative``.

The HTML is email-safe on purpose: a single centered table, inline styles,
a system font stack, no images and no external resources — nothing for a mail
client to block, which also keeps the spam score down. Colors are literal hex
because email clients have no CSS variables; they mirror the app's paper theme
(`--ds-*` tokens in ``design-tokens.css``).

Everything interpolated into the HTML goes through :func:`_esc` — the
recipient address is caller-supplied text and the instance name is
operator-supplied config, neither is markup.
"""

import html
from datetime import timedelta
from urllib.parse import urlsplit

_FONT_STACK = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _strong(value: str) -> str:
    return f'<strong style="color:#0f172a;font-weight:600;">{_esc(value)}</strong>'


def _instance_name() -> str:
    # Lazy import — this module sits on the auth import path and
    # ``app.instance_config`` is not (same convention as ``_common.py``).
    from app.instance_config import get_instance_name

    return get_instance_name()


def _link_host(link: str) -> str:
    try:
        return urlsplit(link).hostname or "this server"
    except ValueError:
        return "this server"


def human_ttl(ttl: timedelta) -> str:
    """``timedelta`` → the phrase the emails print ("7 days", "24 hours", "1 hour").

    Derived from the same constants that enforce expiry (``SETUP_TOKEN_TTL``,
    ``RESET_TOKEN_TTL``, ``MAGIC_LINK_EXPIRY``) so the copy can't drift from
    the actual cutoff.
    """
    total_seconds = int(ttl.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    # A single day reads better as "24 hours" for a token cutoff, so days
    # only from 2 up.
    if days >= 2 and not hours and not minutes:
        return f"{days} days"
    total_hours = days * 24 + hours
    if total_hours and not minutes:
        return "1 hour" if total_hours == 1 else f"{total_hours} hours"
    total_minutes = total_hours * 60 + minutes
    return "1 minute" if total_minutes == 1 else f"{total_minutes} minutes"


def _render(
    *,
    instance_name: str,
    preheader: str,
    heading: str,
    intro_text: str,
    intro_html: str,
    cta_label: str,
    cta_url: str,
    validity: str,
    footer_text: str,
) -> tuple[str, str]:
    """Shared layout → ``(body_text, body_html)``."""
    body_text = (
        f"{heading}\n"
        f"\n"
        f"{intro_text}\n"
        f"\n"
        f"{cta_label}:\n"
        f"{cta_url}\n"
        f"\n"
        f"The link is valid for {validity} and can be used once.\n"
        f"\n"
        f"{footer_text}\n"
    )

    url = _esc(cta_url)
    body_html = f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background-color:#f3f6fa;">
<span style="display:none;font-size:1px;color:#f3f6fa;max-height:0;overflow:hidden;">{_esc(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f3f6fa;">
  <tr>
    <td align="center" style="padding:32px 16px;">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;background-color:#ffffff;border:1px solid #e2e8f0;border-radius:10px;">
        <tr>
          <td style="padding:30px 32px 24px;font-family:{_FONT_STACK};">
            <p style="margin:0 0 22px;font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#0369a1;">{_esc(instance_name)}</p>
            <h1 style="margin:0 0 10px;font-size:20px;font-weight:700;color:#0f172a;">{_esc(heading)}</h1>
            <p style="margin:0 0 22px;font-size:14px;line-height:1.6;color:#475569;">{intro_html}</p>
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="background-color:#0284c7;border-radius:8px;">
                  <a href="{url}" style="display:inline-block;padding:11px 22px;font-family:{_FONT_STACK};font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;">{_esc(cta_label)}</a>
                </td>
              </tr>
            </table>
            <p style="margin:14px 0 0;font-size:12px;color:#64748b;">The link is valid for {_esc(validity)} and can be used once.</p>
            <p style="margin:20px 0 0;font-size:12px;color:#64748b;">If the button doesn't work, copy this link into your browser:<br>
              <a href="{url}" style="font-size:11px;color:#0369a1;word-break:break-all;">{url}</a></p>
            <hr style="border:none;border-top:1px solid #e2e8f0;margin:22px 0 16px;">
            <p style="margin:0;font-size:11px;line-height:1.6;color:#64748b;">{_esc(footer_text)}</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""
    return body_text, body_html


def invite_email(email: str, link: str, ttl: timedelta) -> tuple[str, str, str]:
    """Account-setup invitation → ``(subject, body_text, body_html)``."""
    name = _instance_name()
    validity = human_ttl(ttl)
    intro_text = f"An administrator created an account for {email} on {name}. Set a password to activate it."
    intro_html = (
        f"An administrator created an account for {_strong(email)} on {_strong(name)}. Set a password to activate it."
    )
    body_text, body_html = _render(
        instance_name=name,
        preheader="Set a password to activate your account.",
        heading="You've been invited",
        intro_text=intro_text,
        intro_html=intro_html,
        cta_label="Set up your account",
        cta_url=link,
        validity=validity,
        footer_text=(
            f"You're receiving this because an administrator invited you to "
            f"{name} at {_link_host(link)}. If you weren't expecting it, you "
            f"can safely ignore this email — no account is active until a "
            f"password is set."
        ),
    )
    return f"You've been invited to {name}", body_text, body_html


def reset_email(email: str, link: str, ttl: timedelta) -> tuple[str, str, str]:
    """Password reset → ``(subject, body_text, body_html)``."""
    name = _instance_name()
    validity = human_ttl(ttl)
    intro_text = (
        f"We received a request to reset the password for {email} on {name}. "
        f"If that was you, choose a new password below."
    )
    intro_html = (
        f"We received a request to reset the password for {_strong(email)} on "
        f"{_strong(name)}. If that was you, choose a new password below."
    )
    body_text, body_html = _render(
        instance_name=name,
        preheader="Choose a new password.",
        heading="Reset your password",
        intro_text=intro_text,
        intro_html=intro_html,
        cta_label="Choose a new password",
        cta_url=link,
        validity=validity,
        footer_text=(
            f"You're receiving this because a password reset was requested for "
            f"your account at {_link_host(link)}. If you didn't request it, you "
            f"can safely ignore this email — your password stays unchanged."
        ),
    )
    return f"Reset your {name} password", body_text, body_html


def magic_link_email(email: str, link: str, ttl: timedelta) -> tuple[str, str, str]:
    """Magic-link sign-in → ``(subject, body_text, body_html)``."""
    name = _instance_name()
    validity = human_ttl(ttl)
    intro_text = f"Use the link below to sign in to {name} as {email}."
    intro_html = f"Use the button below to sign in to {_strong(name)} as {_strong(email)}."
    body_text, body_html = _render(
        instance_name=name,
        preheader="Your one-time sign-in link.",
        heading="Sign in",
        intro_text=intro_text,
        intro_html=intro_html,
        cta_label="Sign in",
        cta_url=link,
        validity=validity,
        footer_text=(
            f"You're receiving this because a sign-in link was requested for "
            f"your account at {_link_host(link)}. If you didn't request it, "
            f"you can safely ignore this email."
        ),
    )
    return f"Your sign-in link — {name}", body_text, body_html
