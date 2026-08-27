"""D1 (config-ownership remediation, Half 2): theme/home_route/studio/
experience move from an always-wins `.env` line to the first-boot-only
`instance.yaml` seed — the same "seed, don't reassert" pattern the branding
fields (logo/brand/subtitle/copyright/favicon/custom_scripts) already use.

Before this change, ``startup-script.sh.tpl`` rewrote these into
``/opt/agnes/.env`` on EVERY boot (recreate, apply, auto-upgrade tick), which
permanently shadowed the admin UI's own `/admin/server-config` control over
the same knob — any Terraform-set value could never be changed by an admin
without hand-editing `.env` on the VM. Folding them into the existing
first-boot instance.yaml seed means: Terraform still sets day-1 value, and
the admin UI owns it from day 2 onward (env still wins if an operator sets it
by hand — no app-side precedence change, see app/instance_config.py).

Same lightweight read-the-template pattern as the toggle-specific tests this
supersedes for the `.env`-line assertions
(``test_startup_studio_toggle.py``, ``test_startup_experience_toggle.py``,
``test_startup_ui_layout_theme_toggle.py`` — updated alongside this file to
drop their now-wrong "the tpl writes an env line" assertions).

D1 residual (2026-08): `data_source.type` gets the same handoff, added here
rather than a new file since it's the same slice of work. Unlike the four
knobs above, the `data_source` template var itself keeps flowing into
startup-script.sh.tpl — it still gates the boot-time keboola-storage-token
secret fetch — only the `.env` line it used to grow is gone. This is also
the one knob where the app-side precedence flips (overlay wins over
`DATA_SOURCE` env, not just "env still wins if hand-set") — see
app/instance_config.py::get_data_source_type() and tests/test_instance_config.py.
"""

import re
import subprocess
import sys
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")
TPL_TEXT = (MODULE / "startup-script.sh.tpl").read_text()
MAIN_TF_TEXT = (MODULE / "main.tf").read_text()


def _env_heredoc_block() -> str:
    """The body of `cat > "$APP_DIR/.env" <<ENVEOF ... ENVEOF` — isolated so
    a check for "is DATA_SOURCE written to .env" can't be fooled by
    `$DATA_SOURCE` uses elsewhere in the script (the bash variable itself,
    the boot-time keboola-token gate)."""
    m = re.search(r'cat > "\$APP_DIR/\.env" <<ENVEOF\n(.*?)\nENVEOF\n', TPL_TEXT, re.DOTALL)
    assert m, "the .env heredoc was not found in startup-script.sh.tpl"
    return m.group(1)


class TestEnvLinesRemoved:
    """None of the four in-scope knobs may write an always-wins `.env` line
    anymore — every boot (recreate/apply/auto-upgrade) rewrites `.env`, so
    any such line permanently shadows the admin UI's control of the same
    knob."""

    def test_no_home_route_env_line(self):
        assert "AGNES_HOME_ROUTE" not in TPL_TEXT

    def test_no_studio_enabled_env_line(self):
        assert "AGNES_STUDIO_ENABLED" not in TPL_TEXT

    def test_no_instance_theme_env_line(self):
        assert "AGNES_INSTANCE_THEME" not in TPL_TEXT

    def test_no_instance_experience_env_line(self):
        assert "AGNES_INSTANCE_EXPERIENCE" not in TPL_TEXT

    def test_chat_provider_env_line_is_unaffected(self):
        """Explicitly OUT of scope: chat_provider pins deployment-provisioned
        backing (the kai-agent sidecar / apps-runner) — it stays an
        always-wins env line, unlike the four presentation-only knobs above."""
        assert re.search(
            r'%\{\s*if\s+chat_provider\s*!=\s*""\s*~?\}\s*\nAGNES_CHAT_PROVIDER=\$\{chat_provider\}\s*\n%\{\s*endif\s*~?\}',
            TPL_TEXT,
        ), "chat_provider must keep writing its env line — it is not part of this slice"

    def test_no_data_source_env_line(self):
        """D1 residual: the `.env` heredoc must not assign DATA_SOURCE=...
        anymore. The bash variable `$DATA_SOURCE` itself is allowed to
        survive elsewhere in the script (it still gates the boot-time
        keboola-storage-token secret fetch) — only the env-line write is
        gone."""
        heredoc = _env_heredoc_block()
        assert "DATA_SOURCE=" not in heredoc
        # The gating use further up the script is unaffected.
        assert 'if [ "$DATA_SOURCE" = "keboola" ]' in TPL_TEXT


