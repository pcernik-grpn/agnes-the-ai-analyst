#!/usr/bin/env bash
# Smoke check: /catalog (now a 302 into /library?scope=available — the
# Catalog/Marketplace fold) lands on a fully rendered Library, the Scope
# segment is applied, and the section disclosures respond to clicks.
#
# History: this script asserted catalog_unified.html's kind tabs, and before
# that a "Browse"/"My Stack" pair that belonged to /marketplace — both pages
# are retired now. If the Library reshapes again, repoint the assertions at
# whatever the redirect actually lands on; do not resurrect the old markup.

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-./e2e-artifacts}"
mkdir -p "$ARTIFACTS_DIR"

# Bail early if agent-browser isn't installed — surfaces a clearer error
# than the daemon-init failure that follows.
if ! command -v agent-browser >/dev/null 2>&1; then
  echo "::error::agent-browser CLI missing — run 'npm i -g agent-browser && agent-browser install'."
  exit 2
fi

# Use a temporary, isolated session so two parallel scripts can't clobber
# each other's cookies.
SESSION="agnes-e2e-$$"
trap 'agent-browser --session "$SESSION" close >/dev/null 2>&1 || true' EXIT

# Sign the session in before hitting a protected page — /catalog otherwise
# 401-redirects to /login.
source "$(dirname "$0")/_login.sh"

echo "→ open ${BASE_URL}/catalog"
agent-browser --session "$SESSION" open "${BASE_URL}/catalog"

echo "→ screenshot landing"
agent-browser --session "$SESSION" screenshot "$ARTIFACTS_DIR/catalog-landing.png"

# /catalog is a 302 into /library?scope=available since the Catalog/
# Marketplace fold — the smoke's job is now: the redirect lands on a fully
# rendered Library with the "Not added yet" filter applied (the chip is
# the visible proof — the acquisition question demoted off the Scope
# segment into the Filter menu when the segment went two-state), and its
# section disclosures actually work. (This script's previous life asserted
# catalog_unified.html's kind tabs; that template is gone.)
#
# Since the library redesign (Knowledge / Capabilities tabbed shell,
# ek/library-redesign, baseline commit c9a0478e), the landing opens on the
# Knowledge tab and plugin content lives under Capabilities — "Plugins" is
# NOT on the landing anymore (issue #1940 caught this script still
# asserting it). What the landing does guarantee: the tab strip renders
# both tabs unconditionally (app/web/router.py `library_tabs`), and the
# Capabilities count is ≥ 1 on a bare, freshly-seeded instance because the
# built-in marketplace is seeded on every boot and granted to
# Admin/Everyone (app/main.py → src.marketplace.seed_builtin_marketplace).
# Knowledge, by contrast, is legitimately empty here ("Nothing in
# Knowledge yet") — do not assert any Knowledge-side row without seeding
# one first; that shape of fixture-blind needle is what made this smoke
# fail nightly from 2026-08-21 (issues #1497 et al).
echo "→ snapshot landing — the folded Library, availability filter pre-applied"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
for NEEDLE in "Not added yet" Capabilities; do
  if ! grep -qi "$NEEDLE" <<<"$SNAPSHOT"; then
    echo "::error::'${NEEDLE}' missing from the folded /catalog landing (library scope view)."
    echo "$SNAPSHOT" | head -40
    exit 1
  fi
done

# Switch to the Capabilities tab: the tab buttons carry data-own="<key>"
# inside #lib-tabs (library.html renders them from `library_tabs`); the
# seeded built-in marketplace guarantees the Plugins section band renders
# there.
echo "→ click the Capabilities tab"
agent-browser --session "$SESSION" click '#lib-tabs [data-own=capabilities]'
agent-browser --session "$SESSION" wait 500

echo "→ verify the Plugins section is visible under Capabilities"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qi "Plugins" <<<"$SNAPSHOT"; then
  echo "::error::'Plugins' missing after switching to the Capabilities tab."
  echo "$SNAPSHOT" | head -40
  exit 1
fi

# Section disclosures still respond to clicks. Bands now ship EXPANDED
# (library.html renders the group toggle with aria-expanded="true"), so
# the click collapses it; the accessibility snapshot exposes the flip as
# an [expanded=false] marker on the toggle button, whose accessible name
# carries the band label ("Plugins …").
echo "→ click the Plugins section toggle (collapse)"
agent-browser --session "$SESSION" click '[data-sec-toggle=plugin]'
agent-browser --session "$SESSION" wait 500

echo "→ verify the Plugins section collapsed"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qiE 'plugins[^][]*\[[^]]*expanded=false' <<<"$SNAPSHOT"; then
  echo "::error::Clicking the Plugins section toggle didn't collapse it."
  echo "$SNAPSHOT" | head -40
  exit 1
fi

echo "✓ /catalog smoke passed (folded Library, Capabilities tab)."
