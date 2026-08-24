"""Text-assertion contract for the root `Caddyfile`'s `/metrics` deny rule.

`GET /metrics` (app/observability/metrics.py) is an unauthenticated,
internal-scrape-only surface — docs/observability.md: "Operators must not
expose this endpoint publicly." The mtier proxy (deploy/caddy/Caddyfile.mtier)
has denied it since wave 2D, but the single-VM TLS proxy — the root
`Caddyfile`, used by every `tls_mode=caddy` deployment — forwarded it to the
app along with everything else, publishing queue depth, worker-lane occupancy,
replica hostname:pid, and per-route latency histograms to the open internet.

Same test style as tests/test_caddyfile_mtier.py: no Caddy binary/Docker
dependency, lightweight structure assertions so a future edit can't silently
drop the deny rule.
"""

from __future__ import annotations

from pathlib import Path

_CADDYFILE = Path(__file__).resolve().parent.parent / "Caddyfile"


def _text() -> str:
    return _CADDYFILE.read_text()


def _primary_site_block(text: str) -> str:
    """The first site block only — the deny must live in the primary block.

    The legacy DOMAIN_ALIAS block 308-redirects everything to the primary
    domain, so a deny there would be dead config; asserting on the slice
    before it keeps the test honest about WHERE the rule sits.
    """
    marker = "{$DOMAIN_ALIAS"
    return text.split(marker)[0]


def test_metrics_denied_with_404_in_primary_site_block():
    block = _primary_site_block(_text())
    assert "@metrics path /metrics" in block
    assert "respond @metrics 404" in block


def test_health_probes_stay_reachable():
    # /healthz + /readyz leak only status and double as LB/uptime probes —
    # the deny must stay scoped to /metrics alone (mirrors Caddyfile.mtier).
    text = _text()
    assert "path /healthz" not in text
    assert "path /readyz" not in text


def test_app_reverse_proxy_untouched():
    block = _primary_site_block(_text())
    assert "reverse_proxy app:8000" in block
