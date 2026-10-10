"""Browser and recording lifecycle for one directed video session."""

from dataclasses import dataclass, field
import asyncio
import json
import tempfile
import shutil
import os
from pathlib import Path
import subprocess
import time
import uuid
from typing import cast

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .render import (
    CARD_DURATION,
    QUIET_FLOOR,
    SEGMENT_FLOOR,
    mux_narration,
    plan_segments,
    probe_duration,
    segmented_render,
    segmented_render_enabled,
    RenderConfig,
    start_recording,
    stop_recording,
)
from .camera import EASE_SECONDS, FRAMING_ZOOM, Camera, capture_scale
from .annotations import (
    annotation_hold_seconds,
    annotation_id,
    annotation_script,
    caption_script,
    validate_annotation,
)
from .refs import semantic_ref
from .schema import Action, mask_stylesheet
from .tts import synthesize


DEFAULT_OUTPUT_DIR = Path("/home/ubuntu/.video-director/sessions")




# How an element is found again later: a unique id, test id, label, name or
# placeholder, then link target or button text, then its path. Shared, verbatim,
# by _stable_selector and the one-pass collector below.
_STABLE_SELECTOR_JS = """(el) => {
                const escape = (value) => {
                    if (globalThis.CSS && CSS.escape) return CSS.escape(value);
                    return value.replace(/[^a-zA-Z0-9_-]/g, (char) => `\\${char}`);
                };
                const tag = el.tagName.toLowerCase();
                const id = el.getAttribute('id');
                if (id && document.querySelectorAll(`#${escape(id)}`).length === 1) {
                    return `#${escape(id)}`;
                }
                for (const attribute of ['data-testid', 'aria-label', 'name', 'placeholder']) {
                    const value = el.getAttribute(attribute);
                    if (!value) continue;
                    const selector = `${tag}[${attribute}=${JSON.stringify(value)}]`;
                    if (document.querySelectorAll(selector).length === 1) return selector;
                }
                const href = el.getAttribute('href');
                if (tag === 'a' && href) {
                    const selector = `a[href=${JSON.stringify(href)}]`;
                    if (document.querySelectorAll(selector).length === 1) return selector;
                }
                const text = (el.innerText || '').trim().split(/\\n+/)[0].replace(/\\s+/g, ' ');
                if (text && ['a', 'button', 'label', '[role=button]'].some((role) =>
                    role === tag || role === '[role=button]' && el.getAttribute('role') === 'button'
                )) {
                    const shortText = text.slice(0, 120);
                    const target = el.getAttribute('role') === 'button' ? '[role="button"]' : tag;
                    // :has-text matches every element of that kind whose text
                    // contains this, case-insensitively, and the action takes
                    // the first. A login page with a "Sign In" tab above a
                    // "Sign In" submit button sent every click to the tab
                    // (2026-10-08). Use the text only when it is unique; else
                    // fall through to the position path, which always is.
                    const needle = shortText.toLowerCase();
                    const rivals = Array.from(document.querySelectorAll(target)).filter((other) =>
                        (other.innerText || '').toLowerCase().includes(needle)
                    );
                    if (rivals.length === 1) {
                        return `${target}:has-text(${JSON.stringify(shortText)})`;
                    }
                }
                const parts = [];
                while (el && el.nodeType === 1 && el !== document.body) {
                    let index = 1;
                    let sibling = el.previousElementSibling;
                    while (sibling) {
                        if (sibling.tagName === el.tagName) index += 1;
                        sibling = sibling.previousElementSibling;
                    }
                    parts.unshift(`${el.tagName.toLowerCase()}:nth-of-type(${index})`);
                    el = el.parentElement;
                }
                return parts.join(" > ");
            }"""

# One pass over every matched element, inside the page, replacing a loop of
# separate round trips per element. Visibility mirrors Playwright's is_visible:
# a non-empty box and not visibility:hidden.
_COLLECT_ELEMENTS_JS = (
    "(elements) => {\n"
    "    const stableSelector = " + _STABLE_SELECTOR_JS + ";\n"
    """    const out = [];
    elements.forEach((el, index) => {
        const rect = el.getBoundingClientRect();
        if (!(rect.width > 0 && rect.height > 0)) return;
        if (getComputedStyle(el).visibility === 'hidden') return;
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || tag;
        let text = ['input', 'textarea', 'select'].includes(role) ? '' : (el.innerText || '').trim();
        if (!text) text = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
        const type = (el.getAttribute('type') || '').toLowerCase();
        const submits = (tag === 'button' && (type === 'submit' || (!type && el.form)))
            || (tag === 'input' && type === 'submit');
        out.push({
            index, role, text, submits,
            box: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
            selector: stableSelector(el),
        });
    });
    return out;
}"""
)


# Chrome's own popups are browser UI, not page content: no mask reaches them,
# and they sit on top of the recording. A real profile (needed for fullscreen,
# see screen_geometry) offers to save every password typed into it — after an
# agent signed in, "Save password?" stayed in the corner of the whole video,
# showing the account's email (2026-10-08). The profile is written with the
# password manager, autofill, translation and notification prompts off.
QUIET_PREFERENCES = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {
        "password_manager_enabled": False,
        "default_content_setting_values": {"notifications": 2, "geolocation": 2},
    },
    "autofill": {"profile_enabled": False, "credit_card_enabled": False},
    "translate": {"enabled": False},
    "browser": {"check_default_browser": False},
}

QUIET_FLAGS = [
    "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection,Translate,AutofillServerCommunication",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]


