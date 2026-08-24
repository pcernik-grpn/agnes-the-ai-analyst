"""Admin doctor endpoints.

- ``POST /api/admin/doctor/new-instance`` — the deployment-gate doctor
  (active pass/fail checks; logic in ``app/services/instance_doctor.py``).
  Host-side siblings (COMPOSE_FILE↔instance.yaml consistency, TLS predicate
  agreement) live in ``scripts/ops/post-deploy-smoke-test.sh``, which calls
  this endpoint for the server-side half.
- ``GET /api/admin/doctor/support`` — the support-bundle doctor (redacted
  state snapshot; logic in ``app/services/support_bundle.py``). Feeds the
  server section of ``agnes doctor``.

Both admin-only, both deliberately never MCP-exposed (operator
security-posture diagnostics exemption — see CONTRIBUTING.md).
"""

from typing import Optional

from anyio import to_thread
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.access import require_admin
from app.services.instance_doctor import run_new_instance_doctor
from app.services.support_bundle import build_support_bundle

router = APIRouter(prefix="/api/admin/doctor", tags=["admin"])


class NewInstanceDoctorRequest(BaseModel):
    # When set, the email-delivery check sends a real test message to this
    # address through the same ``_send_mail`` path the login flows use —
    # the only honest answer to "can this instance send email", because the
    # send endpoints return 200 even when the relay drops the message.
    email_to: Optional[str] = None


@router.post("/new-instance")
async def doctor_new_instance(
    request: Request,
    body: Optional[NewInstanceDoctorRequest] = None,
    _user: dict = Depends(require_admin),
):
    """Run the new-instance deployment checks and return a verdict per check.

    Response: ``{status, checks: [{name, status, audience, detail}]}`` with
    ``status ∈ {ok, warning, error, info}`` per check (the ``agnes diagnose``
    vocabulary) and the headline aggregating the worst check. Checks:
    ``login-door``, ``email-delivery``, ``chat-grant``, ``agent-scope``,
    ``branding``. Optional body ``{"email_to": ...}`` makes the
    email-delivery check send a real test message.
    """
    email_to = body.email_to if body else None
    return await run_new_instance_doctor(request.app, email_to=email_to)


@router.get("/support")
async def doctor_support(_user: dict = Depends(require_admin)):
    """Collect the redacted support-bundle snapshot (server section of `agnes doctor`).

    Response sections: ``build`` (version/channel/image tag/commit/deployed_at),
    ``schema`` (backend + current vs expected migration verdict), ``retrieval``
    (``hybrid``/``lexical_only`` — the latter with ``status: warning`` so the
    degradation is loud), ``sync`` (per-``source_type`` rollup: table/ok/error/
    stale/never_synced counts, newest ``last_sync_max``, and ``last_errors`` —
    a sample of up to 5 failing tables in registry name order; failure time is
    not recorded, so the sample is capped, not recency-sorted, while the
    ``errors`` count stays exact), ``disk`` (data-dir filesystem usage + DB file sizes),
    ``process`` (state backend, roles, python), and ``secrets`` (env-var
    **presence booleans only — never values**). Each section is collected in
    isolation; a crashing collector reports ``{"status": "error"}`` for its
    section instead of failing the request.
    """
    return await to_thread.run_sync(build_support_bundle)
