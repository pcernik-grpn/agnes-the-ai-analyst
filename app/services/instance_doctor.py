"""New-instance deployment doctor — the server-side half of the deploy gate.

Six checks, each of which silently failed on a real new-instance deployment
and cost hours of debugging. They are deliberately independent of startup
logs: the failures they catch end in a single ``logger.warning`` (or in no
signal at all), so the doctor re-derives every answer from the database, the
environment and a real page render.

- ``login-door`` — at least one auth provider is actually usable, not merely
  configured: a password holder exists, an OAuth provider probes available,
  or magic-link email has a transport. (The seed-admin path in
  ``app/main.py`` swallows every failure into one warning, so an instance
  can boot with zero ways to sign in.)
- ``email-delivery`` — the send path can genuinely deliver: flags the
  SendGrid-key-without-package trap (the ``sendgrid`` SDK is not a
  dependency of this project), the default ``noreply@example.com`` sender
  that relays silently drop, and — given a recipient — sends a real test
  message through the same ``_send_mail`` the login flows use.
- ``chat-grant`` — ``chat.enabled`` needs an explicit ``(group, chat, chat)``
  resource grant to be visible to ANYONE, admins included; god-mode
  deliberately does not surface the entry point (``_compute_can_chat``).
- ``agent-scope`` — every table-scoped agent profile has a non-empty
  owner-grants ∩ scope; an empty intersection means the agent answers every
  data question with 403 "not in your stack".
- ``app-state-backend`` — since A1, fresh installs run app-state on Postgres
  (``side_car``/``cloud``); DuckDB is legacy-only for new deploys. This
  check is a NEW-instance gate — an existing instance still on DuckDB
  failing it here is correct and informative, not a bug.
- ``branding`` — when ``instance.brand`` is customized, the *rendered* login
  page no longer shows a default title (the title reads ``instance.name``, a
  different knob, so setting brand alone leaves the default visible).

Check rows reuse the ``agnes diagnose`` vocabulary — ``{name, status,
audience, detail}`` with ``status ∈ {ok, warning, error, info}`` — so the
platform keeps one verdict format, not two. Every check is isolated: one
crashing resolver reports itself as an error row instead of killing the
report (same contract as ``app/services/admin_dashboard.py``).

Consumed by ``POST /api/admin/doctor/new-instance`` (``app/api/admin_doctor.py``),
``agnes admin doctor --new-instance`` and ``scripts/ops/post-deploy-smoke-test.sh``.
"""

import importlib.util
import logging
import os
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SENDER = "noreply@example.com"

CHECK_NAMES = ("login-door", "email-delivery", "chat-grant", "agent-scope", "app-state-backend", "branding")


def _row(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "audience": "operator", "detail": detail}


def check_login_door() -> dict:
    """At least one login door is genuinely usable — independent of logs."""
    from app.auth.provider_registry import probe_providers
    from app.auth.scheduler_token import SCHEDULER_USER_EMAIL
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo

    offered = {p["name"] for p in probe_providers() if p["allowed"] and p["available"]}
    # The synthetic scheduler user is auto-added to Admin and may carry a
    # hash, but it cannot sign in interactively — mirror the /auth/bootstrap
    # lock and the C8 startup warning by excluding it.
    holders = [u for u in users_repo().list_all() if u.get("password_hash") and u.get("email") != SCHEDULER_USER_EMAIL]

    doors: list[str] = []
    if "password" in offered and holders:
        doors.append(f"password ({len(holders)} user(s) hold a password)")
    for oauth in ("google", "microsoft", "keboola"):
        if oauth in offered:
            doors.append(f"{oauth} OAuth")
    email_open = "email" in offered

    if doors:
        detail = "usable login door(s): " + ", ".join(doors)
        if email_open:
            detail += "; magic-link email is offered too (verify delivery via the email-delivery check)"
        return _row("login-door", "ok", detail)

    if email_open:
        return _row(
            "login-door",
            "warning",
            "the ONLY login door is magic-link email and its delivery is unverified — "
            "re-run with email_to to send a real test message before trusting it. "
            "Magic links are only minted for already-registered users.",
        )

    # No door at all. Say precisely how to open one: /auth/bootstrap is
    # reachable unauthenticated only while no (human) admin AND no password
    # holder exists; once the seed admin row exists, the fix is
    # SEED_ADMIN_PASSWORD or OAuth.
    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    admin_members = user_group_members_repo().list_members_for_group(admin_group["id"]) if admin_group else []
    admin_exists = any(m.get("email") != SCHEDULER_USER_EMAIL for m in admin_members)
    if not admin_exists:
        hint = (
            "/auth/bootstrap is reachable UNAUTHENTICATED — claim the seed admin now "
            "(POST /auth/bootstrap with the seed email + a strong password) before exposing the URL."
        )
    else:
        hint = (
            "an admin row exists but holds no password, so /auth/bootstrap is already locked — "
            "set SEED_ADMIN_PASSWORD in the environment and recreate the app (it applies to a "
            "password-less seed admin on startup), or configure an OAuth provider "
            "(GOOGLE_CLIENT_ID/SECRET or MICROSOFT_*)."
        )
    extra = ""
    if "password" in offered and not holders:
        extra = "password sign-in is enabled but no user holds a password; "
    return _row("login-door", "error", f"NO usable login door — {extra}{hint}")


