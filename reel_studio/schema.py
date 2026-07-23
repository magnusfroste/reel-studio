from typing import Literal

from pydantic import BaseModel, Field


class Action(BaseModel):
    type: Literal[
        "goto", "click", "type", "scroll", "scroll_to_text",
        "hover", "highlight", "wait",
    ]
    url: str | None = None
    ref: str | None = None
    text: str | None = None
    dy: int = 0
    ms: int = Field(default=0, ge=0)
    spotlight: bool = True