def write_quiet_profile(profile_dir: Path) -> None:
    default = profile_dir / "Default"
    default.mkdir(parents=True, exist_ok=True)
    (default / "Preferences").write_text(json.dumps(QUIET_PREFERENCES))


FULLSCREEN_NOTICE_SECONDS = 6.0


def screen_geometry(width: int, height: int, scale: float = 1.0) -> dict:
    """The X screen and browser window that make a recording show only the page.

    The browser was launched with --kiosk, then given a new context — and a new
    context opens an ordinary window. With no window manager under Xvfb, kiosk
    never took effect: every recording showed Chrome's tab and address bars,
    and the window grew past the screen, so the bottom ~85 pixels of each page —
    captions included — were never in the video (found 2026-10-08, when an
    agent's captions came out as a 2-pixel sliver).

    A persistent context started with --start-fullscreen does fill the screen,
    but one pixel short each way. So the X screen is one pixel larger than the
    recording, and the recorder grabs exactly width x height from the top-left:
    measured at 1920x1080, 1280x720 and 1080x1350, the page covers the recorded
    frame edge to edge, with no browser chrome.

    With a device scale factor (hi-res capture for the camera), the X screen
    and the recording are in physical pixels while --window-size is in CSS
    pixels: width x height gives a viewport of exactly width x height CSS
    pixels filling the physical frame (measured at 1920x1080 and 4/3; one
    pixel more made the page two CSS pixels too large and clipped its edges).
    """
    physical_w, physical_h = round(width * scale), round(height * scale)
    if scale == 1.0:
        window = [f"--window-size={width + 1},{height + 1}"]
    else:
        window = [
            f"--window-size={width},{height}",
            f"--force-device-scale-factor={scale:.10g}",
        ]
    return {
        "screen": f"{physical_w + 1}x{physical_h + 1}x24",
        "record_size": (physical_w, physical_h),
        "browser_args": [
            *window,
            "--window-position=0,0",
            "--start-fullscreen",
            *QUIET_FLAGS,
        ],
    }