def check_email_delivery(email_to: Optional[str] = None) -> dict:
    """The send path can really deliver — optionally proven with a real message.

    A 200 from the send endpoints proves nothing: the magic-link flow hides
    ``send_error`` behind an anti-enumeration message, and an SMTP relay can
    accept mail from an unverified sender and then drop it without a trace.
    """
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    smtp_host = os.environ.get("SMTP_HOST")

    if sendgrid_key and importlib.util.find_spec("sendgrid") is None:
        return _row(
            "email-delivery",
            "error",
            "SENDGRID_API_KEY is set but the sendgrid python package is not installed in this "
            "image — the login page offers magic links while every send raises "
            "ModuleNotFoundError. Use the SMTP relay instead: SMTP_HOST=smtp.sendgrid.net, "
            "SMTP_USER=apikey, SMTP_PASSWORD=<the key>, SMTP_FROM=<verified sender>.",
        )
    if not sendgrid_key and not smtp_host:
        return _row(
            "email-delivery",
            "info",
            "no email transport configured (neither SMTP_HOST nor SENDGRID_API_KEY) — "
            "magic-link login, password resets and invite emails cannot send",
        )

    if sendgrid_key:
        transport = "sendgrid"
        sender_knob = "EMAIL_FROM_ADDRESS"
        sender = os.environ.get("EMAIL_FROM_ADDRESS", _DEFAULT_SENDER)
    else:
        transport = f"smtp ({smtp_host})"
        sender_knob = "SMTP_FROM"
        sender = os.environ.get("SMTP_FROM", _DEFAULT_SENDER)

    default_sender_note = (
        f"the sender defaults to {_DEFAULT_SENDER} — set {sender_knob}; relays commonly "
        "accept and then silently drop mail from an unverified sender"
    )

    if not email_to:
        if sender == _DEFAULT_SENDER:
            return _row(
                "email-delivery",
                "warning",
                f"email transport configured ({transport}) but {default_sender_note}. "
                "Pass email_to to send a real test message.",
            )
        return _row(
            "email-delivery",
            "info",
            f"email transport configured ({transport}, sender {sender}); delivery unverified — "
            "pass email_to to send a real test message",
        )

    # Same sender the login flows use — a bool, exceptions already logged.
    from app.auth.providers import password as password_provider

    accepted = password_provider._send_mail(
        email_to,
        "Deployment doctor test email",
        "This is a test message sent by the new-instance deployment doctor.\n"
        "If you are reading it, outbound email works on this instance.",
    )
    if not accepted:
        return _row(
            "email-delivery",
            "error",
            f"test email to {email_to} FAILED to send (transport {transport}) — the exception "
            "was logged server-side; check the app container logs for the smtplib/sendgrid error",
        )
    detail = (
        f"test email accepted for delivery to {email_to} via {transport}, sender {sender} — "
        "now confirm it actually arrived; acceptance by the relay is not delivery"
    )
    if sender == _DEFAULT_SENDER:
        return _row("email-delivery", "warning", f"{detail}. Also: {default_sender_note}.")
    return _row("email-delivery", "ok", detail)


def check_chat_grant(app) -> dict:
    """``chat.enabled`` implies an explicit ``(group, chat, chat)`` grant."""
    chat_config = getattr(app.state, "chat_config", None)
    if chat_config is None or not getattr(chat_config, "enabled", False):
        return _row("chat-grant", "info", "chat is disabled (chat.enabled) — nothing to verify")

    from src.repositories import resource_grants_repo

    grants = resource_grants_repo().list_all(resource_type="chat")
    holders = sorted({g["group_name"] for g in grants if g.get("resource_id") == "chat"})
    if holders:
        return _row("chat-grant", "ok", "chat is granted to group(s): " + ", ".join(holders))
    return _row(
        "chat-grant",
        "error",
        "chat.enabled is on but NO (group, chat, chat) resource grant exists — the chat entry "
        "point is invisible to every user INCLUDING admins (god-mode deliberately does not "
        "surface it). Fix: `agnes admin grant create <group> chat chat` or /admin/access.",
    )


