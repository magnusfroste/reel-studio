"""Browser-side visual annotations for director-led product videos."""

from __future__ import annotations

import re
from typing import Literal


AnnotationKind = Literal["marker", "callout", "underline"]


def validate_annotation(kind: str, label: str, duration_ms: int) -> dict[str, object]:
    """Validate and normalize the small annotation action contract."""
    kind = kind.strip().lower()
    label = label.strip()
    if kind not in {"marker", "callout", "underline"}:
        raise ValueError("annotation kind must be marker, callout, or underline")
    if not label:
        raise ValueError("annotation label cannot be empty")
    if duration_ms < 250 or duration_ms > 30000:
        raise ValueError("annotation duration must be between 250 and 30000 ms")
    return {"kind": kind, "label": label, "duration_ms": duration_ms}


def annotation_script() -> str:
    """Return an idempotent browser function used by Playwright."""
    return r"""
(spec) => {
  const rootId = "video-director-annotations";
  let root = document.getElementById(rootId);
  if (!root) {
    root = document.createElement("div");
    root.id = rootId;
    Object.assign(root.style, {
      position: "fixed", inset: "0", pointerEvents: "none",
      zIndex: "2147483647", fontFamily: "Inter, system-ui, sans-serif"
    });
    document.documentElement.appendChild(root);
  }
  const old = root.querySelector(`[data-annotation-id="${spec.id}"]`);
  if (old) old.remove();
  const group = document.createElement("div");
  group.dataset.annotationId = spec.id;
  const {x, y, width, height} = spec.box;
  const accent = spec.accent || "#ffd166";
  if (spec.dim) {
    const dim = document.createElement("div");
    dim.dataset.annotationDim = "true";
    Object.assign(dim.style, {position:"fixed", inset:"0", background:"rgba(5,10,20,.34)"});
    group.appendChild(dim);
  }
  const box = document.createElement("div");
  box.dataset.annotationBox = "true";
  Object.assign(box.style, {
    position:"fixed", left:`${x-10}px`, top:`${y-10}px`,
    width:`${width+20}px`, height:`${height+20}px`,
    border:`3px solid ${accent}`, borderRadius:"12px",
    boxShadow:`0 0 0 5px color-mix(in srgb, ${accent} 30%, transparent), 0 0 28px color-mix(in srgb, ${accent} 75%, transparent)`,
    background:"transparent", transition:"all 180ms ease"
  });
  group.appendChild(box);
  if (spec.kind !== "underline") {
    const label = document.createElement("div");
    label.textContent = spec.label;
    Object.assign(label.style, {
      position:"fixed", left:`${Math.max(8, x)}px`, top:`${Math.max(8, y-48)}px`,
      maxWidth:"360px", padding:"8px 12px", borderRadius:"8px",
      background:"#101827", color:"#fff", border:`1px solid ${accent}`,
      fontSize:"16px", fontWeight:"700", lineHeight:"1.2",
      boxShadow:"0 8px 24px rgba(0,0,0,.3)"
    });
    group.appendChild(label);
  }
  root.appendChild(group);
}
"""


def annotation_id(ref: str, counter: int) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", ref.lower()).strip("-") or "target"
    return f"annotation-{safe}-{counter}"
