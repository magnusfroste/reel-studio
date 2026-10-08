from typing import Any, Literal, get_args

from pydantic import BaseModel, Field


class Action(BaseModel):
    type: Literal[
        "goto", "click", "click_and_wait", "type", "select_option", "press_key", "set_zoom", "annotate", "scroll", "scroll_to_text",
        "hover", "highlight", "wait", "caption", "mask", "unmask",
    ]
    url: str | None = None
    ref: str | None = None
    text: str | None = None
    dy: int = 0
    ms: int = Field(default=0, ge=0)
    spotlight: bool = True
    style: Literal["marker", "callout", "underline"] = "callout"
    dim: bool = False
    target_text: str | None = None
    wait_for_url: str | None = None
    wait_for_text: str | None = None
    settle_ms: int = Field(default=500, ge=0, le=10000)
    narration_timing: Literal["before_action", "after_action", "after_settle"] = "after_settle"


# The action contract, stated once for everything an agent sees: the tool's
# input schema, the hint on an invalid action, and the director prompt.
#
# act used to declare its parameter as a bare ``dict``, so MCP clients saw
# ``{"type": "object"}`` and nothing else. An agent recording a demo guessed
# ``{"type": "fill"}``, got ``invalid_action`` with a raw pydantic message, and
# spent a round trip per guess (agent feedback, 2026-10-07).
ACTION_TYPES: tuple[str, ...] = get_args(Action.model_fields["type"].annotation)

ACTION_FIELDS: dict[str, str] = {
    "goto": "url",
    "click": "ref",
    "click_and_wait": "ref, plus wait_for_url or wait_for_text",
    "hover": "ref",
    "highlight": "ref",
    "type": "ref, text (the verb is type, not fill)",
    "press_key": "ref, text (a key such as Enter or Escape)",
    "select_option": "ref, text (the option label)",
    "scroll": "dy (pixels; negative scrolls up)",
    "scroll_to_text": "text",
    "wait": "ms",
    "caption": "text, ms",
    "set_zoom": "text (a level from 0.5 to 2.0, e.g. \"1.25\")",
    "annotate": "ref, text, style (marker | callout | underline)",
    "mask": "ref (blurs that element on screen until unmask or a page load)",
    "unmask": "ref",
}

ACTION_CONTRACT = (
    "action = {\"type\": <verb>, ...}. Verbs and their fields: "
    + "; ".join(f"{verb}: {ACTION_FIELDS[verb]}" for verb in ACTION_TYPES)
    + ". Optional on any step: target_text (pick the exact element inside ref), "
    "wait_for_url, wait_for_text, settle_ms, narration_timing."
)


def action_json_schema() -> dict[str, Any]:
    """The action object as inline JSON Schema, with no $ref.

    Inline on purpose: several model providers reject tool schemas that use
    $defs/$ref, and the point is that every client sees the verbs.
    """
    return {
        "type": "object",
        "description": ACTION_CONTRACT,
        "required": ["type"],
        "properties": {
            "type": {"type": "string", "enum": list(ACTION_TYPES)},
            "url": {"type": "string"},
            "ref": {"type": "string", "description": "A ref from the latest observe"},
            "text": {"type": "string"},
            "dy": {"type": "integer"},
            "ms": {"type": "integer", "minimum": 0},
            "spotlight": {"type": "boolean"},
            "style": {"type": "string", "enum": ["marker", "callout", "underline"]},
            "dim": {"type": "boolean"},
            "target_text": {"type": "string"},
            "wait_for_url": {"type": "string"},
            "wait_for_text": {"type": "string"},
            "settle_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
            "narration_timing": {"type": "string", "enum": ["before_action", "after_action", "after_settle"]},
        },
    }


FRAMINGS: tuple[str, ...] = ("wide", "medium", "close")


def mask_stylesheet(selectors: list[str] | None) -> str:
    """CSS that blurs every element matching any of the selectors.

    One rule per selector, so a selector the browser does not understand
    drops only its own rule. Braces are refused: a selector is not a place
    to write CSS, and one could close the rule and add arbitrary styles.
    """
    rules = []
    for raw in selectors or []:
        selector = str(raw).strip()
        if not selector:
            continue
        if "{" in selector or "}" in selector:
            raise ValueError(f"mask selector may not contain braces: {selector!r}")
        rules.append(
            f"{selector} {{ filter: blur(9px) !important; user-select: none !important; }}"
        )
    return "\n".join(rules)