def output_root() -> Path:
    return Path(os.environ.get("REEL_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR))).expanduser()


def _free_display() -> int:
    for number in range(99, 200):
        if not Path(f"/tmp/.X11-unix/X{number}").exists():
            return number
    raise RuntimeError("No free X display found")


@dataclass
class BrowserSession:
    session_id: str
    start_url: str
    width: int
    height: int
    voice: str
    provider: str
    output_size: tuple[int, int] | None
    render_config: RenderConfig
    directory: Path
    display_number: int
    xvfb: subprocess.Popen[bytes]
    playwright: object
    browser: Browser
    context: BrowserContext
    page: Page
    recorder: subprocess.Popen[bytes]
    t0: float
    refs: dict[str, str] = field(default_factory=dict)
    narrations: list[tuple[float, Path, str]] = field(default_factory=list)
    timeline: list[tuple[float, Path | None, float]] = field(default_factory=list)
    # The URL each timeline step ended on: a page change is a hard cut.
    timeline_pages: list[str] = field(default_factory=list)
    # Each timeline step's minimum length in the video: QUIET_FLOOR for a step
    # silent on purpose, SEGMENT_FLOOR otherwise.
    timeline_floors: list[float] = field(default_factory=list)
    # A step done offscreen before anything was shown: the video then opens on
    # the first visible step rather than on the page it was set up from.
    offscreen_first: bool = False
    refs_stale: bool = True
    runtime_closed: bool = False
    # Monotonic time of the last tool call on this session; see touch().
    last_activity: float = 0.0
    # The browser profile made for this session; removed when it closes.
    profile_dir: Path | None = None
    annotation_counter: int = 0
    camera: Camera | None = None
    # A shot declared by begin_shot; the camera moves when its first step
    # starts, since the time between tool calls is cut from the video.
    pending_shot: tuple[float, float | None, float | None] | None = None
    # A sticky caption on screen: (annotation id, label), redrawn when the
    # camera moves so it stays inside the frame.
    sticky_caption: tuple[str, str] | None = None

    @classmethod
    async def create(
        cls,
        start_url: str,
        width: int,
        height: int,
        voice: str,
        provider: str = "edge",
        output_size: tuple[int, int] | None = None,
        render_config: RenderConfig | None = None,
        mask_selectors: list[str] | None = None,
    ) -> "BrowserSession":
        session_id = uuid.uuid4().hex
        mask_css = mask_stylesheet(mask_selectors)
        directory = output_root() / session_id
        directory.mkdir(parents=True, exist_ok=True)
        display_number = _free_display()
        display = f":{display_number}"
        scale = capture_scale(width, height)
        geometry = screen_geometry(width, height, scale)
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", geometry["screen"], "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{display_number}").exists():
                break
            await asyncio.sleep(0.1)
        else:
            xvfb.terminate()
            raise RuntimeError("Xvfb did not start")

        playwright = await async_playwright().start()
        # A fresh profile per session: no cookie or login carries from one
        # recording to the next. Removed again in _close_runtime.
        profile_dir = Path(tempfile.mkdtemp(prefix="reel-profile-"))
        write_quiet_profile(profile_dir)
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            env={**os.environ, "DISPLAY": display},
            args=geometry["browser_args"],
            no_viewport=True,
        )
        # A persistent context has no separate Browser; closing the context
        # closes the browser, which is what _close_runtime needs.
        browser = context
        # Chrome's own "Please fill out this field" bubble showed over a
        # login form while an agent's hook caption played (2026-10-09). It is
        # browser chrome, not page content; validation itself still runs.
        await context.add_init_script(
            "document.addEventListener('invalid', (e) => e.preventDefault(), true);"
        )
        if mask_css:
            # Applied by the browser before any page script runs, on every page
            # and frame, so a masked element is blurred from its first frame —
            # a secret never flashes past before an agent gets to mask it.
            await context.add_init_script(
                "(() => { const css = " + json.dumps(mask_css) + ";"
                " const add = () => { const s = document.createElement('style');"
                " s.dataset.reelMask = 'session'; s.textContent = css;"
                " (document.head || document.documentElement).appendChild(s); };"
                " if (document.documentElement) add();"
                " else document.addEventListener('DOMContentLoaded', add); })()"
            )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(start_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(500)
        # Entering fullscreen, Chrome shows "To exit full screen, press and hold
        # Esc" over the top of the page for about five seconds (measured: gone
        # by 5.6 s, and it does not return on navigation or with the pointer at
        # the top edge). It is timed from the first page shown, not from launch:
        # counted from launch, a slow first page left it in the first seconds
        # of an agent's video. Recording starts after it.
        await asyncio.sleep(FULLSCREEN_NOTICE_SECONDS)
        recorder = start_recording(
            display, *geometry["record_size"], directory / "screen.mp4"
        )
        # Give ffmpeg one frame before t0 is recorded.
        await asyncio.sleep(0.4)
        if recorder.poll() is not None:
            exit_code = recorder.returncode
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await playwright.stop()
            except Exception:
                pass
            try:
                xvfb.terminate()
                xvfb.wait(timeout=5)
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise RuntimeError(
                f"Screen recorder exited during startup (exit code {exit_code})"
            )
        session = cls(
            session_id, start_url, width, height, voice, provider, output_size,
            render_config or RenderConfig(), directory, display_number, xvfb,
            playwright, browser, context, page, recorder, time.monotonic(),
        )
        session.profile_dir = profile_dir
        session.camera = Camera(width, height, scale)
        return session

    async def capture_screenshot(self) -> Path:
        screenshot = self.directory / f"screenshot-{int(time.time() * 1000)}.jpg"
        scale = self.camera.scale if self.camera is not None else 1.0
        if scale == 1.0:
            await self.page.screenshot(path=str(screenshot), type="jpeg", quality=80)
            return screenshot
        # Taken at the capture density and scaled to CSS size afterwards, so
        # boxes an agent reads off it are in CSS pixels. Playwright's
        # scale="css" switches the live page to scale 1 while it captures,
        # and the recording caught it: after every step the page shrank to
        # 3/4 in the top-left corner with white around it for a few frames
        # (the "flicker between pans", 2026-10-09).
        raw = screenshot.with_suffix(".raw.jpg")
        await self.page.screenshot(path=str(raw), type="jpeg", quality=90)
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw),
             "-vf", f"scale={self.width}:{self.height}", "-q:v", "4",
             str(screenshot)],
            check=True,
        )
        raw.unlink(missing_ok=True)
        return screenshot

    async def _stable_selector(self, item: Locator) -> str:
        return await item.evaluate(_STABLE_SELECTOR_JS)

    def touch(self) -> None:
        """Note that an agent is still directing this session."""
        self.last_activity = time.monotonic()

    async def observe(self, detail: str = "full") -> tuple[dict, Path | None]:
        """Capture the page and its interactive elements.

        Elements are collected in one pass inside the page. The previous loop
        asked the browser seven or so questions per element — visible? box?
        role? text? selector? — so a page with a few hundred controls took
        tens of seconds and, on a busy host, timed out (2026-10-07). The rules
        are unchanged: same selector, same visibility test as Playwright's
        (a non-empty box and not visibility:hidden), same text and refs.

        ``detail="refs"`` skips the screenshot and page text and returns only
        ref, role and text per element: what an agent needs to act, at a
        fraction of the size.
        """
        screenshot = await self.capture_screenshot() if detail != "refs" else None
        self.refs.clear()
        collected = await self.page.locator(
            "a,button,input,textarea,select,[role=button],[onclick]"
        ).evaluate_all(_COLLECT_ELEMENTS_JS)
        elements = []
        used_refs: set[str] = set()
        # Two controls with one name — the "Sign In" tab and the "Sign In"
        # button of a login form — got refs a suffix apart and nothing else
        # to tell them by; an agent clicked the tab, nothing submitted, and the
        # sign-in had to be recorded twice (2026-10-08). Say how many share a
        # name, and which of them submits a form.
        name_counts: dict[tuple[str, str], int] = {}
        for item in collected:
            key = (item["role"], item["text"].strip().lower())
            name_counts[key] = name_counts.get(key, 0) + 1
        for item in collected:
            role, text = item["role"], item["text"]
            ref = semantic_ref(role, text, item["index"], used_refs)
            self.refs[ref] = item["selector"]
            element = {"ref": ref, "role": role, "text": text}
            if detail != "refs":
                element["box"] = item["box"]
            shared = name_counts[(role, text.strip().lower())]
            if shared > 1:
                element["same_name"] = shared
            if item.get("submits"):
                element["submits_form"] = True
            elements.append(element)
        self.refs_stale = False
        payload: dict = {
            "url": self.page.url,
            "title": await self.page.title(),
            "elements": elements,
            "refs_stale": False,
            "detail": detail,
        }
        if detail != "refs":
            payload["screenshot_path"] = str(screenshot)
            payload["page_text"] = (await self.page.locator("body").inner_text())[:4000]
        return payload, screenshot

    async def _inject_spotlight(self, target: Locator) -> None:
        await target.evaluate(
            """(el) => {
                const rect = el.getBoundingClientRect();
                const accent = 'rgba(255, 193, 7, 0.95)';
                const node = document.createElement('div');
                node.dataset.videoDirectorSpotlight = 'true';
                Object.assign(node.style, {
                    position: 'fixed',
                    left: `${rect.left - 14}px`,
                    top: `${rect.top - 14}px`,
                    width: `${rect.width + 28}px`,
                    height: `${rect.height + 28}px`,
                    border: `3px solid ${accent}`,
                    borderRadius: '18px',
                    boxShadow: '0 0 0 6px rgba(255, 193, 7, 0.3), 0 0 30px 12px rgba(255, 193, 7, 0.75)',
                    pointerEvents: 'none',
                    zIndex: '2147483647',
                    transition: 'opacity 420ms ease, transform 420ms ease',
                    transform: 'scale(0.94)',
                    opacity: '1',
                });
                const halo = document.createElement('div');
                halo.dataset.videoDirectorCursorHalo = 'true';
                Object.assign(halo.style, {
                    position: 'fixed',
                    left: `${rect.left + rect.width / 2 - 18}px`,
                    top: `${rect.top + rect.height / 2 - 18}px`,
                    width: '36px', height: '36px', borderRadius: '50%',
                    border: `2px solid ${accent}`,
                    boxShadow: '0 0 0 4px rgba(255, 193, 7, 0.28), 0 0 22px rgba(255, 193, 7, 0.8)',
                    pointerEvents: 'none', zIndex: '2147483647',
                    animation: 'cursor-halo 900ms ease-out',
                });
                const pulse = document.createElement('div');
                pulse.dataset.videoDirectorClickPulse = 'true';
                Object.assign(pulse.style, {
                    position: 'fixed',
                    left: `${rect.left + rect.width / 2 - 7}px`,
                    top: `${rect.top + rect.height / 2 - 7}px`,
                    width: '14px', height: '14px', borderRadius: '50%',
                    background: accent, pointerEvents: 'none', zIndex: '2147483647',
                    animation: 'click-pulse 650ms ease-out',
                });
                const style = document.createElement('style');
                style.dataset.videoDirectorSpotlightStyle = 'true';
                style.textContent = `
                    @keyframes cursor-halo { from { transform: scale(.72); opacity: .95; } to { transform: scale(1.45); opacity: 0; } }
                    @keyframes click-pulse { from { transform: scale(.7); opacity: .95; } to { transform: scale(3.2); opacity: 0; } }
                `;
                document.head.appendChild(style);
                document.body.append(node, halo, pulse);
                requestAnimationFrame(() => {
                    node.style.transform = 'scale(1.04)';
                    node.style.opacity = '0';
                });
                setTimeout(() => {
                    node.remove(); halo.remove(); pulse.remove(); style.remove();
                }, 900);
            }"""
        )

    async def _clear_spotlights(self) -> None:
        await self.page.locator(
            "[data-video-director-spotlight]"
        ).evaluate_all("(nodes) => nodes.forEach((node) => node.remove())")

    async def _visible_text_target(self, text: str, exact: bool = False) -> Locator | None:
        text = text.strip()
        if not text:
            return None
        matches = self.page.get_by_text(text, exact=exact)
        for index in range(await matches.count()):
            candidate = matches.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except PlaywrightError:
                continue
        return None

    async def _action_target(
        self, action: Action, actionable: bool = False
    ) -> Locator | None:
        """Resolve an action ref, optionally narrowing it to exact visible text."""
        assert action.ref is not None
        target = self.page.locator(self.refs[action.ref]).first
        if not action.target_text:
            return target
        base_handle = await target.element_handle()
        exact = self.page.get_by_text(action.target_text.strip(), exact=True)
        for index in range(await exact.count()):
            candidate = exact.nth(index)
            try:
                candidate_handle = await candidate.element_handle()
                in_target = (
                    candidate_handle
                    and base_handle
                    and await target.evaluate(
                        "(el, candidate) => el === candidate || el.contains(candidate)",
                        candidate_handle,
                    )
                )
                if not in_target or not await candidate.is_visible():
                    continue
                if actionable:
                    control = candidate.locator(
                        "xpath=ancestor-or-self::*[self::a or self::button "
                        "or self::input or self::textarea or self::select "
                        "or @role='button'][1]"
                    ).first
                    if await control.count():
                        return control
                return candidate
            except PlaywrightError:
                continue
        return None

    async def _box_in_viewport(self, box: dict | None) -> bool:
        if not box:
            return False
        viewport = self.page.viewport_size or {
            "width": self.width,
            "height": self.height,
        }
        return (
            box["x"] < viewport["width"]
            and box["y"] < viewport["height"]
            and box["x"] + box["width"] > 0
            and box["y"] + box["height"] > 0
        )

    async def set_shot(
        self,
        framing: str,
        zoom: float | None = None,
        focus_ref: str | None = None,
        focus_text: str | None = None,
    ) -> dict:
        """Aim the camera for the next step: framing, or an explicit zoom, on a focus.

        The move starts with the next recorded step, so the push-in lands
        while that step's narration names the thing it frames.
        """
        level = zoom if zoom is not None else FRAMING_ZOOM.get(framing, 1.0)
        level = max(1.0, level)
        box = None
        box_is_text = False
        note = ""
        if level > 1.0:
            if focus_ref and focus_ref in self.refs and not self.refs_stale:
                target = self.page.locator(self.refs[focus_ref]).first
                try:
                    box = await target.bounding_box()
                except PlaywrightError:
                    box = None
            elif focus_text:
                target = await self._visible_text_target(focus_text, exact=False)
                if target is not None:
                    box = await self._text_box(target, focus_text)
                    box_is_text = True
            if box is None:
                note = (
                    "No focus_ref or focus_text box on screen: the camera pushes in "
                    "on the current centre and follows the next clicked element."
                )
        cx = cy = None
        if box:
            if self.camera is not None:
                # Words are read from their start: frame a text focus from
                # its first word, not around its middle.
                cx, cy = self.camera.aim(box, level, lead=box_is_text)
            else:
                cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.pending_shot = (level, cx, cy)
        result: dict = {"camera_zoom": level}
        if self.camera is not None:
            # What the frame will show, in CSS pixels: the centre is clamped so
            # the frame stays on the page, and an agent should not have to crop
            # screenshots to find out what it got (agent feedback, 2026-10-09).
            current = self.camera.final_state()
            state = (level, cx if cx is not None else current[1],
                     cy if cy is not None else current[2])
            frame = self.camera.view(state if level > 1.0 else (1.0, 0.0, 0.0))
            result["camera_frame"] = {k: round(frame[k]) for k in ("x", "y", "w", "h")}
        if box:
            result["camera_centre"] = {"x": round(cx or 0), "y": round(cy or 0)}
        if note:
            result["camera_note"] = note
        return result

    async def _text_box(self, target: Locator, text: str) -> dict | None:
        """The box of the words themselves, not of the element holding them.

        get_by_text resolves to an element, and a block element is as wide as
        its container even when its text is a short line at the left.
        """
        try:
            box = await target.evaluate(
                """(el, needle) => {
                    const want = needle.toLowerCase().replace(/\\s+/g, ' ').trim();
                    const rectOf = (range) => {
                        const b = range.getBoundingClientRect();
                        return b.width && b.height
                            ? {x: b.x, y: b.y, width: b.width, height: b.height} : null;
                    };
                    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                    let node;
                    while ((node = walker.nextNode())) {
                        const at = node.data.toLowerCase().indexOf(want);
                        if (at < 0) continue;
                        const range = document.createRange();
                        range.setStart(node, at);
                        range.setEnd(node, Math.min(node.data.length, at + want.length));
                        const rect = rectOf(range);
                        if (rect) return rect;
                    }
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    return rectOf(range);
                }""",
                text.strip(),
            )
        except PlaywrightError:
            box = None
        if box:
            return box
        try:
            return await target.bounding_box()
        except PlaywrightError:
            return None

    def _camera_now(self) -> float:
        return time.monotonic() - self.t0

    async def _follow(self, target: Locator) -> None:
        """Pan to a target that is outside the zoomed view, before acting on it."""
        if self.camera is None:
            return
        try:
            # A link to another page: the click ends this shot, and a pan to
            # it — to a sidebar entry, usually — only showed as a half-second
            # drift before the cut (2026-10-09).
            if await target.evaluate(
                "(el) => { const a = el.closest('a[href]'); if (!a) return false;"
                " const to = new URL(a.href, location.href);"
                " return to.origin + to.pathname + to.search"
                "   !== location.origin + location.pathname + location.search; }"
            ):
                return
            box = await target.bounding_box()
        except PlaywrightError:
            return
        centre = self.camera.needs_follow(box)
        if centre is None:
            return
        zoom = self.camera.final_state()[0]
        if self.camera.move(self._camera_now(), zoom, *centre):
            await self._redraw_sticky_caption()
            # Let the pan land before the click, so the viewer sees where it
            # goes before it happens.
            await asyncio.sleep(EASE_SECONDS)

    async def _redraw_sticky_caption(self) -> None:
        if self.sticky_caption is None:
            return
        caption_id, label = self.sticky_caption
        try:
            await self.page.evaluate(
                caption_script(),
                {"id": caption_id, "label": label, "duration_ms": 0,
                 "sticky": True, "view": self._caption_view(),
                 "delay_ms": int(EASE_SECONDS * 1000)},
            )
        except PlaywrightError:
            self.sticky_caption = None

    def _shot_moves_camera(self) -> bool:
        if self.camera is None or self.pending_shot is None:
            return False
        level, cx, cy = self.pending_shot
        current = self.camera.final_state()
        target = (max(level, 1.0),
                  current[1] if cx is None else cx,
                  current[2] if cy is None else cy)
        if target[0] == 1.0 and current[0] == 1.0:
            return False
        return any(abs(a - b) > 0.5 for a, b in
                   zip(self.camera.view(target).values(), self.camera.view(current).values()))

    def _caption_view(self) -> dict | None:
        """The frame a caption drawn now will be seen in.

        A declared shot moves the camera when its step's footage starts, so a
        caption on that step belongs to the shot's frame, not the current one.
        """
        if self.camera is None:
            return None
        if self.pending_shot is not None:
            level, cx, cy = self.pending_shot
            _, current_cx, current_cy = self.camera.final_state()
            if level <= 1.0:
                return None
            view = self.camera.view((
                level,
                current_cx if cx is None else cx,
                current_cy if cy is None else cy,
            ))
        else:
            view = self.camera.view()
        return view if view["zoom"] > 1.0 else None

    async def assert_visible(self, text: str) -> dict:
        target = await self._visible_text_target(text)
        if target is None:
            return {"visible": False, "box": None, "in_viewport": False}
        box = await target.bounding_box()
        return {
            "visible": True,
            "box": box,
            "in_viewport": await self._box_in_viewport(box),
        }

    async def error_result(self, error_type: str, message: str) -> tuple[dict, Path | None]:
        screenshot: Path | None = None
        try:
            screenshot = await self.capture_screenshot()
        except PlaywrightError:
            pass
        return {
            "ok": False,
            "error": {"type": error_type, "message": message},
            "url": self.page.url,
            "title": await self.page.title(),
            "refs_stale": self.refs_stale,
            **({"screenshot_path": str(screenshot)} if screenshot else {}),
        }, screenshot

    async def act(self, action: Action, narration: str = "") -> tuple[dict, Path | None]:
        offset = time.monotonic() - self.t0
        clip: Path | None = None
        duration = 0.0
        action_box: dict | None = None
        action_in_viewport: bool | None = None
        live_annotation_id: str | None = None
        annotation_duration = 0.0
        settled_by: dict[str, str] = {}
        action_finished_at: float | None = None
        action_completed_at: float | None = None
        before_url = self.page.url
        if action.ref and self.refs_stale:
            return await self.error_result(
                "stale_refs",
                "Element refs are stale; call observe again before using a ref.",
            )
        if action.offscreen and narration.strip():
            return await self.error_result(
                "invalid_action",
                "An offscreen step is cut from the video, so it cannot be narrated. "
                "Narrate the first visible step instead.",
            )
        if narration:
            try:
                clip = await synthesize(
                    narration, self.voice, self.directory, self.provider
                )
                duration = probe_duration(clip)
            except Exception as exc:
                return await self.error_result("narration_failed", str(exc))
        try:
            action_type = action.type
            if action_type == "caption":
                label = (action.text or "").strip()
                duration_ms = action.ms or max(250, int(round(duration * 1000)))
                if not label:
                    return await self.error_result("invalid_action", "caption requires text")
                if duration_ms > 30000:
                    return await self.error_result("invalid_action", "caption duration must be 30000 ms or less")
                self.annotation_counter += 1
                live_annotation_id = annotation_id("caption", self.annotation_counter)
                # A shot declared for this step moves the camera when its
                # footage starts, after the settle. Drawn for the new frame
                # straight away, the caption showed at half size and grew with
                # the zoom; it appears once the move has landed instead.
                delay_ms = (
                    action.settle_ms + int(EASE_SECONDS * 1000)
                    if self._shot_moves_camera() else 0
                )
                annotation_duration = (duration_ms + delay_ms) / 1000
                await self.page.evaluate(
                    caption_script(),
                    {"id": live_annotation_id, "label": label, "duration_ms": duration_ms,
                     "sticky": action.sticky, "view": self._caption_view(),
                     "delay_ms": delay_ms},
                )
                # Any caption replaces a sticky one.
                self.sticky_caption = (live_annotation_id, label) if action.sticky else None
            elif action_type == "goto":
                if not action.url:
                    return await self.error_result("invalid_action", "goto requires url")
                await self.page.goto(action.url, wait_until="domcontentloaded", timeout=15000)
            elif action_type == "scroll_to_text":
                if not action.text or not action.text.strip():
                    return await self.error_result(
                        "invalid_action", "scroll_to_text requires text"
                    )
                target = await self._visible_text_target(action.text)
                if target is None:
                    return await self.error_result(
                        "text_not_found",
                        f"Visible text not found: {action.text}",
                    )
                await target.scroll_into_view_if_needed(timeout=5000)
                action_box = await target.bounding_box()
                action_in_viewport = await self._box_in_viewport(action_box)
            elif action_type == "set_zoom":
                try:
                    level = float((action.text or "").strip())
                except ValueError:
                    return await self.error_result(
                        "invalid_action", "set_zoom requires a numeric level"
                    )
                if not 0.5 <= level <= 2.0:
                    return await self.error_result(
                        "invalid_action", "set_zoom level must be between 0.5 and 2.0"
                    )
                await self.page.evaluate(
                    "(level) => { document.documentElement.style.zoom = String(level); }",
                    level,
                )
            elif action_type == "annotate":
                if not action.ref or action.ref not in self.refs:
                    return await self.error_result(
                        "unknown_ref", f"Unknown element ref: {action.ref}"
                    )
                normalized = validate_annotation(
                    action.style, action.text or "", action.ms or 2500
                )
                target = await self._action_target(action)
                if target is None:
                    return await self.error_result(
                        "target_text_not_found",
                        f"Exact visible target text not found: {action.target_text}",
                    )
                await target.wait_for(state="visible", timeout=5000)
                await target.scroll_into_view_if_needed(timeout=5000)
                await self._follow(target)
                box = await target.bounding_box()
                if not box:
                    return await self.error_result(
                        "focus_target_not_visible", f"Target has no viewport box: {action.ref}"
                    )
                self.annotation_counter += 1
                live_annotation_id = annotation_id(action.ref, self.annotation_counter)
                annotation_duration = cast(float, normalized["duration_ms"]) / 1000
                visual_duration_ms = max(
                    cast(int, normalized["duration_ms"]), int(round(duration * 1000))
                )
                await target.evaluate(
                    "(el, id) => el.setAttribute('data-video-director-annotation-target', id)",
                    live_annotation_id,
                )
                await self.page.evaluate(
                    annotation_script(),
                    {
                        "id": live_annotation_id,
                        "kind": normalized["kind"],
                        "label": normalized["label"],
                        "duration_ms": visual_duration_ms,
                        "dim": action.dim,
                        "box": box,
                        "selector": (
                            f"[data-video-director-annotation-target="
                            f"{live_annotation_id!r}]"
                        ),
                        "follow_target": True,
                    },
                )
            elif action_type in {"click", "click_and_wait", "type", "select_option", "press_key", "hover", "highlight", "mask", "unmask"}:
                if not action.ref or action.ref not in self.refs:
                    return await self.error_result(
                        "unknown_ref", f"Unknown element ref: {action.ref}"
                    )
                target = self.page.locator(self.refs[action.ref])
                if await target.count() == 0:
                    return await self.error_result(
                        "unknown_ref", f"Element ref no longer matches: {action.ref}"
                    )
                target = target.first
                if action.target_text:
                    target = await self._action_target(action, actionable=True)
                    if target is None:
                        return await self.error_result(
                            "target_text_not_found",
                            f"Exact visible target text not found: {action.target_text}",
                        )
                await target.wait_for(state="visible", timeout=5000)
                await target.scroll_into_view_if_needed(timeout=5000)
                if action_type not in {"mask", "unmask"}:
                    await self._follow(target)
                if action_type in {"click", "click_and_wait"}:
                    await self._inject_spotlight(target)
                    await target.click()
                    await self.page.wait_for_timeout(500)
                    await self._clear_spotlights()
                elif action_type == "type":
                    await target.fill(action.text or "")
                elif action_type == "select_option":
                    option_text = (action.text or "").strip()
                    if not option_text:
                        return await self.error_result(
                            "invalid_action", "select_option requires text"
                        )
                    tag_name = await target.evaluate("(el) => el.tagName.toLowerCase()")
                    if tag_name == "select":
                        await target.select_option(label=option_text)
                    else:
                        await target.click()
                        option = self.page.get_by_role(
                            "option", name=option_text, exact=False
                        ).first
                        await option.click(timeout=5000)
                elif action_type == "press_key":
                    key = (action.text or "").strip()
                    if not key:
                        return await self.error_result(
                            "invalid_action", "press_key requires text"
                        )
                    await target.press(key)
                elif action_type == "hover":
                    await target.hover()
                elif action_type == "mask":
                    await target.evaluate(
                        "(el) => { el.dataset.reelMaskedFilter = el.style.filter || '';"
                        " el.style.setProperty('filter', 'blur(9px)', 'important'); }"
                    )
                elif action_type == "unmask":
                    await target.evaluate(
                        "(el) => { el.style.filter = el.dataset.reelMaskedFilter || '';"
                        " delete el.dataset.reelMaskedFilter; }"
                    )
                else:
                    if action.spotlight:
                        await self._inject_spotlight(target)
                    await target.evaluate(
                        "(el) => { el.dataset.videoDirectorOldOutline = el.style.outline; "
                        "el.style.outline = '4px solid #ff3b30'; }"
                    )
                    await self.page.wait_for_timeout(1500)
                    await self._clear_spotlights()
                    await target.evaluate(
                        "(el) => { el.style.outline = el.dataset.videoDirectorOldOutline || ''; "
                        "delete el.dataset.videoDirectorOldOutline; }"
                    )
            elif action_type == "scroll":
                await self.page.mouse.wheel(0, action.dy)
            elif action_type == "wait":
                await self.page.wait_for_timeout(action.ms)
            action_finished_at = time.monotonic() - self.t0
            if self.page.url != before_url:
                self.refs_stale = True
                # A new page: whatever the camera framed is gone. Back to
                # wide, as a cut: the new page is a cut in the video too, and
                # an eased zoom-out showed its first second at the old zoom.
                if self.camera is not None:
                    self.camera.move(action_finished_at, 1.0, ease=0.0)
                # A sticky caption belonged to the old page. In a single-page
                # app the old page's DOM — and the caption — survives the
                # navigation, so it is removed rather than forgotten.
                self.sticky_caption = None
                try:
                    await self.page.evaluate(
                        "() => document.querySelectorAll('[data-reel-sticky]')"
                        ".forEach((el) => el.remove())"
                    )
                except PlaywrightError:
                    pass
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except PlaywrightTimeoutError:
                pass
            if action.wait_for_url:
                try:
                    await self.page.wait_for_url(
                        f"**{action.wait_for_url}**", timeout=action.wait_timeout_ms
                    )
                    settled_by["url_contains"] = action.wait_for_url
                except PlaywrightTimeoutError as exc:
                    return await self.error_result(
                        "page_not_settled",
                        f"URL did not contain {action.wait_for_url!r}: {exc}",
                    )
            if action.wait_for_text:
                deadline = time.monotonic() + action.wait_timeout_ms / 1000
                while time.monotonic() < deadline:
                    # Contained text, not the element's whole text: "The model
                    # answered" never matched "✓ The model answered in 17.4s.
                    # Chat will work.", so a test that passed read as a
                    # failure and cancelled the rest of the batch (2026-10-08).
                    if await self._visible_text_target(action.wait_for_text, exact=False):
                        settled_by["visible_text"] = action.wait_for_text
                        break
                    await self.page.wait_for_timeout(100)
                else:
                    return await self.error_result(
                        "page_not_settled",
                        f"Visible text did not appear within "
                        f"{action.wait_timeout_ms} ms: {action.wait_for_text}. "
                        "The action itself already ran — do not repeat it; wait "
                        "for the text with a wait step, and set wait_timeout_ms "
                        "(up to 60000) for slow answers.",
                    )
            await self.page.wait_for_timeout(action.settle_ms)
            action_completed_at = time.monotonic() - self.t0
        except PlaywrightTimeoutError as exc:
            if self.page.url != before_url:
                self.refs_stale = True
            return await self.error_result("timeout", str(exc))
        except (PlaywrightError, ValueError) as exc:
            try:
                await self._clear_spotlights()
            except PlaywrightError:
                pass
            if self.page.url != before_url:
                self.refs_stale = True
            return await self.error_result("action_failed", str(exc))
        if action.narration_timing == "after_action" and action_finished_at is not None:
            offset = action_finished_at
        elif action.narration_timing == "after_settle" and action_completed_at is not None:
            # TTS is prepared before the browser action; align narration with
            # the settled visual state rather than with synthesis start.
            offset = action_completed_at
        if self.pending_shot is not None and self.camera is not None:
            # The shot's move starts where this step's footage starts in the
            # video: at its narration, after the action has settled. Timed
            # from the start of the act call, it eased during the action and
            # the settle — footage the segmented render cuts — so most
            # push-ins reached the video as a jump to the zoomed frame, not a
            # move (found comparing cut boundaries, 2026-10-09).
            if self.camera.move(offset, *self.pending_shot):
                await self._redraw_sticky_caption()
            self.pending_shot = None
        hold_duration = annotation_hold_seconds(duration, annotation_duration)
        if action.offscreen:
            # Done, but not part of the video: it adds nothing to the timeline
            # the renderer cuts from, so the footage around it is cut away.
            if not self.timeline:
                self.offscreen_first = True
        else:
            if clip:
                self.narrations.append((offset, clip, narration))
            self.timeline.append((offset, clip, hold_duration))
            self.timeline_pages.append(self.page.url)
            self.timeline_floors.append(QUIET_FLOOR if action.quiet else SEGMENT_FLOOR)
        if hold_duration:
            elapsed = time.monotonic() - (self.t0 + offset)
            padding_applied = elapsed < hold_duration
            if elapsed < hold_duration:
                await asyncio.sleep(hold_duration - elapsed)
        else:
            padding_applied = False
        if live_annotation_id and not (
            self.sticky_caption and self.sticky_caption[0] == live_annotation_id
        ):
            await self.page.evaluate(
                "(id) => document.querySelector(`[data-annotation-id=\"${id}\"]`)?.remove()",
                live_annotation_id,
            )
        screenshot = await self.capture_screenshot()
        result = {
            "ok": True,
            "offset_seconds": round(offset, 3),
            "url": self.page.url,
            "title": await self.page.title(),
            "changed": self.page.url != before_url,
            "narration_duration": round(duration, 3),
            # The clip's file name in the session directory, so a rerender
            # can reuse it while the line is unchanged.
            **({"narration_clip": clip.name} if clip else {}),
            "visual_hold_duration": round(hold_duration, 3),
            "padding_applied": padding_applied,
            "refs_stale": self.refs_stale,
            "screenshot_path": str(screenshot),
        }
        if settled_by:
            result["settled_by"] = settled_by
        if action_completed_at is not None:
            result["settled_at_seconds"] = round(action_completed_at, 3)
        if action_type == "scroll_to_text":
            result.update({"box": action_box, "in_viewport": action_in_viewport})
        if live_annotation_id:
            result["annotation_id"] = live_annotation_id
            result["annotation_duration"] = round(annotation_duration, 3)
        return result, screenshot

    def status(self) -> dict:
        elapsed = time.monotonic() - self.t0
        narrated = sum(probe_duration(clip) for _, clip, _ in self.narrations)
        return {
            "elapsed_seconds": round(elapsed, 3),
            "recorded_steps": len(self.timeline),
            "total_narrated_seconds": round(narrated, 3),
            "estimated_video_length": round(self.estimated_length(elapsed), 3),
        }

    def estimated_length(self, elapsed: float) -> float:
        """How long the finished video will be if the session ends now.

        It used to be the wall-clock time since start, which counts the
        minutes an agent spends thinking between calls; those are cut from
        the video. An agent read 800 s on a take that rendered at 92 s and
        suspected its pauses were being recorded (2026-10-09). This is the
        renderer's own plan for the steps so far, plus the title and
        call-to-action cards.
        """
        if not segmented_render_enabled():
            return elapsed
        segments, _ = plan_segments(
            self.timeline, max(elapsed, 0.001), self.timeline_floors,
            not self.offscreen_first,
        )
        cards = CARD_DURATION * (
            bool(self.render_config.title.strip())
            + bool(self.render_config.cta_url.strip())
        )
        return sum(segment.output_duration for segment in segments) + cards

    def _finish_media(self) -> Path:
        stop_recording(self.recorder)
        video = self.directory / "screen.mp4"
        size = video.stat().st_size if video.is_file() else 0
        if size == 0:
            raise FileNotFoundError(
                f"Recording is missing or empty: {video} (size={size} bytes)"
            )
        try:
            probe_duration(video)
        except Exception as exc:
            raise RuntimeError(
                f"Recording is unreadable: {video} (size={size} bytes): {exc}"
            ) from exc
        final = self.directory / "video.mp4"
        if self.camera is not None:
            self.camera.save(self.directory)
        if segmented_render_enabled():
            segmented_render(
                video, self.timeline, final, self.output_size,
                self.render_config, self.camera, self.timeline_pages,
                self.timeline_floors, not self.offscreen_first,
            )
        else:
            mux_narration(
                video,
                [(offset, clip) for offset, clip, _ in self.narrations],
                final,
                self.output_size,
                self.render_config,
                self.camera,
            )
        if not final.is_file() or final.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not produce a playable video")
        return final

    async def finish(self) -> Path:
        try:
            return await asyncio.to_thread(self._finish_media)
        finally:
            await self._close_runtime()

    async def abort(self) -> None:
        """Tear down the runtime without producing a video (force delete)."""
        try:
            stop_recording(self.recorder)
        except Exception:
            pass
        await self._close_runtime()

    async def _close_runtime(self) -> None:
        if not self.runtime_closed:
            try:
                await self.browser.close()
            except Exception:
                pass
            try:
                await self.playwright.stop()
            except Exception:
                pass
            try:
                self.xvfb.terminate()
            except Exception:
                pass
            try:
                self.xvfb.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.xvfb.kill()
            if self.profile_dir is not None:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.runtime_closed = True
