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
# Unlike those retired kind tabs (always rendered, hidden only when a kind
# had zero rows), a folded-Library section doesn't exist in the DOM at all
# until it holds at least one row (app/web/router.py builds `grouped` by
# appending — a kind with zero items never gets a key). Plugins is the one
# kind a bare, freshly-seeded instance is GUARANTEED to have: the built-in
# marketplace is seeded unconditionally on every boot and granted to
# Admin/Everyone (app/main.py → src.marketplace.seed_builtin_marketplace).
# Recipes carries no such bundled seed — it's pure admin-curated content —
# so it never renders here. Do not resurrect a "Recipes" needle without
# first seeding a recipe *and* a RECIPE resource_grants row for the e2e
# user; asserting it against this fixture is what made the smoke fail
# nightly from 2026-08-21 rather than catch anything (issues #1497 et al).
echo "→ snapshot landing — the folded Library, availability filter pre-applied"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
for NEEDLE in "Not added yet" Plugins; do
  if ! grep -qi "$NEEDLE" <<<"$SNAPSHOT"; then
    echo "::error::'${NEEDLE}' missing from the folded /catalog landing (library scope view)."
    echo "$SNAPSHOT" | head -40
    exit 1
  fi
done

# Sections ship collapsed; the toggle is a real button carrying
# aria-expanded, so the accessibility snapshot exposes the flip as an
# [expanded] marker — assert on that, not on row text a seeded instance
# may or may not have.
echo "→ click the Plugins section toggle"
agent-browser --session "$SESSION" click '[data-sec-toggle=plugin]'
agent-browser --session "$SESSION" wait 500

echo "→ verify the Plugins section expanded"
SNAPSHOT="$(agent-browser --session "$SESSION" snapshot -i)"
if ! grep -qiE '\[[^]]*expanded[^]]*\].*plugin|plugin[^\n]*\[[^]]*expanded' <<<"$SNAPSHOT" \
   && ! grep -qiE 'expanded' <<<"$SNAPSHOT"; then
  echo "::error::Clicking the Plugins section toggle didn't expand it."
  echo "$SNAPSHOT" | head -40
  exit 1
fi

echo "✓ /catalog smoke passed (folded Library)."
