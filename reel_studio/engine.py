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

from .render import (
    mux_narration,
    probe_duration,
    segmented_render,
    segmented_render_enabled,
    start_recording,
    stop_recording,
)
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
    provider: str
    output_size: tuple[int, int] | None
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
    refs_stale: bool = True
    runtime_closed: bool = False

    @classmethod
    async def create(
        cls,
        start_url: str,
        width: int,
        height: int,
        voice: str,
        provider: str = "edge",
        output_size: tuple[int, int] | None = None,
    ) -> "BrowserSession":
        session_id = uuid.uuid4().hex
        directory = output_root() / session_id
        directory.mkdir(parents=True, exist_ok=True)
        display_number = _free_display()
        display = f":{display_number}"
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", f"{width}x{height}x24", "-ac"],
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
            raise RuntimeError(
                f"Screen recorder exited during startup (exit code {exit_code})"
            )
        return cls(
            session_id, start_url, width, height, voice, provider, output_size,
            directory, display_number, xvfb, playwright, browser, context, page,
            recorder, time.monotonic(),
        )

    async def capture_screenshot(self) -> Path:
        screenshot = self.directory / f"screenshot-{int(time.time() * 1000)}.jpg"
        await self.page.screenshot(path=str(screenshot), type="jpeg", quality=80)
        return screenshot

    async def _stable_selector(self, item: Locator) -> str:
        return await item.evaluate(
            """(el) => {
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
        )

    async def observe(self) -> tuple[dict, Path]:
        screenshot = await self.capture_screenshot()
        self.refs.clear()
        page_text = (await self.page.locator("body").inner_text())[:4000]
        elements = []
        locator = self.page.locator("a,button,input,textarea,select,[role=button],[onclick]")
        count = await locator.count()
        for index in range(count):
            item = locator.nth(index)
            if not await item.is_visible():
                continue
            ref = f"e{index}"
            self.refs[ref] = await self._stable_selector(item)
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
            "page_text": page_text,
            "elements": elements,
            "refs_stale": False,
        }, screenshot

    async def _inject_spotlight(self, target: Locator) -> None:
        await target.evaluate(
            """(el) => {
                const rect = el.getBoundingClientRect();
                const node = document.createElement('div');
                node.dataset.videoDirectorSpotlight = 'true';
                Object.assign(node.style, {
                    position: 'fixed',
                    left: `${rect.left - 14}px`,
                    top: `${rect.top - 14}px`,
                    width: `${rect.width + 28}px`,
                    height: `${rect.height + 28}px`,
                    border: '3px solid rgba(255, 193, 7, 0.95)',
                    borderRadius: '18px',
                    boxShadow: '0 0 0 6px rgba(255, 193, 7, 0.3), 0 0 30px 12px rgba(255, 193, 7, 0.75)',
                    pointerEvents: 'none',
                    zIndex: '2147483647',
                    transition: 'opacity 420ms ease, transform 420ms ease',
                    transform: 'scale(0.94)',
                    opacity: '1',
                });
                document.body.appendChild(node);
                requestAnimationFrame(() => {
                    node.style.transform = 'scale(1.04)';
                    node.style.opacity = '0';
                });
                setTimeout(() => node.remove(), 500);
            }"""
        )

    async def _clear_spotlights(self) -> None:
        await self.page.locator(
            "[data-video-director-spotlight]"
        ).evaluate_all("(nodes) => nodes.forEach((node) => node.remove())")

    async def _visible_text_target(self, text: str) -> Locator | None:
        text = text.strip()
        if not text:
            return None
        matches = self.page.get_by_text(text, exact=False)
        for index in range(await matches.count()):
            candidate = matches.nth(index)
            try:
                if await candidate.is_visible():
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
        before_url = self.page.url
        if action.ref and self.refs_stale:
            return await self.error_result(
                "stale_refs",
                "Element refs are stale; call observe again before using a ref.",
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
            if action_type == "goto":
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
            elif action_type in {"click", "type", "hover", "highlight"}:
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
                await target.wait_for(state="visible", timeout=5000)
                await target.scroll_into_view_if_needed(timeout=5000)
                if action_type == "click":
                    await self._inject_spotlight(target)
                    await target.click()
                    await self.page.wait_for_timeout(500)
                    await self._clear_spotlights()
                elif action_type == "type":
                    await target.fill(action.text or "")
                elif action_type == "hover":
                    await target.hover()
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
            if self.page.url != before_url:
                self.refs_stale = True
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=2000)
            except PlaywrightTimeoutError:
                pass
            await self.page.wait_for_timeout(250)
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
        if clip:
            self.narrations.append((offset, clip, narration))
        self.timeline.append((offset, clip, duration))
        if clip:
            elapsed = time.monotonic() - (self.t0 + offset)
            padding_applied = elapsed < duration
            if elapsed < duration:
                await asyncio.sleep(duration - elapsed)
        else:
            padding_applied = False
        screenshot = await self.capture_screenshot()
        result = {
            "ok": True,
            "offset_seconds": round(offset, 3),
            "url": self.page.url,
            "title": await self.page.title(),
            "changed": self.page.url != before_url,
            "narration_duration": round(duration, 3),
            "padding_applied": padding_applied,
            "refs_stale": self.refs_stale,
            "screenshot_path": str(screenshot),
        }
        if action_type == "scroll_to_text":
            result.update({"box": action_box, "in_viewport": action_in_viewport})
        return result, screenshot

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
        if segmented_render_enabled():
            segmented_render(video, self.timeline, final, self.output_size)
        else:
            mux_narration(
                video,
                [(offset, clip) for offset, clip, _ in self.narrations],
                final,
                self.output_size,
            )
        if not final.is_file() or final.stat().st_size == 0:
            raise RuntimeError("FFmpeg did not produce a playable video")
        return final

    async def finish(self) -> Path:
        try:
            return await asyncio.to_thread(self._finish_media)
        finally:
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
                self.runtime_closed = True
