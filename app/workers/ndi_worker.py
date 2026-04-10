"""
NDI Output Worker

Each instance runs in its own process:
  1. Launches headless Playwright browser
  2. Captures screenshots at the configured capture_fps into a pre-allocated frame buffer
  3. Sends frames to NDI at the global output_fps (duplicating frames as needed)
  4. Optionally auto-refreshes content at a configurable interval
  5. Periodically recycles the browser to prevent Chromium memory leaks
  6. Updates a shared heartbeat timestamp so the watchdog can detect hangs

Memory management:
  - A single BGRX frame buffer is pre-allocated at startup and reused for every
    capture, avoiding per-frame numpy/PIL allocations that cause heap fragmentation
    and prevent Python from returning memory to the OS.

Browser recycling:
  - Chromium leaks memory over long runs (DOM caches, JS heap growth, internal
    buffers). Every `browser_recycle_hours` the worker tears down the entire
    browser and launches a fresh one. The last captured frame continues to be
    sent to NDI during the ~1-2s recycle window so receivers see no interruption.

Heartbeat:
  - The worker writes time.monotonic() into a multiprocessing.Value after every
    successful frame send. The parent watchdog checks this value; if it hasn't
    updated in > heartbeat_timeout seconds the worker is considered hung and
    gets killed + restarted. This catches cases like an unresponsive webpage
    causing Playwright to block indefinitely.
"""

import io
import gc
import time
import signal
import ctypes
import logging
import multiprocessing as mp
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Default: recycle browser every 4 hours
DEFAULT_RECYCLE_HOURS = 4

# Heartbeat stale after 30 seconds = considered hung
HEARTBEAT_TIMEOUT = 30.0


