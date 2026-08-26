"""Static contract for the chat-provider pin's Terraform → startup plumbing.

``chat_provider`` codifies which engine runs an instance's web-chat sessions
IN TERRAFORM: the per-VM module field writes ``AGNES_CHAT_PROVIDER`` into the
app ``.env``, and ``load_chat_config`` resolves env > instance.yaml >
"kai-agent".
Without it the provider choice lives only in the hand-edited instance.yaml
overlay on the data disk — which survives reboots and recreates but not a
fresh data disk, and is invisible in review.

Same read-the-template pattern as ``test_startup_experience_toggle.py``,
whose field this one mirrors — per-VM (dev-first rollout), empty default
writes NO env line (an ``AGNES_CHAT_PROVIDER=`` empty line is treated as
unset by the resolver, but writing it anyway would imply a pin that does not
exist), and the Terraform allowlist must track the app's boot allowlist in
``app/main.py`` so a typo fails the plan instead of refusing the ChatManager
at runtime on the VM.
"""

import re
from pathlib import Path

MODULE = Path("infra/modules/customer-instance")


def _object_type_blocks(body: str) -> list[str]:
    """Return the prod_instance + dev_instances object-type declarations."""
    blocks = []
    for var in ("prod_instance", "dev_instances"):
        m = re.search(rf'variable\s+"{var}"\s*\{{', body)
        assert m, f"variables.tf must declare {var}"
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "{":
                depth += 1
            elif body[i] == "}":
                depth -= 1
            i += 1
        blocks.append(body[m.start() : i])
    return blocks


def test_both_object_types_declare_the_chat_provider_field():
    body = (MODULE / "variables.tf").read_text()
    decls = re.findall(r'chat_provider\s*=\s*optional\(string,\s*""\)', body)
    assert len(decls) == 2, f"expected chat_provider optional on prod+dev object types, got {len(decls)}"
    # NOT a module-global variable (a provider pin is a per-VM rollout choice).
    assert not re.search(r'variable\s+"chat_provider"\s*\{', body)


def test_main_tf_forwards_chat_provider_per_vm():
    body = (MODULE / "main.tf").read_text()
    assert re.search(r"chat_provider\s*=\s*each\.value\.chat_provider", body)
    assert not re.search(r"chat_provider\s*=\s*var\.chat_provider", body)


def test_tpl_emits_the_env_line_only_when_set():
    body = (MODULE / "startup-script.sh.tpl").read_text()
    # Guarded, so the empty default writes NO line and the instance keeps
    # following instance.yaml / the app default.
    assert '%{ if chat_provider != "" ~}' in body
    assert "AGNES_CHAT_PROVIDER=${chat_provider}" in body
    guard = body.index('%{ if chat_provider != "" ~}')
    line = body.index("AGNES_CHAT_PROVIDER=${chat_provider}")
    endif = body.index("%{ endif ~}", line)
    assert guard < line < endif


def test_tf_allowlist_matches_the_apps_boot_allowlist():
    """The Terraform validation and app/main.py's provider allowlist must
    accept the same set — a value the plan admits but boot refuses turns a
    typo into a VM whose every chat route 503s."""
    vbody = (MODULE / "variables.tf").read_text()
    tf_values = set(re.findall(r'contains\(\["", "docker", "kai-agent"\][^)]*chat_provider\)', vbody))
    assert len(tf_values) >= 1, "chat_provider allowlist validation missing"
    prod_block, dev_block = _object_type_blocks(vbody)
    assert "chat_provider" in prod_block and "chat_provider" in dev_block
    app_main = Path("app/main.py").read_text()
    assert 'not in ("docker", "kai-agent")' in app_main, (
        "app/main.py's provider allowlist changed — update the Terraform validation to match"
    )


def test_kai_agent_pin_requires_the_engine_on_the_same_vm():
    """chat_provider=kai-agent on a VM without kai_agent_enabled would refuse
    every session at mint time — must fail the plan, on both object types."""
    body = (MODULE / "variables.tf").read_text()
    pairings = re.findall(
        r'chat_provider\s*!=\s*"kai-agent"\s*\|\|\s*(?:var\.prod_instance\.|i\.)kai_agent_enabled', body
    )
    assert len(pairings) == 2, f"expected the kai-agent↔engine pairing validated on prod+dev, got {len(pairings)}"
