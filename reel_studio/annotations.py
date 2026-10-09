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
  const accent = spec.accent || "#ffd166";
  if (spec.dim) {
    const dim = document.createElement("div");
    dim.dataset.annotationDim = "true";
    Object.assign(dim.style, {position:"fixed", inset:"0", background:"rgba(5,10,20,.34)"});
    group.appendChild(dim);
  }
  let label = null;
  const box = document.createElement("div");
  box.dataset.annotationBox = "true";
  Object.assign(box.style, {
    position:"fixed",
    border:`3px solid ${accent}`, borderRadius:"12px",
    boxShadow:`0 0 0 5px color-mix(in srgb, ${accent} 30%, transparent), 0 0 28px color-mix(in srgb, ${accent} 75%, transparent)`,
    background:"transparent", transition:"all 180ms ease"
  });
  group.appendChild(box);
  if (spec.kind !== "underline") {
    label = document.createElement("div");
    label.textContent = spec.label;
    Object.assign(label.style, {
      position:"fixed",
      maxWidth:"360px", padding:"8px 12px", borderRadius:"8px",
      background:"#101827", color:"#fff", border:`1px solid ${accent}`,
      fontSize:"16px", fontWeight:"700", lineHeight:"1.2",
      boxShadow:"0 8px 24px rgba(0,0,0,.3)"
    });
    group.appendChild(label);
  }
  root.appendChild(group);
  const target = spec.selector ? document.querySelector(spec.selector) : null;
  // documentElement CSS zoom scales fixed-position descendants a second time,
  // so viewport coordinates must be divided back before assigning left/top.
  const zoomFactor = () => {
    try {
      const value = parseFloat(getComputedStyle(document.documentElement).zoom);
      return Number.isFinite(value) && value > 0 ? value : 1;
    } catch { return 1; }
  };
  const update = () => {
    const rect = target?.getBoundingClientRect?.() || spec.box;
    if (!rect) return;
    const z = zoomFactor();
    const {x, y, width, height} = {
      x: rect.left / z, y: rect.top / z,
      width: rect.width / z, height: rect.height / z,
    };
    Object.assign(box.style, {
      left:`${x-10}px`, top:`${y-10}px`,
      width:`${width+20}px`, height:`${height+20}px`,
    });
    if (label) {
      // Measure after the label is in the DOM and choose the side with room.
      // Coordinates are in the unzoomed fixed-layer coordinate system.
      const labelWidth = label.offsetWidth || 240;
      const labelHeight = label.offsetHeight || 36;
      const viewportWidth = window.innerWidth / z;
      const viewportHeight = window.innerHeight / z;
      const gap = 12;
      const labelX = Math.min(
        Math.max(8, x + (width - labelWidth) / 2),
        Math.max(8, viewportWidth - labelWidth - 8),
      );
      const aboveY = y - labelHeight - gap;
      const belowY = y + height + gap;
      const labelY = aboveY >= 8 ? aboveY : (
        belowY + labelHeight <= viewportHeight - 8 ? belowY : Math.max(8, aboveY)
      );
      Object.assign(label.style, {
        left:`${labelX}px`, top:`${labelY}px`,
      });
    }
  };
  update();
  if (target && spec.follow_target !== false) {
    const observer = new ResizeObserver(update);
    observer.observe(target);
    const onScroll = () => update();
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onScroll);
    setTimeout(() => {
      observer.disconnect();
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    }, spec.duration_ms + 100);
  }
  setTimeout(() => {
    group.remove();
    target?.removeAttribute?.("data-video-director-annotation-target");
  }, spec.duration_ms);
}
"""


def caption_script() -> str:
    """Return a browser function for readable narration-linked captions."""
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
  // A caption replaces any sticky one still on screen.
  root.querySelectorAll("[data-reel-sticky]").forEach((el) => el.remove());
  const group = document.createElement("div");
  group.dataset.annotationId = spec.id;
  if (spec.sticky) group.dataset.reelSticky = "1";
  const caption = document.createElement("div");
  caption.setAttribute("aria-label", spec.label);
  caption.textContent = spec.label;
  Object.assign(caption.style, {
    position: "fixed", left: "50%", bottom: "34px",
    transform: "translateX(-50%)", maxWidth: "min(900px, calc(100vw - 64px))",
    padding: "12px 22px", borderRadius: "12px",
    background: "rgba(16,24,39,.96)", color: "#fff",
    border: "2px solid #ffd166", fontSize: "22px", fontWeight: "800",
    lineHeight: "1.25", textAlign: "center",
    boxShadow: "0 8px 28px rgba(0,0,0,.42)",
    letterSpacing: ".01em"
  });
  if (spec.view) {
    // The camera is pushed in: draw the caption inside the framed region,
    // scaled by 1/zoom, so it comes out the same size and in the same place
    // in the video as an unzoomed caption.
    const v = spec.view;
    Object.assign(caption.style, {
      left: `${v.x + v.w / 2}px`,
      bottom: `${window.innerHeight - (v.y + v.h) + 34 / v.zoom}px`,
      transform: `translateX(-50%) scale(${1 / v.zoom})`,
      transformOrigin: "50% 100%",
      maxWidth: `${Math.min(900, v.w * v.zoom - 64)}px`
    });
  }
  group.appendChild(caption);
  root.appendChild(group);
  if (!spec.sticky) setTimeout(() => group.remove(), spec.duration_ms);
}
"""


def annotation_hold_seconds(narration_duration: float, annotation_duration: float) -> float:
    """Return the visual hold needed to keep an annotation visible."""
    return max(0.0, narration_duration, annotation_duration)


def annotation_id(ref: str, counter: int) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", ref.lower()).strip("-") or "target"
    return f"annotation-{safe}-{counter}"