def _first_boot_guard_span() -> tuple[int, int]:
    """Start/end character offsets of the `[ ! -f "$INSTANCE_YAML" ]`
    first-boot guard's `if ... fi` body in the template — the ONE branch
    that only ever runs on a brand-new VM."""
    m = re.search(r'if \[ ! -f "\$INSTANCE_YAML" \]; then\n.*?\nfi\n', TPL_TEXT, re.DOTALL)
    assert m, "first-boot instance.yaml guard not found in startup-script.sh.tpl"
    return m.start(), m.end()


def _data_source_backfill_block() -> str:
    """The unconditional, idempotent boot-time backfill that migrates an
    EXISTING instance.yaml (one predating the first-boot seed) — bounded by
    its own heading comment and the next numbered section's heading, so the
    extraction does not depend on counting nested `if`/`fi` pairs."""
    m = re.search(r"# --- 2b\. Backfill .*?(?=\n# --- 3\.)", TPL_TEXT, re.DOTALL)
    assert m, "data_source.type backfill block (section 2b) not found in startup-script.sh.tpl"
    return m.group(0)


class TestExistingVmDataSourceBackfill:
    """Regression for the BLOCKING gap the precedence flip alone left open:
    a VM provisioned BEFORE this change already has an instance.yaml, so the
    first-boot seed (section 2) is skipped forever on it — and since the
    `.env` heredoc (section 4) no longer writes `DATA_SOURCE=...` on ANY
    boot, that VM's overlay AND `.env` both go empty the moment `.env` is
    next rewritten (recreate/apply/auto-upgrade tick), and
    get_data_source_type() silently falls back to `"local"`. Section 2b is
    the fix: an unconditional, idempotent backfill that reaches already-
    deployed VMs too."""

    def test_backfill_block_exists(self):
        block = _data_source_backfill_block()
        assert "data_source" in block
        assert "import yaml" in block

    def test_backfill_is_outside_the_first_boot_guard(self):
        """Nesting the backfill inside `if [ ! -f "$INSTANCE_YAML" ]` would
        defeat its entire purpose — that guard is exactly what makes the
        seed skip an existing VM. Assert both that the guard's own body does
        not contain the backfill's heading (the guard's prose legitimately
        MENTIONS `data_source.type` as part of what the seed blob carries,
        so that substring alone is not a safe marker), and that the
        backfill starts strictly after the guard closes."""
        guard_start, guard_end = _first_boot_guard_span()
        guard_body = TPL_TEXT[guard_start:guard_end]
        assert "# --- 2b. Backfill" not in guard_body

        backfill_start = TPL_TEXT.index("# --- 2b. Backfill")
        assert backfill_start > guard_end, (
            "the data_source.type backfill must be placed AFTER (and outside) the first-boot instance.yaml guard closes"
        )

    def test_backfill_guards_empty_data_source(self):
        """No `var.data_source` configured must write nothing — not an
        implicit 'local' clobbering an intentional absence."""
        block = _data_source_backfill_block()
        assert '[ -n "$DATA_SOURCE" ]' in block

    def test_backfill_only_fires_when_key_absent(self):
        """Permanent no-op once `data_source.type` exists — from this
        backfill, the first-boot seed, or a later admin edit."""
        block = _data_source_backfill_block()
        assert 'data_source.get("type")' in block

    def test_backfill_uses_atomic_tmp_rename(self):
        """Same non-destructive write shape as write_instance_yaml in
        scripts/ops/agnes-state-applier.sh — never a truncating in-place
        write that could lose the file mid-write."""
        block = _data_source_backfill_block()
        assert 'tmp = path + ".tmp"' in block
        assert "os.replace(tmp, path)" in block


