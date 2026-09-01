"""JSON schema for LLM structured output from the verification detector.

Confidence is intentionally NOT part of this schema. It is derived in code from
(source_type, detection_type) via services.corporate_memory.confidence — the LLM
is not trusted to set its own credibility (see docs/archive/pd-ps-comments.md Q3).

``scope`` (issue #1971 Part 5) is likewise a MODEL-PROPOSED label, not a
decision: the model reports whether a fact reads as organization-wide
("general") or tied to a single client/engagement ("engagement"), but the
ROUTING that label drives — which memory domain the item lands in — is
deterministic code in ``services/session_processors/verification.py``, never
the model. An "engagement" item is still returned (never silently dropped by
the model) so that code has something to route.
"""

VERIFICATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "detection_type": {
                        "type": "string",
                        "enum": ["correction", "confirmation", "unprompted_definition"],
                    },
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "user_quote": {"type": "string"},
                    "domain": {
                        "type": "string",
                        "enum": [
                            "finance",
                            "engineering",
                            "product",
                            "data",
                            "operations",
                            "infrastructure",
                        ],
                    },
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "scope": {
                        "type": "string",
                        "enum": ["general", "engagement"],
                    },
                },
                "required": [
                    "detection_type",
                    "title",
                    "content",
                    "user_quote",
                    "domain",
                    "entities",
                    "scope",
                ],
            },
        }
    },
    "required": ["verifications"],
}
