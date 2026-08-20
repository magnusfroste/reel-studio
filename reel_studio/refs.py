"""Semantic, human-readable references for observed browser elements."""

import re
from collections.abc import MutableSet


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value or "element"


def semantic_ref(role: str, text: str, index: int, used: MutableSet[str]) -> str:
    """Return a stable-ish ref that tells an agent what the element is.

    The index is retained only as a final fallback. Duplicate visible labels are
    disambiguated with a numeric suffix.
    """
    base = f"{_slug(role)}:{_slug(text)}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
