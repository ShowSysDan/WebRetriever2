"""
NDI Output Worker

Each instance runs in its own process:
  1. Launches headless Playwright browser (webpage/image/text sources)
     — or opens a V4L2 webcam via OpenCV (webcam source, no browser at all)
     — or decodes an uploaded video file via OpenCV/FFmpeg (video source,
       with play/stop control over shared mp.Values from the API process)
  2. Captures frames at the configured capture_fps into a pre-allocated frame buffer
  3. Sends frames to NDI at the global output_fps (duplicating frames as needed)
  4. Optionally auto-refreshes content at a configurable interval
  5. Periodically recycles the browser to prevent Chromium memory leaks
  6. Updates a shared heartbeat timestamp so the watchdog can detect hangs

Webcam sources:
  - source_value is the V4L2 device path — preferably a stable udev symlink
    (/dev/v4l/by-id/... or by-path/...) so the instance stays bound to the
    correct physical camera across replugs and reboots; a raw /dev/videoN
    path also works but those numbers shuffle with enumeration order.
  - A background grabber thread reads frames continuously at the camera's
    native rate; the send loop samples the latest frame at capture_fps and
    sends at output_fps, so capture/output stay decoupled exactly like the
    browser sources. If the camera stalls or is unplugged, the grabber
    reopens it automatically while the last good frame keeps streaming.

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
import os
import gc
import sys
import time
import signal
import logging
import tempfile
import threading
import multiprocessing as mp
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Default: recycle browser every 4 hours
DEFAULT_RECYCLE_HOURS = 4

# Heartbeat stale after 30 seconds = considered hung
HEARTBEAT_TIMEOUT = 30.0

# Consecutive failed reads before the grabber reopens the camera
WEBCAM_MAX_READ_FAILURES = 5

# Seconds between reopen attempts when the camera is unavailable
WEBCAM_RECONNECT_DELAY = 2.0

# Video playback commands (shared mp.Value between API process and worker)
VIDEO_CMD_NONE = 0
VIDEO_CMD_PLAY = 1
VIDEO_CMD_STOP = 2

# Video playback states (worker reports back through shared mp.Value)
VIDEO_STATE_STOPPED = 0
VIDEO_STATE_PLAYING = 1


class WebcamGrabber(threading.Thread):
    """Reads frames from a V4L2 device on a background thread.

    cap.read() blocks at the camera's native frame rate, so it runs on its
    own thread and only the most recent frame is kept. That lets the NDI
    send loop keep its own timing (capture_fps sampling, output_fps sends)
    instead of being paced by the camera.

    Reopens the device automatically if reads start failing (camera
    unplugged, driver stall).

    Memory note: cap.read() intentionally allocates a fresh array per frame
    (rather than reading into a shared buffer) so the send loop can hold a
    reference to the previous frame without tearing while the next one is
    decoded. These are large single allocations that glibc serves via mmap
    and returns to the OS on free — not the small-object churn the frame
    buffer pre-allocation elsewhere in this module is designed to avoid.
    """

    def __init__(self, device: str, width: int, height: int, fps: int):
        super().__init__(daemon=True, name="webcam-grabber")
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.lock = threading.Lock()
        self.frame = None  # latest BGR frame (numpy array)
        self.frame_seq = 0
        self.connected = False
        self._stop_event = threading.Event()

    def _open(self):
        import cv2

        backend = cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY

        # Resolve stable udev symlinks (/dev/v4l/by-id/..., by-path/...) to the
        # current /dev/videoN. Resolved fresh on every open attempt: after a
        # replug the camera may come back as a different videoN, and udev
        # re-points the symlink — this is what keeps an instance bound to the
        # correct physical camera. A dangling/missing link just fails the open
        # and we retry.
        device = os.path.realpath(str(self.device))

        # Prefer opening by index: /dev/videoN → N. Some OpenCV builds ship a
        # V4L2 backend that can't open by filename, but index-based open maps
        # to the same /dev/videoN node and is always supported.
        cap = None
        digits = "".join(ch for ch in os.path.basename(device) if ch.isdigit())
        if digits and device.startswith("/dev/video"):
            cap = cv2.VideoCapture(int(digits), backend)
            if not cap.isOpened():
                cap.release()
                cap = None
        if cap is None:
            cap = cv2.VideoCapture(device, backend)
        if not cap.isOpened():
            cap.release()
            return None
        # MJPEG is required for high res/fps over USB — uncompressed YUYV
        # tops out around 5-10fps at 1080p on USB 2.0 bandwidth.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        # Keep the driver queue shallow so sampled frames are always fresh
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        resolved = f" → {device}" if device != str(self.device) else ""
        logger.info(
            f"Webcam opened: {self.device}{resolved} — negotiated "
            f"{actual_w}x{actual_h} @ {actual_fps:.0f}fps "
            f"(requested {self.width}x{self.height} @ {self.fps}fps)"
        )
        return cap

    def run(self):
        cap = None
        failures = 0
        try:
            while not self._stop_event.is_set():
                if cap is None:
                    cap = self._open()
                    if cap is None:
                        self.connected = False
                        logger.warning(
                            f"Webcam unavailable: {self.device}, retrying in "
                            f"{WEBCAM_RECONNECT_DELAY}s"
                        )
                        if self._stop_event.wait(WEBCAM_RECONNECT_DELAY):
                            break
                        continue
                    self.connected = True
                    failures = 0

                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    if failures >= WEBCAM_MAX_READ_FAILURES:
                        logger.warning(
                            f"Webcam read failing ({failures}x), reopening: {self.device}"
                        )
                        cap.release()
                        cap = None
                        self.connected = False
                    continue

                failures = 0
                with self.lock:
                    self.frame = frame
                    self.frame_seq += 1
        finally:
            if cap is not None:
                cap.release()

    def stop(self):
        self._stop_event.set()


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
        video_settings: Optional[dict] = None,
        heartbeat: Optional[mp.Value] = None,
        video_cmd: Optional[mp.Value] = None,
        video_state: Optional[mp.Value] = None,
        preview_dir: Optional[str] = None,
        preview_interval: float = 2.0,
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
        self.video_settings = video_settings or {}
        self._stop_event = mp.Event()
        self._heartbeat = heartbeat  # shared with parent process
        self._video_cmd = video_cmd  # play/stop commands from the API process
        self._video_state = video_state  # playback state reported to the API process
        self._preview_dir = preview_dir
        self._preview_interval = preview_interval

    # ------------------------------------------------------------------
    # Frame buffer management
    # ------------------------------------------------------------------

    def _alloc_frame_buffer(self) -> np.ndarray:
        """Pre-allocate a single BGRX frame buffer. Reused for every capture."""
        buf = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        buf[:, :, 3] = 255  # X channel — set once, never touched again
        logger.info(
            f"Frame buffer allocated: {self.width}x{self.height} "
            f"({buf.nbytes / 1024 / 1024:.1f} MB)"
        )
        return buf

    def _capture_into_buffer(self, page, frame_buffer: np.ndarray) -> bool:
        """
        Capture a screenshot and decode it directly into the pre-allocated buffer.
        Returns True on success.

        Performance notes:
          - JPEG is ~5x faster to encode (Chromium) and ~3x faster to decode
            (Pillow) compared to PNG. At 1080p this saves ~25ms per frame.
          - Single-pass RGB→BGR reversal via arr[:, :, ::-1] instead of
            4 separate channel copies.
          - No .convert("RGBA") needed — JPEG is already RGB, and NDI's
            BGRX X channel is just padding (set to 255 once).
        """
        try:
            screenshot_bytes = page.screenshot(type="jpeg", quality=90)
            img = Image.open(io.BytesIO(screenshot_bytes))
            arr = np.asarray(img)  # RGB uint8, zero-copy view when possible
            # RGB → BGR in one pass, write directly into buffer
            frame_buffer[:, :, :3] = arr[:, :, ::-1]
            # X channel stays 255 (set once in _alloc_frame_buffer)
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

    def _load_content(self, page, reload: bool = False):
        """Load or reload content into the Playwright page.

        When reload=True and the source is a webpage, issue page.reload() instead
        of page.goto(). This reuses the already-parsed frame tree and compiled
        JS/CSS caches, which is cheaper and produces less memory churn than a
        full navigation. For text/image sources the HTML is regenerated either
        way (and may have changed), so set_content is still used.
        """
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
            if reload:
                page.reload(wait_until="networkidle", timeout=30000)
            else:
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
                "--mute-audio",
                "--disable-extensions",
                "--disable-features=TranslateUI",
                "--disable-blink-features=AutomationControlled",
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

    def _teardown_playwright(self, pw, page, context, browser):
        """Full shutdown of the browser stack and the Playwright driver."""
        if page is not None:
            self._teardown_browser(page, context, browser)
        try:
            pw.stop()
        except Exception:
            pass

    def _recycle_and_refresh(self, pw, browser, context, page, now,
                             last_recycle_time, last_refresh_time):
        """Shared browser-recycle + auto-refresh handling for the main and
        dummy loops. Returns the (possibly relaunched) browser/context/page
        and the updated timestamps."""
        recycle_interval = self.browser_recycle_hours * 3600.0

        if now - last_recycle_time >= recycle_interval:
            logger.info(f"Recycling browser: {self.ndi_name}")
            self._teardown_browser(page, context, browser)
            browser, context, page = self._launch_browser(pw)
            last_recycle_time = now
            last_refresh_time = now  # content was just loaded
            logger.info(f"Browser recycled: {self.ndi_name}")

        if self.refresh_interval > 0 and now - last_refresh_time >= self.refresh_interval:
            try:
                logger.info(f"Auto-refreshing: {self.ndi_name}")
                self._load_content(page, reload=True)
                last_refresh_time = now
            except Exception as e:
                logger.warning(f"Auto-refresh failed for {self.ndi_name}: {e}")

        return browser, context, page, last_recycle_time, last_refresh_time

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _update_heartbeat(self):
        """Write current monotonic time to shared value."""
        if self._heartbeat is not None:
            self._heartbeat.value = time.monotonic()

    # ------------------------------------------------------------------
    # NDI lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _destroy_ndi(ndi, ndi_send):
        """Release the NDI sender and library, ignoring shutdown errors."""
        try:
            ndi.send_destroy(ndi_send)
        except Exception:
            pass
        try:
            ndi.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Preview thumbnail
    # ------------------------------------------------------------------

    def _save_preview(self, frame_buffer: np.ndarray):
        """Save a small JPEG preview from the current frame buffer."""
        if not self._preview_dir:
            return
        try:
            # BGRX → RGB as a reversed-stride view (no copy); Pillow
            # materializes on resize/save so the zero-copy view is safe.
            rgb_view = frame_buffer[:, :, 2::-1]
            img = Image.fromarray(rgb_view, "RGB")
            # Downscale to 320px wide, maintain aspect ratio
            thumb_w = 320
            thumb_h = int(self.height * (thumb_w / self.width))
            img = img.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            # Atomic write: temp file then replace
            dest = os.path.join(self._preview_dir, f"{self.instance_id}.jpg")
            fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=self._preview_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    img.save(f, "JPEG", quality=60)
                os.replace(tmp, dest)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.debug(f"Preview save failed for {self.ndi_name}: {e}")

    # ------------------------------------------------------------------
    # Webcam capture loop
    # ------------------------------------------------------------------

    def _run_webcam_loop(self, frame_buffer, ndi=None, ndi_send=None, video_frame=None):
        """Capture from a V4L2 webcam and send to NDI (no browser involved).

        Pass ndi=None to run in dummy mode (preview thumbnails only).

        The heartbeat is updated every iteration, not just on send: the
        grabber thread owns all blocking camera I/O, so this loop can't hang
        the way Playwright can — and while a camera is unplugged we keep
        streaming the last good frame, which shouldn't count as unhealthy.
        """
        import cv2

        grabber = WebcamGrabber(
            self.source_value, self.width, self.height, self.capture_fps
        )
        grabber.start()

        capture_interval = 1.0 / self.capture_fps
        output_interval = 1.0 / self.output_fps
        frame_ready = False
        last_capture_time = 0.0
        last_preview_time = 0.0
        last_seq = 0
        resize_buf = None  # allocated once, only if the camera mode differs

        logger.info(
            f"Webcam worker started: {self.ndi_name} | device={self.source_value} | "
            f"{self.width}x{self.height} | "
            f"capture={self.capture_fps}fps, output={self.output_fps}fps"
        )
        self._update_heartbeat()

        try:
            while not self._stop_event.is_set():
                frame_start = time.monotonic()

                # --- Sample latest camera frame into buffer ---
                if frame_start - last_capture_time >= capture_interval:
                    with grabber.lock:
                        bgr = grabber.frame
                        seq = grabber.frame_seq
                    # cap.read() allocates a fresh array per frame, so using
                    # the reference outside the lock is safe.
                    if bgr is not None and seq != last_seq:
                        last_seq = seq
                        if bgr.shape[0] != self.height or bgr.shape[1] != self.width:
                            # Camera negotiated a different mode than requested;
                            # resize into a reused buffer to avoid per-frame allocation
                            if resize_buf is None:
                                resize_buf = np.empty(
                                    (self.height, self.width, 3), dtype=np.uint8
                                )
                            cv2.resize(bgr, (self.width, self.height), dst=resize_buf)
                            bgr = resize_buf
                        frame_buffer[:, :, :3] = bgr
                        frame_ready = True
                        last_capture_time = frame_start
                        if frame_start - last_preview_time >= self._preview_interval:
                            self._save_preview(frame_buffer)
                            last_preview_time = frame_start

                # --- Send to NDI (duplicates last frame up to output_fps) ---
                if frame_ready and ndi is not None:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)

                self._update_heartbeat()

                # --- Pace to output FPS ---
                target_time = frame_start + output_interval
                sleep_time = target_time - time.monotonic()
                if sleep_time > 0.001:
                    time.sleep(sleep_time)
        finally:
            grabber.stop()
            grabber.join(timeout=5)

    def _run_webcam_source(self, frame_buffer, ndi=None, ndi_send=None, video_frame=None):
        """Run the webcam loop with shared error handling (real or dummy mode)."""
        try:
            self._run_webcam_loop(
                frame_buffer, ndi=ndi, ndi_send=ndi_send, video_frame=video_frame
            )
        except ImportError:
            logger.error(
                "opencv-python-headless not installed — webcam source "
                f"'{self.ndi_name}' cannot run. Install it and restart."
            )
            self._idle_until_stopped()
        except Exception:
            logger.exception(f"Worker crashed: {self.ndi_name}")

    # ------------------------------------------------------------------
    # Video file playback loop
    # ------------------------------------------------------------------

    def _poll_video_cmd(self) -> int:
        """Read and clear the pending playback command (edge-triggered)."""
        if self._video_cmd is None:
            return VIDEO_CMD_NONE
        with self._video_cmd.get_lock():
            cmd = self._video_cmd.value
            self._video_cmd.value = VIDEO_CMD_NONE
        return cmd

    def _report_video_state(self, playing: bool):
        if self._video_state is not None:
            self._video_state.value = VIDEO_STATE_PLAYING if playing else VIDEO_STATE_STOPPED

    def _run_video_loop(self, frame_buffer, ndi=None, ndi_send=None, video_frame=None):
        """Decode a video file with OpenCV and send it to NDI (no browser).

        Pass ndi=None to run in dummy mode (preview thumbnails only).

        Playback model:
          - Frames advance at the file's native FPS; NDI sends run at the
            global output_fps, duplicating the current frame in between —
            the same capture/output decoupling as every other source.
          - `play` always restarts from the first frame. `stop` freezes on
            the frame chosen by video_hold ("last" = current frame stays on
            air, "first" = jump back to the opening frame).
          - When a play-once video reaches the end it stops and holds per
            video_hold; in loop mode it seeks back to frame 0 and continues.
          - While stopped, the held frame keeps streaming so receivers never
            lose the source.
        """
        import cv2

        path = self.source_value
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            logger.error(f"Cannot open video file for '{self.ndi_name}': {path}")
            self._idle_until_stopped()
            return

        src_fps = cap.get(cv2.CAP_PROP_FPS)
        if not src_fps or src_fps < 1 or src_fps > 240:
            src_fps = 30.0
        frame_interval = 1.0 / src_fps
        output_interval = 1.0 / self.output_fps

        loop_playback = bool(self.video_settings.get("loop", False))
        hold = self.video_settings.get("hold", "last")
        playing = bool(self.video_settings.get("autoplay", False))

        # Letterbox geometry: scale to fit inside the output resolution while
        # preserving aspect ratio, centered on black. Computed once — the
        # file's frame size never changes mid-stream.
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or self.width
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self.height
        scale = min(self.width / src_w, self.height / src_h)
        fit_w = max(1, int(round(src_w * scale)))
        fit_h = max(1, int(round(src_h * scale)))
        off_x = (self.width - fit_w) // 2
        off_y = (self.height - fit_h) // 2
        needs_resize = (fit_w, fit_h) != (src_w, src_h)
        resize_buf = np.empty((fit_h, fit_w, 3), dtype=np.uint8) if needs_resize else None
        # BGRX buffer starts zeroed (black) with X=255, so the letterbox bars
        # are already in place — only the fitted region is ever written.

        def blit(bgr):
            if needs_resize:
                cv2.resize(bgr, (fit_w, fit_h), dst=resize_buf)
                bgr = resize_buf
            frame_buffer[off_y:off_y + fit_h, off_x:off_x + fit_w, :3] = bgr

        def read_first_frame():
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
            if ok and frame is not None:
                blit(frame)
            return ok

        # Show the first frame immediately so the NDI source is never blank
        if not read_first_frame():
            logger.error(f"Cannot decode video file for '{self.ndi_name}': {path}")
            cap.release()
            self._idle_until_stopped()
            return
        if playing:
            # Frame 0 is already on the buffer; keep decoding from frame 1
            pass
        else:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

        logger.info(
            f"Video worker started: {self.ndi_name} | file={path} | "
            f"{src_w}x{src_h}@{src_fps:.2f}fps → {self.width}x{self.height} | "
            f"loop={loop_playback}, hold={hold}, autoplay={playing}, "
            f"output={self.output_fps}fps"
        )
        self._update_heartbeat()
        self._report_video_state(playing)

        next_frame_time = time.monotonic()
        last_preview_time = 0.0

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()

                # --- Apply pending play/stop command ---
                cmd = self._poll_video_cmd()
                if cmd == VIDEO_CMD_PLAY:
                    if read_first_frame():
                        playing = True
                        next_frame_time = now + frame_interval
                    self._report_video_state(playing)
                elif cmd == VIDEO_CMD_STOP:
                    playing = False
                    if hold == "first":
                        read_first_frame()
                    self._report_video_state(playing)

                # --- Advance playback at the file's native FPS ---
                # May decode several frames per output tick (e.g. 60fps file
                # on a 30fps output): extra frames are decoded-and-dropped so
                # wall-clock playback speed stays correct. The budget bounds
                # decode cost per iteration; if still behind after that,
                # resync rather than stalling the send loop.
                if playing:
                    latest = None
                    decode_budget = 8
                    while playing and now >= next_frame_time and decode_budget > 0:
                        decode_budget -= 1
                        ok, frame = cap.read()
                        if ok and frame is not None:
                            latest = frame
                            next_frame_time += frame_interval
                        else:
                            # End of file (or decode error mid-file)
                            if loop_playback:
                                if not read_first_frame():
                                    logger.warning(
                                        f"Video loop restart failed, reopening: {path}"
                                    )
                                    cap.release()
                                    cap = cv2.VideoCapture(path)
                                    read_first_frame()
                                latest = None  # read_first_frame already blitted
                                next_frame_time = now + frame_interval
                            else:
                                playing = False
                                if hold == "first":
                                    read_first_frame()
                                    latest = None
                                self._report_video_state(playing)
                                logger.info(f"Video finished (hold={hold}): {self.ndi_name}")
                    if latest is not None:
                        blit(latest)
                    if playing and next_frame_time < now:
                        next_frame_time = now + frame_interval

                # --- Send to NDI (held frame keeps streaming while stopped) ---
                if ndi is not None:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)

                if now - last_preview_time >= self._preview_interval:
                    self._save_preview(frame_buffer)
                    last_preview_time = now

                self._update_heartbeat()

                # --- Pace to output FPS ---
                sleep_time = (now + output_interval) - time.monotonic()
                if sleep_time > 0.001:
                    time.sleep(sleep_time)
        finally:
            cap.release()
            self._report_video_state(False)

    def _run_video_source(self, frame_buffer, ndi=None, ndi_send=None, video_frame=None):
        """Run the video loop with shared error handling (real or dummy mode)."""
        try:
            self._run_video_loop(
                frame_buffer, ndi=ndi, ndi_send=ndi_send, video_frame=video_frame
            )
        except ImportError:
            logger.error(
                "opencv-python-headless not installed — video source "
                f"'{self.ndi_name}' cannot run. Install it and restart."
            )
            self._idle_until_stopped()
        except Exception:
            logger.exception(f"Worker crashed: {self.ndi_name}")

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

        # --- Webcam source: capture via V4L2/OpenCV, no browser ---
        if self.source_type == "webcam":
            try:
                self._run_webcam_source(
                    frame_buffer, ndi=ndi, ndi_send=ndi_send, video_frame=video_frame
                )
            finally:
                logger.info(f"Stopping worker: {self.ndi_name}")
                self._destroy_ndi(ndi, ndi_send)
            return

        # --- Video file source: decode via OpenCV/FFmpeg, no browser ---
        if self.source_type == "video":
            try:
                self._run_video_source(
                    frame_buffer, ndi=ndi, ndi_send=ndi_send, video_frame=video_frame
                )
            finally:
                logger.info(f"Stopping worker: {self.ndi_name}")
                self._destroy_ndi(ndi, ndi_send)
            return

        # --- Playwright setup ---
        pw = sync_playwright().start()
        browser = context = page = None
        try:
            browser, context, page = self._launch_browser(pw)

            # --- Timing ---
            capture_interval = 1.0 / self.capture_fps
            output_interval = 1.0 / self.output_fps

            last_capture_time = 0.0
            last_refresh_time = time.monotonic()
            last_recycle_time = time.monotonic()
            last_preview_time = 0.0

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

                # --- Browser recycle + auto-refresh ---
                browser, context, page, last_recycle_time, last_refresh_time = \
                    self._recycle_and_refresh(
                        pw, browser, context, page, frame_start,
                        last_recycle_time, last_refresh_time,
                    )

                # --- Capture into buffer ---
                if frame_start - last_capture_time >= capture_interval:
                    if self._capture_into_buffer(page, frame_buffer):
                        frame_ready = True
                        last_capture_time = frame_start
                        # --- Save preview thumbnail ---
                        if frame_start - last_preview_time >= self._preview_interval:
                            self._save_preview(frame_buffer)
                            last_preview_time = frame_start

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
            self._teardown_playwright(pw, page, context, browser)
            self._destroy_ndi(ndi, ndi_send)

    def _idle_until_stopped(self):
        """Keep the process alive (with heartbeat) after an unrecoverable
        config error, so the watchdog doesn't restart-loop it every 5s."""
        while not self._stop_event.is_set():
            self._update_heartbeat()
            time.sleep(1.0)

    def _run_dummy_mode(self):
        """Fallback when NDI SDK is not available."""
        logger.warning(f"DUMMY MODE (no NDI): {self.ndi_name}")

        frame_buffer = self._alloc_frame_buffer()

        if self.source_type == "webcam":
            self._run_webcam_source(frame_buffer)
            return

        if self.source_type == "video":
            self._run_video_source(frame_buffer)
            return

        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        browser = context = page = None
        try:
            browser, context, page = self._launch_browser(pw)

            capture_interval = 1.0 / self.capture_fps
            last_refresh_time = time.monotonic()
            last_recycle_time = time.monotonic()
            last_preview_time = 0.0

            while not self._stop_event.is_set():
                now = time.monotonic()

                # Browser recycle + auto-refresh
                browser, context, page, last_recycle_time, last_refresh_time = \
                    self._recycle_and_refresh(
                        pw, browser, context, page, now,
                        last_recycle_time, last_refresh_time,
                    )

                if self._capture_into_buffer(page, frame_buffer):
                    if now - last_preview_time >= self._preview_interval:
                        self._save_preview(frame_buffer)
                        last_preview_time = now
                self._update_heartbeat()
                time.sleep(capture_interval)
        except Exception:
            logger.exception(f"Dummy worker crashed: {self.ndi_name}")
        finally:
            self._teardown_playwright(pw, page, context, browser)

    def stop(self):
        self._stop_event.set()


def worker_entry(worker: NDIWorker):
    """Multiprocessing entry point."""
    worker.run()