def check_agent_scope() -> dict:
    """Every table-scoped agent has a non-empty owner-grants ∩ scope."""
    from src.agent_scope_intersection import compute_agent_intersection
    from src.repositories import agents_repo

    repo = agents_repo()
    broken: list[str] = []
    checked = 0
    for agent in repo.list():
        if agent.get("tables_mode") != "selected":
            continue
        scoped = {i["item_id"] for i in repo.get_scope(agent["id"]) if i.get("item_type") == "table"}
        if not scoped:
            # An empty allowlist is a deliberate "no tables", not a misconfig.
            continue
        checked += 1
        intersection = compute_agent_intersection(agent["owner_user_id"], agent).get("table", frozenset())
        if not intersection:
            label = agent.get("slug") or agent.get("name") or agent["id"]
            broken.append(f"{label} (owner {agent['owner_user_id']}, {len(scoped)} scoped table(s))")
    if broken:
        return _row(
            "agent-scope",
            "error",
            f"{len(broken)} agent(s) have an EMPTY owner-grants ∩ scope and will answer every "
            f'data question with 403 "not in your stack": {"; ".join(broken)}. Fix: grant the '
            "scoped tables as per-table resource grants "
            "(`agnes admin grant create <group> table <table_id>`) to a group the owner belongs "
            "to — data-package membership alone does not satisfy the agent intersection.",
        )
    if checked == 0:
        return _row("agent-scope", "info", "no table-scoped agent profiles to verify")
    return _row("agent-scope", "ok", f"{checked} table-scoped agent(s) verified — owner grants ∩ scope non-empty")


async def check_branding(app) -> dict:
    """When ``instance.brand`` is customized, the rendered login title follows."""
    from app.instance_config import get_instance_brand

    brand = get_instance_brand()
    if brand == "Agnes":
        return _row("branding", "info", "instance.brand is not customized (default product name) — nothing to verify")

    # Render the REAL page through the full stack — the failure this catches
    # is precisely "the config is set but the page still shows the default".
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/login")
    if resp.status_code != 200:
        return _row("branding", "error", f"could not render /login to verify branding (HTTP {resp.status_code})")

    match = re.search(r"<title>(.*?)</title>", resp.text, re.S)
    title = match.group(1).strip() if match else ""
    leaked = [d for d in ("AI Harness", "Data Analyst Portal") if d in title]
    if re.search(r"\bAgnes\b", title):
        leaked.append("Agnes")
    if leaked or not title:
        return _row(
            "branding",
            "error",
            f"instance.brand is {brand!r} but the rendered login page title is {title!r} — still a "
            "default. The login title reads instance.name (a different knob than instance.brand); "
            "set instance.name in instance.yaml to the customer-facing name as well.",
        )
    return _row("branding", "ok", f"login page title renders as {title!r}")


def check_app_state_backend() -> dict:
    """Since A1, fresh installs run app-state on Postgres — DuckDB is
    legacy-only for new deploys. This is a NEW-instance gate: an existing
    instance still persisted on DuckDB is expected to fail this check;
    that is correct and informative, not a bug.

    Uses ``use_pg()`` (src/repositories/__init__.py) rather than reading
    ``instance.yaml::database.backend`` directly: ``use_pg()`` also honors
    the ``DATABASE_URL``/``AGNES_DB_URL`` env-var fallback, which is how
    self-hosted-managed-Postgres deployments and the local/CI
    ``docker-compose.postgres.yml`` overlay both activate Postgres WITHOUT
    ever writing an ``instance.yaml`` — reading the raw persisted enum alone
    would misreport those as legacy DuckDB.
    """
    from app.instance_config import get_database_config
    from src.repositories import use_pg

    if use_pg():
        backend = get_database_config()["backend"]
        return _row("app-state-backend", "ok", f"app-state backend is Postgres (persisted backend={backend!r})")
    return _row(
        "app-state-backend",
        "error",
        "app-state backend is DuckDB — fresh installs must run Postgres app-state "
        "(see docs/QUICKSTART.md); DuckDB is legacy-only. If this is an existing instance "
        "that predates the Postgres default, this failure is expected and can be ignored.",
    )


def _isolated(name: str, fn: Callable[[], dict]) -> dict:
    """Run one check; a crashing resolver reports itself instead of dying."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — the whole point is containment
        logger.exception("new-instance doctor check %s crashed", name)
        return _row(name, "error", f"check crashed: {e}")


def aggregate_status(checks: list[dict]) -> str:
    statuses = {c["status"] for c in checks}
    if "error" in statuses:
        return "error"
    if "warning" in statuses:
        return "warning"
    return "ok"


async def run_new_instance_doctor(app, email_to: Optional[str] = None) -> dict:
    """All six checks, blocking work off the event loop, one report."""
    from anyio import to_thread

    checks: list[dict] = []
    sync_checks: list[tuple[str, Callable[[], dict]]] = [
        ("login-door", check_login_door),
        ("email-delivery", lambda: check_email_delivery(email_to)),
        ("chat-grant", lambda: check_chat_grant(app)),
        ("agent-scope", check_agent_scope),
        ("app-state-backend", check_app_state_backend),
    ]
    for name, fn in sync_checks:
        checks.append(await to_thread.run_sync(_isolated, name, fn))
    try:
        checks.append(await check_branding(app))
    except Exception as e:  # noqa: BLE001 — same containment as _isolated
        logger.exception("new-instance doctor check branding crashed")
        checks.append(_row("branding", "error", f"check crashed: {e}"))
    return {"status": aggregate_status(checks), "checks": checks}