def _templatefile_call_block() -> str:
    """The `templatefile("${path.module}/startup-script.sh.tpl", { ... })`
    vars-map argument, brace-matched — isolated from the `instance_brand_scalars`
    local (which legitimately reads `var.home_route` / `inst.theme` /
    `inst.experience` to build the seed blob)."""
    m = re.search(r'templatefile\("\$\{path\.module\}/startup-script\.sh\.tpl",\s*\{', MAIN_TF_TEXT)
    assert m, "templatefile(...) call for startup-script.sh.tpl not found"
    depth, i = 1, m.end()
    while i < len(MAIN_TF_TEXT) and depth:
        if MAIN_TF_TEXT[i] == "{":
            depth += 1
        elif MAIN_TF_TEXT[i] == "}":
            depth -= 1
        i += 1
    return MAIN_TF_TEXT[m.start() : i]


class TestTemplateVarsNoLongerForwarded:
    """The four knobs are no longer separate `templatefile()` arguments —
    they are folded into the `instance_branding_b64` seed blob instead, so
    passing them again as raw vars would be dead weight the template no
    longer references (and `templatefile()` rejects unused vars)."""

    def test_theme_and_experience_not_forwarded_per_vm(self):
        block = _templatefile_call_block()
        assert not re.search(r"theme\s*=\s*each\.value\.theme", block)
        assert not re.search(r"experience\s*=\s*each\.value\.experience", block)

    def test_home_route_and_studio_enabled_not_forwarded(self):
        block = _templatefile_call_block()
        assert not re.search(r"^\s*home_route\s*=", block, re.MULTILINE)
        assert not re.search(r"^\s*studio_enabled\s*=", block, re.MULTILINE)


class TestFirstBootSeedCarriesTheKnobs:
    """The `instance_brand_scalars` / `instance_studio_map` locals — folded
    into the same `instance_branding_b64` blob the startup script appends
    only inside its `[ ! -f "$INSTANCE_YAML" ]` branch — are where the four
    knobs now live."""

    def test_instance_brand_scalars_includes_theme_experience_home_route(self):
        m = re.search(r"instance_brand_scalars\s*=\s*\{.*?\n  \}\n", MAIN_TF_TEXT, re.DOTALL)
        assert m, "instance_brand_scalars local not found"
        block = m.group(0)
        assert re.search(r'theme\s*=\s*try\(inst\.theme,\s*""\)', block)
        assert re.search(r'experience\s*=\s*try\(inst\.experience,\s*""\)', block)
        assert re.search(r"home_route\s*=\s*var\.home_route", block)

    def test_instance_studio_map_local_exists(self):
        assert re.search(
            r"instance_studio_map\s*=\s*var\.studio_enabled\s*\?\s*\{\}\s*:\s*\{\s*enabled\s*=\s*false\s*\}",
            MAIN_TF_TEXT,
        ), (
            "instance_studio_map must omit the block entirely when studio_enabled "
            "is true (the default) so a non-customized instance's seed stays "
            "byte-for-byte unchanged"
        )

    def test_instance_branding_map_merges_in_the_studio_block(self):
        m = re.search(r"instance_branding_map\s*=\s*\{.*?\n  \}\n", MAIN_TF_TEXT, re.DOTALL)
        assert m, "instance_branding_map local not found"
        assert "instance_studio_map" in m.group(0)

    def test_instance_data_source_map_local_exists(self):
        """D1 residual: same conditional shape as instance_studio_map —
        omit the block entirely when var.data_source is empty (never true
        given the "keboola" default, but mirrors the other locals' guard
        style) so an unset knob doesn't grow the seed."""
        assert re.search(
            r'instance_data_source_map\s*=\s*var\.data_source\s*!=\s*""\s*\?\s*\{\s*type\s*=\s*var\.data_source\s*\}\s*:\s*\{\}',
            MAIN_TF_TEXT,
        ), "instance_data_source_map local not found or has an unexpected shape"

    def test_instance_branding_map_merges_in_the_data_source_block(self):
        m = re.search(r"instance_branding_map\s*=\s*\{.*?\n  \}\n", MAIN_TF_TEXT, re.DOTALL)
        assert m, "instance_branding_map local not found"
        assert "instance_data_source_map" in m.group(0)


