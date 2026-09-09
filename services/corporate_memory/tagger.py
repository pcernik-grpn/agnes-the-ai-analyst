"""Automatic topic tagging for corporate memory knowledge items.

Uses Haiku to assign topics from a shared vocabulary to knowledge items.
Topics are stored in the existing `tags` field alongside free-form keywords.
The vocabulary is hardcoded for V1; a future admin UI will let operators
extend or replace it without a code change.
"""

import logging
from typing import Any

from connectors.llm.exceptions import LLMError

logger = logging.getLogger(__name__)

# Starter topic vocabulary. Intentionally broad so most knowledge items map to
# at least one topic. Future: expose via instance.yaml so operators can
# customise without a code deploy.
TOPIC_VOCABULARY: list[str] = [
    "data",
    "automation",
    "reports",
    "alerts",
    "metrics",
    "queries",
    "infrastructure",
    "processes",
    "integrations",
    "debugging",
    "performance",
    "access",
]

# JSON schema for Haiku structured output — batch assignment
_TOPIC_TAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "topics": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["id", "topics"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def _build_prompt(items: list[dict], vocabulary: list[str]) -> str:
    vocab_list = ", ".join(f'"{t}"' for t in vocabulary)
    items_text = "\n".join(
        f"- id={item['id']} | title={item.get('title', '')} | content={item.get('content', '')[:200]}" for item in items
    )
    return (
        f"Assign topics from the vocabulary to each knowledge item.\n\n"
        f"Vocabulary (use ONLY these values): [{vocab_list}]\n\n"
        f"Items:\n{items_text}\n\n"
        f"Rules:\n"
        f"- Assign 1-3 topics per item from the vocabulary above.\n"
        f"- Pick topics that describe the SUBJECT of the item, not how it was written.\n"
        f"- If nothing fits, assign the single closest match.\n"
        f"- Return every item id exactly once."
    )


def auto_tag_items(items: list[dict], extractor: Any) -> dict[str, list[str]]:
    """Batch-tag items with topics from TOPIC_VOCABULARY.

    Returns a mapping {item_id: [topic, ...]}. On any LLM error returns {}
    so callers can treat tagging as best-effort.

    Args:
        items: List of dicts with at least 'id', 'title', 'content'.
        extractor: LLM extractor (connectors.llm.BaseExtractor).
    """
    if not items:
        return {}

    from src.observability.llm_context import llm_context

    prompt = _build_prompt(items, TOPIC_VOCABULARY)
    try:
        with llm_context(workload="corporate_memory", purpose="tagger"):
            result = extractor.extract_json(
                prompt,
                max_tokens=1024,
                json_schema=_TOPIC_TAG_SCHEMA,
                schema_name="topic_tag_assignment",
            )
    except LLMError as e:
        logger.warning("auto_tag_items: LLM error — %s", type(e).__name__)
        return {}
    except Exception as e:
        logger.warning("auto_tag_items: unexpected error — %s", e)
        return {}

    assignments: dict[str, list[str]] = {}
    vocab_set = set(TOPIC_VOCABULARY)
    for entry in result.get("assignments", []):
        item_id = entry.get("id", "")
        if not item_id:
            continue
        # Keep only vocabulary terms; drop any hallucinated values
        valid_topics = [t for t in (entry.get("topics") or []) if t in vocab_set]
        assignments[item_id] = valid_topics

    return assignments
