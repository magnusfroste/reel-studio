"""Browser and recording lifecycle for one directed video session."""

from dataclasses import dataclass, field
import asyncio
import os
from pathlib import Path
import subprocess
import time
import uuid

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from .render import mux_narration, probe_duration, start_recording, stop_recording
from .schema import Action
from .tts import synthesize


DEFAULT_OUTPUT_DIR = Path("/home/ubuntu/.video-director/sessions")


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
    directory: Path
    display_number: int
    xvfb: subprocess.Popen[bytes]
    playwright: object
    browser: Browser
    context: BrowserContext
    page: Page
    recorder: subprocess.Popen[bytes]
    t0: float
    refs: dict[str, Locator] = field(default_factory=dict)
    narrations: list[tuple[float, Path, str]] = field(default_factory=list)
    refs_stale: bool = True

    @classmethod
    async def create(cls, start_url: str, width: int, height: int, voice: str) -> "BrowserSession":
        session_id = uuid.uuid4().hex
        directory = output_root() / session_id
        directory.mkdir(parents=True, exist_ok=True)
        display_number = _free_display()
        display = f":{display_number}"
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", f"{width}x{height}x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{display_number}").exists():
                break
            await asyncio.sleep(0.1)
        else:
            xvfb.terminate()
            raise RuntimeError("Xvfb did not start")

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            env={**os.environ, "DISPLAY": display},
            args=[f"--window-size={width},{height}", "--window-position=0,0", "--kiosk"],
        )
        context = await browser.new_context(viewport={"width": width, "height": height})
        page = await context.new_page()
        await page.goto(start_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(500)
        recorder = start_recording(display, width, height, directory / "screen.mp4")
        # Give ffmpeg one frame before t0 is recorded.
        await asyncio.sleep(0.4)
        return cls(session_id, start_url, width, height, voice, directory, display_number,
                   xvfb, playwright, browser, context, page, recorder, time.monotonic())

    async def capture_screenshot(self) -> Path:
        screenshot = self.directory / f"screenshot-{int(time.time() * 1000)}.jpg"
        await self.page.screenshot(path=str(screenshot), type="jpeg", quality=80)
        return screenshot

    async def observe(self) -> tuple[dict, Path]:
        screenshot = await self.capture_screenshot()
        self.refs.clear()
        elements = []
        locator = self.page.locator("a,button,input,textarea,select,[role=button],[onclick]")
        count = await locator.count()
        for index in range(count):
            item = locator.nth(index)
            if not await item.is_visible():
                continue
            ref = f"e{index}"
            self.refs[ref] = item
            box = await item.bounding_box()
            role = await item.get_attribute("role") or (await item.evaluate(
                "(el) => el.tagName.toLowerCase()"
            ))
            text = (await item.inner_text()).strip() if role not in {"input", "textarea", "select"} else ""
            if not text:
                text = await item.get_attribute("aria-label") or await item.get_attribute("placeholder") or ""
            elements.append({"ref": ref, "role": role, "text": text, "box": box})
        self.refs_stale = False
        return {
            "screenshot_path": str(screenshot),
            "url": self.page.url,
            "title": await self.page.title(),
            "elements": elements,
            "refs_stale": False,
        }, screenshot

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
        before_url = self.page.url
        if action.ref and self.refs_stale:
            return await self.error_result(
                "stale_refs",
                "Element refs are stale; call observe again before using a ref.",
            )
        if narration:
            try:
                clip = await synthesize(narration, self.voice, self.directory)
                duration = probe_duration(clip)
            except Exception as exc:
                return await self.error_result("narration_failed", str(exc))
        try:
            action_type = action.type
            if action_type == "goto":
                if not action.url:
                    return await self.error_result("invalid_action", "goto requires url")
                await self.page.goto(action.url, wait_until="domcontentloaded", timeout=15000)
            elif action_type in {"click", "type", "hover", "highlight"}:
                if not action.ref or action.ref not in self.refs:
                    return await self.error_result(
                        "unknown_ref", f"Unknown element ref: {action.ref}"
                    )
                target = self.refs[action.ref]
                await target.scroll_into_view_if_needed(timeout=5000)
                if action_type == "click":
                    await target.click()
                elif action_type == "type":
                    await target.fill(action.text or "")
                elif action_type == "hover":
                    await target.hover()
                else:
                    await target.evaluate(
                        "(el) => { el.dataset.videoDirectorOldOutline = el.style.outline; "
                        "el.style.outline = '4px solid #ff3b30'; }"
                    )
                    await self.page.wait_for_timeout(1500)
                    await target.evaluate(
                        "(el) => { el.style.outline = el.dataset.videoDirectorOldOutline || ''; "
                        "delete el.dataset.videoDirectorOldOutline; }"
                    )
            elif action_type == "scroll":
                await self.page.mouse.wheel(0, action.dy)
            elif action_type == "wait":
                await self.page.wait_for_timeout(action.ms)
            if action_type in {"goto", "click", "type", "scroll"}:
                self.refs_stale = True
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except PlaywrightTimeoutError:
                pass
            await self.page.wait_for_timeout(250)
        except PlaywrightTimeoutError as exc:
            return await self.error_result("timeout", str(exc))
        except (PlaywrightError, ValueError) as exc:
            return await self.error_result("action_failed", str(exc))
        if clip:
            self.narrations.append((offset, clip, narration))
            elapsed = time.monotonic() - (self.t0 + offset)
            padding_applied = elapsed < duration
            if elapsed < duration:
                await asyncio.sleep(duration - elapsed)
        else:
            padding_applied = False
        screenshot = await self.capture_screenshot()
        return {
            "ok": True,
            "offset_seconds": round(offset, 3),
            "url": self.page.url,
            "title": await self.page.title(),
            "changed": self.page.url != before_url,
            "narration_duration": round(duration, 3),
            "padding_applied": padding_applied,
            "refs_stale": self.refs_stale,
            "screenshot_path": str(screenshot),
        }, screenshot

    def status(self) -> dict:
        elapsed = time.monotonic() - self.t0
        narrated = sum(probe_duration(clip) for _, clip, _ in self.narrations)
        return {
            "elapsed_seconds": round(elapsed, 3),
            "recorded_steps": len(self.narrations),
            "total_narrated_seconds": round(narrated, 3),
            "estimated_video_length": round(max(elapsed, max(
                (offset + probe_duration(clip) for offset, clip, _ in self.narrations),
                default=0.0,
            )), 3),
        }

    async def finish(self) -> Path:
        stop_recording(self.recorder)
        video = self.directory / "screen.mp4"
        final = self.directory / "video.mp4"
        mux_narration(video, [(offset, clip) for offset, clip, _ in self.narrations], final)
        await self.browser.close()
        await self.playwright.stop()
        self.xvfb.terminate()
        try:
            self.xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.xvfb.kill()
        return final