class TestVariableDescriptionsNameTheOwnershipHandoff:
    """variables.tf descriptions must stop implying these are re-asserted
    every boot — an operator reading the description should learn the value
    only seeds day-1 and the admin UI owns it after that."""

    def test_home_route_description_mentions_first_boot_seed(self):
        vars_text = (MODULE / "variables.tf").read_text()
        m = re.search(r'variable\s+"home_route"\s*\{(.*?)\n\}', vars_text, re.DOTALL)
        assert m
        assert "first-boot seed" in m.group(1).lower()

    def test_studio_enabled_description_mentions_first_boot_seed(self):
        vars_text = (MODULE / "variables.tf").read_text()
        m = re.search(r'variable\s+"studio_enabled"\s*\{(.*?)\n\}', vars_text, re.DOTALL)
        assert m
        assert "first-boot seed" in m.group(1).lower()

    def test_data_source_description_mentions_first_boot_seed_and_breaking(self):
        vars_text = (MODULE / "variables.tf").read_text()
        m = re.search(r'variable\s+"data_source"\s*\{(.*?)\n\}', vars_text, re.DOTALL)
        assert m
        description = m.group(1).lower()
        assert "first-boot seed" in description
        assert "breaking" in description


def _extract_backfill_python_snippet() -> str:
    """The PyYAML script embedded in section 2b's `python3 - ... <<'PY'`
    heredoc, pulled out so it can be executed directly (as
    `python3 -c <snippet> <path> <value>`, matching the heredoc's own
    `sys.argv[1]`/`sys.argv[2]` positions) against real fixture files
    instead of re-implementing a bash interpreter for this test."""
    block = _data_source_backfill_block()
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", block, re.DOTALL)
    assert m, "PyYAML heredoc not found inside the data_source.type backfill block"
    return m.group(1)


class TestExistingVmDataSourceBackfillBehavior:
    """Executes the ACTUAL embedded PyYAML snippet (not a re-implementation)
    against real fixture instance.yaml files, so a future edit to the
    heredoc that breaks its logic fails here even if the static assertions
    above still pass."""

    def _run(self, tmp_path, initial_yaml: str, value: str = "keboola") -> str:
        instance_yaml = tmp_path / "instance.yaml"
        instance_yaml.write_text(initial_yaml)
        snippet = _extract_backfill_python_snippet()
        result = subprocess.run(
            [sys.executable, "-c", snippet, str(instance_yaml), value],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"backfill snippet failed: {result.stderr}"
        return instance_yaml.read_text()

    def test_writes_data_source_type_when_absent(self, tmp_path):
        after = self._run(tmp_path, "database:\n  backend: side_car\n", value="bigquery")
        assert "data_source" in after
        assert "type: bigquery" in after
        # The unrelated key the overlay already carried must survive —
        # this is a key-scoped merge, never a from-scratch rewrite.
        assert "backend: side_car" in after

    def test_no_change_when_already_present(self, tmp_path):
        before = "database:\n  backend: side_car\ndata_source:\n  type: keboola\n"
        after = self._run(tmp_path, before, value="bigquery")
        # Permanent no-op: an already-set value is never overwritten, even
        # by a DIFFERENT $DATA_SOURCE — this is what stops the backfill
        # from ever re-shadowing a deliberate later UI edit.
        assert "type: keboola" in after
        assert "type: bigquery" not in after

    def test_preserves_unrelated_top_level_keys(self, tmp_path):
        before = "database:\n  backend: side_car\nlogging:\n  level: debug\n"
        after = self._run(tmp_path, before, value="keboola")
        assert "level: debug" in after
        assert "type: keboola" in after