class NDIWorker:
    """Manages capture + NDI send for a single output instance."""

    def __init__(
        self,
        instance_id: int,
        ndi_name: str,
        source_type: str,
        source_value: str,
        width: int,
        height: int,
        capture_fps: int,
        output_fps: int,
        refresh_interval: int = 0,
        browser_recycle_hours: float = DEFAULT_RECYCLE_HOURS,
        text_settings: Optional[dict] = None,
        heartbeat: Optional[mp.Value] = None,
    ):
        self.instance_id = instance_id
        self.ndi_name = ndi_name
        self.source_type = source_type
        self.source_value = source_value
        self.width = width
        self.height = height
        self.capture_fps = capture_fps
        self.output_fps = output_fps
        self.refresh_interval = refresh_interval
        self.browser_recycle_hours = browser_recycle_hours
        self.text_settings = text_settings or {}
        self._stop_event = mp.Event()
        self._heartbeat = heartbeat  # shared with parent process

    # ------------------------------------------------------------------
    # Frame buffer management
    # ------------------------------------------------------------------

    def _alloc_frame_buffer(self) -> np.ndarray:
        """Pre-allocate a single BGRX frame buffer. Reused for every capture."""
        buf = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        logger.info(
            f"Frame buffer allocated: {self.width}x{self.height} "
            f"({buf.nbytes / 1024 / 1024:.1f} MB)"
        )
        return buf

    def _capture_into_buffer(self, page, frame_buffer: np.ndarray) -> bool:
        """
        Capture a screenshot and decode it directly into the pre-allocated buffer.
        Returns True on success.
        """
        try:
            screenshot_bytes = page.screenshot(type="png")
            img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGBA")
            arr = np.asarray(img)  # zero-copy view when possible
            # RGBA -> BGRX (swap R and B) directly into buffer
            frame_buffer[:, :, 0] = arr[:, :, 2]  # B
            frame_buffer[:, :, 1] = arr[:, :, 1]  # G
            frame_buffer[:, :, 2] = arr[:, :, 0]  # R
            frame_buffer[:, :, 3] = arr[:, :, 3]  # X (alpha)
            del arr
            img.close()
            return True
        except Exception as e:
            logger.warning(f"Screenshot failed for {self.ndi_name}: {e}")
            return False

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    def _build_text_html(self) -> str:
        ts = self.text_settings
        content = ts.get("content", self.source_value)
        font = ts.get("font", "Arial")
        size = ts.get("size", 48)
        color = ts.get("color", "#FFFFFF")
        bg = ts.get("bg_color", "#000000")
        align = ts.get("align", "center")

        return f"""<!DOCTYPE html>
<html><head><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    width: {self.width}px; height: {self.height}px;
    background: {bg};
    display: flex; align-items: center; justify-content: center;
    font-family: '{font}', sans-serif;
    font-size: {size}px;
    color: {color};
    text-align: {align};
    padding: 40px;
    overflow: hidden;
  }}
  .content {{ max-width: 90%; word-wrap: break-word; }}
</style></head>
<body><div class="content">{content}</div></body></html>"""

    def _load_content(self, page):
        """Load or reload content into the Playwright page."""
        if self.source_type == "text":
            page.set_content(self._build_text_html())
        elif self.source_type == "image":
            img_html = f"""<!DOCTYPE html><html><head><style>
                *{{margin:0;padding:0}}
                body{{width:{self.width}px;height:{self.height}px;background:#000;
                display:flex;align-items:center;justify-content:center;overflow:hidden}}
                img{{max-width:100%;max-height:100%;object-fit:contain}}
            </style></head><body>
            <img src="{self.source_value}"></body></html>"""
            page.set_content(img_html)
            page.wait_for_load_state("networkidle")
        else:  # webpage
            page.goto(self.source_value, wait_until="networkidle", timeout=30000)

    def _launch_browser(self, pw):
        """Create a fresh browser + context + page and load content."""
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                f"--window-size={self.width},{self.height}",
            ],
        )
        context = browser.new_context(
            viewport={"width": self.width, "height": self.height},
            device_scale_factor=1,
        )
        page = context.new_page()

        try:
            self._load_content(page)
        except Exception as e:
            logger.error(f"Failed to load content for {self.ndi_name}: {e}")

        return browser, context, page

    def _teardown_browser(self, page, context, browser):
        """Clean shutdown of browser components."""
        try:
            page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        # Force GC after tearing down Chromium
        gc.collect()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _update_heartbeat(self):
        """Write current monotonic time to shared value."""
        if self._heartbeat is not None:
            self._heartbeat.value = time.monotonic()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        """Main loop — runs in a child process."""
        try:
            signal.signal(signal.SIGTERM, lambda *_: self._stop_event.set())
            signal.signal(signal.SIGINT, lambda *_: self._stop_event.set())
        except OSError:
            # Windows may not support these signals in all contexts;
            # graceful shutdown still works via _stop_event.set() from parent.
            pass

        try:
            import NDIlib as ndi
        except ImportError:
            logger.error("ndi-python not installed — running in dummy mode")
            self._run_dummy_mode()
            return

        from playwright.sync_api import sync_playwright

        # --- NDI setup ---
        if not ndi.initialize():
            logger.error("Failed to initialize NDI")
            return

        send_create = ndi.SendCreate()
        send_create.ndi_name = self.ndi_name
        send_create.clock_video = True
        ndi_send = ndi.send_create(send_create)

        if ndi_send is None:
            logger.error(f"Failed to create NDI sender: {self.ndi_name}")
            ndi.destroy()
            return

        logger.info(f"NDI sender created: {self.ndi_name}")

        # --- Pre-allocate frame buffer ---
        frame_buffer = self._alloc_frame_buffer()
        frame_ready = False

        video_frame = ndi.VideoFrameV2()
        video_frame.xres = self.width
        video_frame.yres = self.height
        video_frame.FourCC = ndi.FOURCC_VIDEO_TYPE_BGRX
        video_frame.frame_rate_N = self.output_fps * 1000
        video_frame.frame_rate_D = 1000

        # --- Playwright setup ---
        pw = sync_playwright().start()
        browser = context = page = None
        try:
            browser, context, page = self._launch_browser(pw)

            # --- Timing ---
            capture_interval = 1.0 / self.capture_fps
            output_interval = 1.0 / self.output_fps
            recycle_interval = self.browser_recycle_hours * 3600.0

            last_capture_time = 0.0
            last_refresh_time = time.monotonic()
            last_recycle_time = time.monotonic()

            logger.info(
                f"Worker started: {self.ndi_name} | "
                f"{self.width}x{self.height} | "
                f"capture={self.capture_fps}fps, output={self.output_fps}fps, "
                f"refresh={self.refresh_interval}s, "
                f"recycle={self.browser_recycle_hours}h"
            )

            self._update_heartbeat()

            while not self._stop_event.is_set():
                frame_start = time.monotonic()

                # --- Browser recycle ---
                if frame_start - last_recycle_time >= recycle_interval:
                    logger.info(f"Recycling browser: {self.ndi_name}")
                    self._teardown_browser(page, context, browser)
                    browser, context, page = self._launch_browser(pw)
                    last_recycle_time = frame_start
                    last_refresh_time = frame_start  # content was just loaded
                    logger.info(f"Browser recycled: {self.ndi_name}")

                # --- Auto-refresh content ---
                if self.refresh_interval > 0:
                    if frame_start - last_refresh_time >= self.refresh_interval:
                        try:
                            logger.info(f"Auto-refreshing: {self.ndi_name}")
                            self._load_content(page)
                            last_refresh_time = frame_start
                        except Exception as e:
                            logger.warning(f"Auto-refresh failed for {self.ndi_name}: {e}")

                # --- Capture into buffer ---
                if frame_start - last_capture_time >= capture_interval:
                    if self._capture_into_buffer(page, frame_buffer):
                        frame_ready = True
                        last_capture_time = frame_start

                # --- Send to NDI ---
                if frame_ready:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)
                    self._update_heartbeat()

                # --- Pace to output FPS with drift correction ---
                target_time = frame_start + output_interval
                now = time.monotonic()
                sleep_time = target_time - now
                if sleep_time > 0.001:
                    time.sleep(sleep_time)
                elif sleep_time < -output_interval:
                    # We're more than a full frame behind; reset to avoid spiral
                    pass
        except Exception:
            logger.exception(f"Worker crashed: {self.ndi_name}")
        finally:
            # --- Cleanup (always runs) ---
            logger.info(f"Stopping worker: {self.ndi_name}")
            if page is not None:
                self._teardown_browser(page, context, browser)
            try:
                pw.stop()
            except Exception:
                pass
            try:
                ndi.send_destroy(ndi_send)
            except Exception:
                pass
            try:
                ndi.destroy()
            except Exception:
                pass

    def _run_dummy_mode(self):
        """Fallback when NDI SDK is not available."""
        from playwright.sync_api import sync_playwright

        logger.warning(f"DUMMY MODE (no NDI): {self.ndi_name}")

        frame_buffer = self._alloc_frame_buffer()

        pw = sync_playwright().start()
        browser = context = page = None
        try:
            browser, context, page = self._launch_browser(pw)

            capture_interval = 1.0 / self.capture_fps
            recycle_interval = self.browser_recycle_hours * 3600.0
            last_refresh_time = time.monotonic()
            last_recycle_time = time.monotonic()

            while not self._stop_event.is_set():
                now = time.monotonic()

                # Browser recycle
                if now - last_recycle_time >= recycle_interval:
                    logger.info(f"Recycling browser (dummy): {self.ndi_name}")
                    self._teardown_browser(page, context, browser)
                    browser, context, page = self._launch_browser(pw)
                    last_recycle_time = now
                    last_refresh_time = now

                # Auto-refresh
                if self.refresh_interval > 0 and now - last_refresh_time >= self.refresh_interval:
                    try:
                        self._load_content(page)
                        last_refresh_time = now
                        logger.info(f"Auto-refreshed (dummy): {self.ndi_name}")
                    except Exception:
                        pass

                self._capture_into_buffer(page, frame_buffer)
                self._update_heartbeat()
                time.sleep(capture_interval)
        except Exception:
            logger.exception(f"Dummy worker crashed: {self.ndi_name}")
        finally:
            if page is not None:
                self._teardown_browser(page, context, browser)
            try:
                pw.stop()
            except Exception:
                pass

    def stop(self):
        self._stop_event.set()


def worker_entry(worker: NDIWorker):
    """Multiprocessing entry point."""
    worker.run()
