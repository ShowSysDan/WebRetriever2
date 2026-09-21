"""
NDI Output Worker

Each instance runs in its own process:
  1. Launches headless Playwright browser (webpage/image/text sources)
     — or opens a V4L2 webcam via OpenCV (webcam source, no browser at all)
     — or decodes an uploaded video file via OpenCV/FFmpeg (video source,
       with play/stop/load control over shared mp.Values from the API
       process; `load` hot-swaps to a different file without restarting
       the worker or dropping the NDI stream)
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

# OpenCV's FFmpeg capture decodes single-threaded by default, which is what
# makes 4K sources stutter on otherwise capable machines. "threads;auto"
# lets FFmpeg spread software decode across cores. Read when a capture is
# opened, and inherited by worker child processes; setdefault so an operator
# can still override it from the environment.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "threads;auto")

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
VIDEO_CMD_LOAD = 3       # hot-swap to a new file, cue on its first frame
VIDEO_CMD_LOAD_PLAY = 4  # hot-swap to a new file and start playing it

# Video playback states (worker reports back through shared mp.Value)
VIDEO_STATE_STOPPED = 0
VIDEO_STATE_PLAYING = 1

# Hold-frame overrides (shared mp.Value; UNSET = keep current behavior)
VIDEO_HOLD_UNSET = 0
VIDEO_HOLD_FIRST = 1
VIDEO_HOLD_LAST = 2

# Size of the shared char buffer carrying file paths for load commands
VIDEO_PATH_MAX = 4096

# Signage playback commands — bit flags in a shared mp.Value so a skip and a
# reload landing in the same poll window can't clobber each other
SIGNAGE_CMD_SKIP = 1    # start transitioning to the next item now
SIGNAGE_CMD_RELOAD = 2  # re-read the playlist JSON from disk

# Build the NEXT signage item's layer this many seconds before its
# transition, on a background thread. Stills are fully decoded into a RAM
# canvas and videos get their file opened (first frame decoded, page cache
# warmed), so going on air never waits on the disk inside the send loop.
# Slots shorter than the lead just preload right after the previous
# transition — the busy/already-preloaded guard in preload_next() means at
# most ONE build ever runs per upcoming item, however often the loop asks.
SIGNAGE_PRELOAD_LEAD = 6.0

# RAM budget for decoded signage stills (MB). Canvases are kept across
# rotations, so a still that already played never touches the disk again.
SIGNAGE_STILL_CACHE_MB = int(os.getenv("SIGNAGE_STILL_CACHE_MB", "256"))

# Preview boost: while a live preview popup is streaming, the API keeps a
# shared monotonic deadline refreshed; until it passes, previews are saved
# larger and faster than the list-view thumbnails
PREVIEW_BOOST_WIDTH = 854      # px (16:9 → 854x480); clamped to output width
PREVIEW_BOOST_INTERVAL = 0.25  # seconds between saves while boosted (~4fps)

# NDI receiver stats: how often the send loops poll the SDK for the number
# of connected receivers and the tally state, into shared values the API
# reads. -1 in the connections value means "unknown" (dummy mode, or an
# ndi-python build without the call).
CONN_STATS_INTERVAL = 1.0
TALLY_PROGRAM = 1  # bit flags in the shared tally value
TALLY_PREVIEW = 2


def _load_signage_playlist(path):
    """Read the playlist JSON the API writes for this instance.

    Returns a list of item dicts (possibly empty). The file is written
    atomically (temp + rename) so a partial read can't happen; any other
    failure just means an empty playlist until the next reload."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
        return items if isinstance(items, list) else []
    except Exception as e:
        logger.warning(f"Cannot read signage playlist {path}: {e}")
        return []


def _warm_file_cache(path):
    """Hint the kernel to read a file into the page cache ahead of use.

    POSIX_FADV_WILLNEED starts asynchronous readahead — by the time the
    decoder wants the bytes they are (mostly) already in RAM, which turns
    the first seconds of playback from disk reads into memory copies. Purely
    advisory: any failure is ignored and playback just reads from disk."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_WILLNEED)
        finally:
            os.close(fd)
    except OSError:
        pass


def _signage_item_eligible(item, now=None):
    """Whether an item may play right now, per its baked-in schedule.

    Datetimes are naive ISO strings interpreted in the server's local
    timezone (signage schedules mean wall-clock time on the box). The daily
    window compares "HH:MM" strings; start > end wraps past midnight."""
    from datetime import datetime
    if item.get("_broken"):
        return False  # failed to open/decode earlier this run — don't retry every cycle
    if not item.get("enabled", True):
        return False
    if now is None:
        now = datetime.now()
    start = item.get("start_at")
    if start:
        try:
            if now < datetime.fromisoformat(start):
                return False
        except ValueError:
            pass
    end = item.get("end_at")
    if end:
        try:
            if now >= datetime.fromisoformat(end):
                return False
        except ValueError:
            pass
    ds, de = item.get("daily_start"), item.get("daily_end")
    if ds and de:
        cur = now.strftime("%H:%M")
        if ds <= de:
            if not (ds <= cur < de):
                return False
        else:  # overnight window, e.g. 22:00–06:00
            if not (cur >= ds or cur < de):
                return False
    return True


class _StillCache:
    """Decoded, letterboxed still canvases kept in RAM across rotations.

    A signage playlist replays the same files for hours; without this every
    rotation re-reads and re-decodes each image from disk. Canvases are
    keyed by (path, mtime, size) — replacing a file on disk naturally
    invalidates its entry — and evicted LRU once the byte budget is hit.
    Cached arrays are shared between plays and marked read-only: an image
    layer never writes its canvas after construction, so sharing is safe
    (and skips even the memcpy a fresh decode would need).

    Thread-safe: layers are built both inline (send loop) and by the
    preload thread.
    """

    def __init__(self, max_bytes):
        from collections import OrderedDict
        self._entries = OrderedDict()  # key -> canvas
        self._bytes = 0
        self._max = max_bytes
        self._lock = threading.Lock()

    @staticmethod
    def _key(path, out_w, out_h):
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (path, st.st_mtime_ns, st.st_size, out_w, out_h)

    def get(self, path, out_w, out_h):
        key = self._key(path, out_w, out_h)
        if key is None:
            return None
        with self._lock:
            buf = self._entries.get(key)
            if buf is not None:
                self._entries.move_to_end(key)
            return buf

    def put(self, path, out_w, out_h, buf):
        if buf.nbytes > self._max:
            return
        key = self._key(path, out_w, out_h)
        if key is None:
            return
        buf.setflags(write=False)
        with self._lock:
            if key in self._entries:
                return
            self._entries[key] = buf
            self._bytes += buf.nbytes
            while self._bytes > self._max and len(self._entries) > 1:
                _, old = self._entries.popitem(last=False)
                self._bytes -= old.nbytes


_STILL_CACHE = _StillCache(SIGNAGE_STILL_CACHE_MB * 1024 * 1024)


class _SignageLayer:
    """One playlist entry rendered onto its own (h, w, 3) BGR canvas.

    The signage loop keeps up to two of these alive at once — the item on
    air and, during a crossfade, the outgoing one — and alpha-blends their
    canvases into the shared BGRX frame buffer. item=None is the black idle
    layer (canvas stays zeroed) shown when nothing is scheduled.

    Still canvases come from / go into _STILL_CACHE, so an image that has
    played once lives in RAM and is never read from disk again (until the
    file changes or the cache budget evicts it).
    """

    def __init__(self, item, out_w, out_h):
        self.item = item or None
        self.buf = None
        self.ok = True
        self.done = False      # video reached end-of-file
        self.advanced = False  # frame() decoded a new video frame this call
        self._vf = None
        self._next_frame_time = None
        if item is None:
            self.buf = np.zeros((out_h, out_w, 3), dtype=np.uint8)
            return

        kind = item.get("kind")
        path = item.get("path", "")
        if kind == "video":
            self.buf = np.zeros((out_h, out_w, 3), dtype=np.uint8)
            self._vf = _VideoFile(path, out_w, out_h)
            first = self._vf.read() if self._vf.ok else None
            if first is None:
                self._vf.release()
                self._vf = None
                self.ok = False
                return
            # _VideoFile.blit writes [..., :3], which selects all three
            # channels of this BGR canvas — same letterbox math as the
            # video source's BGRX buffer
            self._vf.blit(first, self.buf)
        else:  # image
            cached = _STILL_CACHE.get(path, out_w, out_h)
            if cached is not None:
                # Shared read-only canvas — zero disk I/O, zero decode
                self.buf = cached
                return
            import cv2
            self.buf = np.zeros((out_h, out_w, 3), dtype=np.uint8)
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is None:
                self.ok = False
                return
            src_h, src_w = img.shape[:2]
            scale = min(out_w / src_w, out_h / src_h)
            fit_w = max(1, int(round(src_w * scale)))
            fit_h = max(1, int(round(src_h * scale)))
            if (fit_w, fit_h) != (src_w, src_h):
                img = cv2.resize(img, (fit_w, fit_h), interpolation=cv2.INTER_AREA)
            off_x = (out_w - fit_w) // 2
            off_y = (out_h - fit_h) // 2
            self.buf[off_y:off_y + fit_h, off_x:off_x + fit_w] = img
            _STILL_CACHE.put(path, out_w, out_h, self.buf)

    def start(self, now):
        """Begin playback clock (videos advance from their first frame)."""
        if self._vf is not None:
            self._next_frame_time = now + self._vf.frame_interval

    def frame(self, now):
        """Current canvas, advancing video playback at the file's native FPS."""
        self.advanced = False
        if self._vf is not None and not self.done and self._next_frame_time is not None:
            latest = None
            decode_budget = 8
            while now >= self._next_frame_time and decode_budget > 0:
                decode_budget -= 1
                f = self._vf.read()
                if f is None:
                    self.done = True
                    break
                latest = f
                self._next_frame_time += self._vf.frame_interval
            if latest is not None:
                self._vf.blit(latest, self.buf)
                self.advanced = True
            if not self.done and self._next_frame_time < now:
                # Fell behind (slow decode) — resync instead of spiraling
                self._next_frame_time = now + self._vf.frame_interval
        return self.buf

    def release(self):
        if self._vf is not None:
            self._vf.release()
            self._vf = None


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


class _VideoFile:
    """An open video file plus its letterbox geometry for a fixed output size.

    Bundling the capture handle with its per-file state (native fps, fit
    rectangle, resize buffer) is what makes hot-swapping files cheap: a
    `load` command just constructs a new _VideoFile and releases the old
    one — no worker restart, no NDI sender teardown.

    Letterbox: scale to fit inside the output resolution while preserving
    aspect ratio, centered on black. Computed once per file — the frame size
    never changes mid-stream.
    """

    def __init__(self, path: str, out_w: int, out_h: int):
        import cv2
        self._cv2 = cv2
        self.path = path
        # Inert defaults so a failed open still leaves a usable (no-op)
        # object — read() returns None and the timing attributes exist
        self.fps = 30.0
        self.frame_interval = 1.0 / 30.0
        self.src_w, self.src_h = out_w, out_h
        self.fit_w, self.fit_h = out_w, out_h
        self.off_x = self.off_y = 0
        self._needs_resize = False
        self._resize_buf = None

        # Ask for hardware-accelerated decode (VAAPI/QSV/NVDEC/...) when the
        # machine has it — VIDEO_ACCELERATION_ANY falls back to software
        # decoding internally, and we fall back to a plain open if this
        # OpenCV build predates the API entirely
        self.cap = None
        try:
            self.cap = cv2.VideoCapture(
                path, cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY],
            )
        except Exception:
            self.cap = None
        if self.cap is None or not self.cap.isOpened():
            if self.cap is not None:
                self.cap.release()
            self.cap = cv2.VideoCapture(path)
        self.ok = self.cap.isOpened()
        if not self.ok:
            self.cap.release()
            return

        fps = self.cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps < 1 or fps > 240:
            fps = 30.0
        self.fps = fps
        self.frame_interval = 1.0 / fps

        src_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or out_w
        src_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or out_h
        self.src_w, self.src_h = src_w, src_h
        scale = min(out_w / src_w, out_h / src_h)
        self.fit_w = max(1, int(round(src_w * scale)))
        self.fit_h = max(1, int(round(src_h * scale)))
        self.off_x = (out_w - self.fit_w) // 2
        self.off_y = (out_h - self.fit_h) // 2
        self._needs_resize = (self.fit_w, self.fit_h) != (src_w, src_h)
        self._resize_buf = (
            np.empty((self.fit_h, self.fit_w, 3), dtype=np.uint8)
            if self._needs_resize else None
        )

    def read(self):
        """Next decoded BGR frame, or None at end-of-file/decode error."""
        ok, frame = self.cap.read()
        return frame if ok and frame is not None else None

    def rewind(self):
        self.cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)

    def blit(self, bgr, frame_buffer: np.ndarray):
        """Write a decoded frame into the fitted region of the BGRX buffer."""
        if self._needs_resize:
            self._cv2.resize(bgr, (self.fit_w, self.fit_h), dst=self._resize_buf)
            bgr = self._resize_buf
        frame_buffer[self.off_y:self.off_y + self.fit_h,
                     self.off_x:self.off_x + self.fit_w, :3] = bgr

    def release(self):
        try:
            self.cap.release()
        except Exception:
            pass


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
        signage_settings: Optional[dict] = None,
        heartbeat: Optional[mp.Value] = None,
        video_cmd: Optional[mp.Value] = None,
        video_state: Optional[mp.Value] = None,
        video_path: Optional[mp.Array] = None,
        video_hold: Optional[mp.Value] = None,
        signage_cmd: Optional[mp.Value] = None,
        preview_boost: Optional[mp.Value] = None,
        ndi_connections: Optional[mp.Value] = None,
        ndi_tally: Optional[mp.Value] = None,
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
        self.signage_settings = signage_settings or {}
        self._stop_event = mp.Event()
        self._heartbeat = heartbeat  # shared with parent process
        self._video_cmd = video_cmd  # play/stop/load commands from the API process
        self._video_state = video_state  # playback state reported to the API process
        self._video_path = video_path  # file path payload for load commands
        self._video_hold = video_hold  # per-command hold-frame override
        self._signage_cmd = signage_cmd  # skip/reload bit flags from the API process
        self._preview_boost = preview_boost  # monotonic deadline: HD previews until then
        self._ndi_connections = ndi_connections  # receiver count reported to the API
        self._ndi_tally = ndi_tally  # program/preview tally bits reported to the API
        self._preview_dir = preview_dir
        self._preview_interval = preview_interval
        self._last_conn_poll = 0.0

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
        try:
            context = browser.new_context(
                viewport={"width": self.width, "height": self.height},
                device_scale_factor=1,
            )
            page = context.new_page()
        except Exception:
            # Don't leak a live Chromium if context/page creation fails —
            # close it before propagating so the caller's error path never
            # leaves a browser without an owner
            try:
                browser.close()
            except Exception:
                pass
            raise

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
    # NDI receiver stats (connection count + tally)
    # ------------------------------------------------------------------

    def _update_conn_stats(self, ndi, ndi_send, now: float):
        """Poll the SDK for connected-receiver count and tally into shared
        values (rate-limited to CONN_STATS_INTERVAL).

        send_get_no_connections counts every receiver holding a connection
        to this sender — each NDI receiver keeps a reliable control/metadata
        connection open even when the video itself travels over UDP or
        multicast, so the count covers all transport modes. Tally reflects
        what downstream switchers report back (on program / on preview).
        Both calls are non-blocking (timeout 0) and wrapped defensively so
        an ndi-python build without them just leaves the values at
        "unknown" instead of taking down the send loop."""
        if ndi is None or self._ndi_connections is None:
            return
        if now - self._last_conn_poll < CONN_STATS_INTERVAL:
            return
        self._last_conn_poll = now
        try:
            self._ndi_connections.value = int(ndi.send_get_no_connections(ndi_send, 0))
        except Exception:
            pass
        if self._ndi_tally is None:
            return
        try:
            tally = ndi.Tally()
            ndi.send_get_tally(ndi_send, tally, 0)
            self._ndi_tally.value = (
                (TALLY_PROGRAM if getattr(tally, "on_program", False) else 0)
                | (TALLY_PREVIEW if getattr(tally, "on_preview", False) else 0)
            )
        except Exception:
            pass

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

    def _preview_params(self, now: float):
        """(save_interval, hd) — while a preview popup is streaming, the API
        keeps the shared boost deadline ahead of now and previews are saved
        larger and faster; otherwise the cheap list-view thumbnail cadence."""
        if self._preview_boost is not None and now < self._preview_boost.value:
            return PREVIEW_BOOST_INTERVAL, True
        return self._preview_interval, False

    def _save_preview(self, frame_buffer: np.ndarray, hd: bool = False):
        """Save a JPEG preview from the current frame buffer.

        hd=True writes a larger frame for the live preview popup stream;
        the default is a small thumbnail for the instance list."""
        if not self._preview_dir:
            return
        try:
            # BGRX → RGB as a reversed-stride view (no copy); Pillow
            # materializes on resize/save so the zero-copy view is safe.
            rgb_view = frame_buffer[:, :, 2::-1]
            img = Image.fromarray(rgb_view, "RGB")
            # Downscale, maintaining aspect ratio
            thumb_w = min(PREVIEW_BOOST_WIDTH, self.width) if hd else 320
            thumb_h = max(1, int(self.height * (thumb_w / self.width)))
            img = img.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            # Atomic write: temp file then replace
            dest = os.path.join(self._preview_dir, f"{self.instance_id}.jpg")
            fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=self._preview_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    img.save(f, "JPEG", quality=70 if hd else 60)
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
                        pv_interval, pv_hd = self._preview_params(frame_start)
                        if frame_start - last_preview_time >= pv_interval:
                            self._save_preview(frame_buffer, hd=pv_hd)
                            last_preview_time = frame_start

                # --- Send to NDI (duplicates last frame up to output_fps) ---
                if frame_ready and ndi is not None:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)

                self._update_conn_stats(ndi, ndi_send, frame_start)
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

    def _poll_video_cmd(self):
        """Read and clear the pending playback command (edge-triggered).

        Returns (cmd, path, hold): `path` is only set for load commands,
        `hold` is "first"/"last" when the command carried an override, else
        None. The cmd Value's lock serializes the whole channel — the
        manager writes path/hold before setting cmd under the same lock.
        """
        if self._video_cmd is None:
            return VIDEO_CMD_NONE, None, None
        with self._video_cmd.get_lock():
            cmd = self._video_cmd.value
            self._video_cmd.value = VIDEO_CMD_NONE
            path = None
            if cmd in (VIDEO_CMD_LOAD, VIDEO_CMD_LOAD_PLAY) and self._video_path is not None:
                path = self._video_path.get_obj().value.decode("utf-8", "replace") or None
            hold = None
            if self._video_hold is not None:
                hv = self._video_hold.value
                self._video_hold.value = VIDEO_HOLD_UNSET
                hold = {VIDEO_HOLD_FIRST: "first", VIDEO_HOLD_LAST: "last"}.get(hv)
        return cmd, path, hold

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
          - `load` / `load_play` hot-swap to a different file without
            restarting the worker: the new file is opened and verified
            first, then swapped in atomically — the old frame stays on air
            until the new file's first frame replaces it, so switching is
            near-instant and the NDI stream never drops. `load` cues the
            new file on its first frame (pre-loaded for an instant later
            `play`); `load_play` starts it immediately.
          - Commands may carry a hold override ("first"/"last") which
            replaces the configured hold for the rest of the run.
          - When a play-once video reaches the end it stops and holds per
            video_hold; in loop mode it seeks back to frame 0 and continues.
          - While stopped, the held frame keeps streaming so receivers never
            lose the source.
        """
        vid = _VideoFile(self.source_value, self.width, self.height)
        if not vid.ok:
            logger.error(f"Cannot open video file for '{self.ndi_name}': {self.source_value}")
            self._idle_until_stopped()
            return

        output_interval = 1.0 / self.output_fps

        loop_playback = bool(self.video_settings.get("loop", False))
        hold = self.video_settings.get("hold", "last")
        playing = bool(self.video_settings.get("autoplay", False))

        # BGRX buffer starts zeroed (black) with X=255, so the letterbox bars
        # are already in place — only the fitted region is ever written.
        frame_dirty = True  # first frame needs an initial preview save

        def show_first_frame():
            """Seek to frame 0 and put it on the buffer. Position is left at
            frame 1, so a subsequent decode continues without re-reading."""
            nonlocal frame_dirty
            vid.rewind()
            frame = vid.read()
            if frame is None:
                return False
            vid.blit(frame, frame_buffer)
            frame_dirty = True
            return True

        def swap_file(new_path):
            """Open a new file and swap it in; on any failure the current
            file keeps playing/holding untouched."""
            nonlocal vid, frame_dirty
            new = _VideoFile(new_path, self.width, self.height)
            first = new.read() if new.ok else None
            if first is None:
                new.release()
                logger.warning(
                    f"Load rejected (unreadable file) for '{self.ndi_name}': {new_path}"
                )
                return False
            old = vid
            vid = new
            old.release()
            # The new file's letterbox geometry may differ — blank the buffer
            # so stale pixels outside the new fit region can't linger
            frame_buffer[:, :, :3] = 0
            vid.blit(first, frame_buffer)
            frame_dirty = True
            logger.info(f"Video loaded: {self.ndi_name} | file={new_path} | "
                        f"{vid.src_w}x{vid.src_h}@{vid.fps:.2f}fps")
            return True

        # Show the first frame immediately so the NDI source is never blank
        if not show_first_frame():
            logger.error(f"Cannot decode video file for '{self.ndi_name}': {vid.path}")
            vid.release()
            self._idle_until_stopped()
            return
        if not playing:
            vid.rewind()

        logger.info(
            f"Video worker started: {self.ndi_name} | file={vid.path} | "
            f"{vid.src_w}x{vid.src_h}@{vid.fps:.2f}fps → {self.width}x{self.height} | "
            f"loop={loop_playback}, hold={hold}, autoplay={playing}, "
            f"output={self.output_fps}fps"
        )
        self._update_heartbeat()
        self._report_video_state(playing)

        next_frame_time = time.monotonic()
        last_preview_time = 0.0
        last_pv_hd = False

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()

                # --- Apply pending play/stop/load command ---
                cmd, new_path, hold_override = self._poll_video_cmd()
                if hold_override:
                    hold = hold_override
                if cmd in (VIDEO_CMD_LOAD, VIDEO_CMD_LOAD_PLAY) and new_path:
                    if swap_file(new_path):
                        playing = (cmd == VIDEO_CMD_LOAD_PLAY)
                        next_frame_time = now + vid.frame_interval
                    self._report_video_state(playing)
                elif cmd == VIDEO_CMD_PLAY:
                    if show_first_frame():
                        playing = True
                        next_frame_time = now + vid.frame_interval
                    self._report_video_state(playing)
                elif cmd == VIDEO_CMD_STOP:
                    playing = False
                    if hold == "first":
                        show_first_frame()
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
                        frame = vid.read()
                        if frame is not None:
                            latest = frame
                            next_frame_time += vid.frame_interval
                        else:
                            # End of file (or decode error mid-file)
                            if loop_playback:
                                if not show_first_frame():
                                    logger.warning(
                                        f"Video loop restart failed, reopening: {vid.path}"
                                    )
                                    reopen_path = vid.path
                                    vid.release()
                                    vid = _VideoFile(reopen_path, self.width, self.height)
                                    if not (vid.ok and show_first_frame()):
                                        # File vanished or became undecodable —
                                        # stop and hold the last good frame
                                        # instead of spinning reopen attempts
                                        logger.error(
                                            f"Video file unreadable, stopping "
                                            f"playback: {reopen_path}"
                                        )
                                        playing = False
                                        self._report_video_state(playing)
                                latest = None  # show_first_frame already blitted
                                next_frame_time = now + vid.frame_interval
                            else:
                                playing = False
                                if hold == "first":
                                    show_first_frame()
                                    latest = None
                                self._report_video_state(playing)
                                logger.info(f"Video finished (hold={hold}): {self.ndi_name}")
                    if latest is not None:
                        vid.blit(latest, frame_buffer)
                        frame_dirty = True
                    if playing and next_frame_time < now:
                        next_frame_time = now + vid.frame_interval

                # --- Send to NDI (held frame keeps streaming while stopped) ---
                if ndi is not None:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)
                self._update_conn_stats(ndi, ndi_send, now)

                # Only write a preview when the frame actually changed —
                # while holding, rewriting an identical JPEG every 2s just
                # wears the disk for nothing. A boost-state flip forces one
                # write so a popup opened on a held frame still upgrades to HD.
                pv_interval, pv_hd = self._preview_params(now)
                if ((frame_dirty or pv_hd != last_pv_hd)
                        and now - last_preview_time >= pv_interval):
                    self._save_preview(frame_buffer, hd=pv_hd)
                    last_preview_time = now
                    last_pv_hd = pv_hd
                    frame_dirty = False

                self._update_heartbeat()

                # --- Pace to output FPS ---
                sleep_time = (now + output_interval) - time.monotonic()
                if sleep_time > 0.001:
                    time.sleep(sleep_time)
        finally:
            vid.release()
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
    # Signage playlist loop
    # ------------------------------------------------------------------

    def _poll_signage_cmd(self) -> int:
        """Read and clear pending signage command bits (skip / reload)."""
        if self._signage_cmd is None:
            return 0
        with self._signage_cmd.get_lock():
            cmds = self._signage_cmd.value
            self._signage_cmd.value = 0
        return cmds

    def _write_signage_status(self, path, current_item, remaining_s, next_item):
        """Atomically write the now-playing status JSON the API serves."""
        if not path:
            return
        import json

        def brief(it):
            if not it:
                return None
            return {"id": it.get("id"), "name": it.get("name"),
                    "media_id": it.get("media_id"), "kind": it.get("kind")}

        try:
            state_dir = os.path.dirname(path)
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=state_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({
                        "current": brief(current_item),
                        "remaining_s": round(max(0.0, remaining_s), 1) if current_item else None,
                        "next": brief(next_item),
                        "ts": time.time(),
                    }, f)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.debug(f"Signage status write failed for {self.ndi_name}: {e}")

    def _run_signage_loop(self, frame_buffer, ndi=None, ndi_send=None, video_frame=None):
        """Play a scheduled playlist of stills and videos with crossfades.

        Pass ndi=None to run in dummy mode (preview thumbnails only).

        Playback model:
          - The playlist JSON (written by the API, schedule/timing already
            resolved per item) defines play order. Items outside their
            schedule window are skipped; when nothing is eligible the output
            fades to black and re-checks every 0.5s.
          - Each item is on air for its `duration` seconds measured from its
            first visible frame. The crossfade into the NEXT item starts
            `crossfade` seconds before that slot ends — so for a video the
            transition begins before the file ends, exactly at
            (duration - crossfade). crossfade=0 is a hard cut.
          - The incoming layer starts playing (videos advance) as soon as
            the fade begins; both layers are alpha-blended per output frame.
          - A `skip` command starts the transition to the next item now; a
            `reload` command re-reads the playlist without dropping the
            stream (the current item keeps playing, its timing refreshed).
          - An impression line is appended for an item the moment it first
            becomes visible (fade-in start).
          - A video whose duration wasn't explicitly set fades out when the
            file actually ends, even if metadata said otherwise; with an
            explicit duration longer than the file it holds its last frame
            until the slot ends.
          - The upcoming item is preloaded on a background thread a few
            seconds before its transition (SIGNAGE_PRELOAD_LEAD): stills are
            decoded into a RAM canvas, videos are opened with their first
            frame decoded and the file read into the OS page cache — so the
            transition itself never blocks the send loop on disk I/O.
        """
        import cv2
        from datetime import datetime

        st = self.signage_settings
        playlist_path = st.get("playlist_path")
        status_path = st.get("status_path")
        impressions_path = st.get("impressions_path")

        output_interval = 1.0 / self.output_fps
        blend_buf = np.empty((self.height, self.width, 3), dtype=np.uint8)

        playlist = _load_signage_playlist(playlist_path) if playlist_path else []
        logger.info(
            f"Signage worker started: {self.ndi_name} | {len(playlist)} item(s) | "
            f"{self.width}x{self.height} | output={self.output_fps}fps"
        )

        current = _SignageLayer(None, self.width, self.height)  # start on black
        cur_index = -1
        cur_start = time.monotonic()
        cur_fade = 0.0            # outgoing crossfade of the current item
        fade_start_t = 0.0        # when the next transition should begin (0 = ASAP)
        fading_from = None        # outgoing layer during a crossfade
        fade_t0 = 0.0
        fade_dur = 0.0
        buffer_synced = False     # steady-state still already copied to frame_buffer
        frame_dirty = True
        idle_recheck_t = 0.0
        last_status_t = 0.0
        last_preview_t = 0.0
        last_pv_hd = False

        def log_impression(item):
            if not impressions_path or not item:
                return
            try:
                # Open/append/close per event so the API can atomically
                # rotate the log without losing later writes
                with open(impressions_path, "a", encoding="utf-8") as f:
                    f.write(f"{item['id']}\n")
            except OSError:
                pass

        def slot_times(item, start_t):
            """(fade_start, fade_len) for an item that went on air at start_t.

            start_t is when the item FIRST became visible (its fade-in
            began). The anchors differ by kind:
              - video: the outgoing fade overlaps the file's tail, starting
                at (duration - fade) so the picture is still moving as it
                dissolves and the file ends as the fade completes.
              - still: duration is time on screen BEFORE the outgoing fade
                begins — "5s with a 3s fade" = 3s fade-in (overlapping the
                previous item), ~2s clean hold, then the 3s fade-out. If
                the fade started at duration - fade like video, a fade
                longer than the remaining solo time would chain fade-in
                straight into fade-out and the still would never hold.
            """
            dur = float(item.get("duration") or 8.0)
            dur = max(0.5, dur)
            fade = max(0.0, float(item.get("crossfade") or 0.0))
            if item.get("kind") == "video":
                fade = min(fade, max(0.0, dur - 0.1))
                return start_t + dur - fade, fade
            return start_t + dur, fade

        def pick_next(from_index):
            """Next eligible (index, item) after from_index, wrapping; (-1, None) if none."""
            if not playlist:
                return -1, None
            n = len(playlist)
            now_dt = datetime.now()
            for step in range(1, n + 1):
                j = (from_index + step) % n
                if _signage_item_eligible(playlist[j], now_dt):
                    return j, playlist[j]
            return -1, None

        # --- Preload: build the upcoming item's layer off the send loop ---
        # Stills decode straight into a RAM canvas; videos open their file
        # (first frame decoded) with the page cache warmed. The layer is
        # handed over at transition time only if the scheduled next item is
        # still the one that was preloaded — otherwise it's rebuilt inline
        # exactly as before.
        preload_lock = threading.Lock()
        preload_layer = None
        preload_id = None
        preload_busy = False

        def _preload_worker(item):
            nonlocal preload_layer, preload_id, preload_busy
            layer = None
            try:
                if item.get("kind") == "video":
                    _warm_file_cache(item.get("path", ""))
                layer = _SignageLayer(item, self.width, self.height)
            except Exception:
                layer = None
            with preload_lock:
                if layer is not None and layer.ok:
                    if preload_layer is not None:
                        preload_layer.release()
                    preload_layer = layer
                    preload_id = item.get("id")
                elif layer is not None:
                    layer.release()
                preload_busy = False

        def preload_next():
            """Kick off a background build of the next eligible item's layer."""
            nonlocal preload_busy
            _, item = pick_next(cur_index)
            if item is None:
                return
            with preload_lock:
                if preload_busy or (preload_layer is not None
                                    and preload_id == item.get("id")):
                    return
                preload_busy = True
            threading.Thread(target=_preload_worker, args=(item,),
                             daemon=True, name="signage-preload").start()

        def take_preloaded(item):
            """Pop the preloaded layer if it matches `item`, else None."""
            nonlocal preload_layer, preload_id
            with preload_lock:
                if (preload_layer is not None and item is not None
                        and preload_id == item.get("id")):
                    layer = preload_layer
                    preload_layer = None
                    preload_id = None
                    return layer
            return None

        def drop_preloaded():
            """Release a stale preloaded layer (playlist edited, shutdown)."""
            nonlocal preload_layer, preload_id
            with preload_lock:
                if preload_layer is not None:
                    preload_layer.release()
                preload_layer = None
                preload_id = None

        def begin_transition(now):
            """Fade from `current` to the next eligible item (or to black)."""
            nonlocal current, cur_index, cur_start, cur_fade, fade_start_t
            nonlocal fading_from, fade_t0, fade_dur, buffer_synced, frame_dirty
            nonlocal last_status_t

            j, item = pick_next(cur_index)
            layer = take_preloaded(item)
            attempts = 0
            while item is not None and attempts < len(playlist):
                if layer is None:
                    layer = _SignageLayer(item, self.width, self.height)
                if layer.ok:
                    break
                logger.warning(
                    f"Signage item unreadable, skipping: {item.get('path')} "
                    f"({self.ndi_name})"
                )
                layer.release()
                layer = None
                item["_broken"] = True
                j, item = pick_next(j)
                attempts += 1

            if item is None:
                # Going to black: a stale preloaded layer (schedule changed
                # since it was built) must not idle holding a file handle
                drop_preloaded()
                if current.item is None:
                    fade_start_t = float("inf")  # already black, stay put
                    return
                layer = _SignageLayer(None, self.width, self.height)

            # Outgoing item's crossfade paces the transition; fading in from
            # black uses the incoming item's own crossfade instead
            if current.item is not None:
                fade = cur_fade
            elif item is not None:
                fade = max(0.0, float(item.get("crossfade") or 0.0))
            else:
                fade = 0.0

            layer.start(now)
            log_impression(item)
            old = current
            current = layer
            cur_index = j if item is not None else cur_index
            cur_start = now
            if item is not None:
                fade_start_t, cur_fade = slot_times(item, now)
            else:
                fade_start_t = float("inf")
                cur_fade = 0.0
            buffer_synced = False
            frame_dirty = True

            if fade > 0.02:
                fading_from = old
                fade_t0 = now
                fade_dur = fade
            else:
                old.release()
                fading_from = None
            # Status file updates this same iteration — the event stream
            # pushes item changes to browsers in real time
            last_status_t = 0.0

        self._update_heartbeat()

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()

                # --- Commands ---
                cmds = self._poll_signage_cmd()
                if cmds & SIGNAGE_CMD_RELOAD:
                    playlist = _load_signage_playlist(playlist_path) if playlist_path else []
                    drop_preloaded()  # baked-in timing/schedule may have changed
                    if current.item is not None:
                        cur_id = current.item.get("id")
                        cur_index = next(
                            (i for i, it in enumerate(playlist) if it.get("id") == cur_id), -1
                        )
                        if cur_index >= 0:
                            # Keep the item on air but adopt its (possibly
                            # edited) timing, measured from when it appeared
                            current.item = playlist[cur_index]
                            fade_start_t, cur_fade = slot_times(current.item, cur_start)
                        else:
                            fade_start_t = now  # removed from playlist — move on
                    else:
                        idle_recheck_t = 0.0  # black: look for content immediately
                    logger.info(f"Signage playlist reloaded: {self.ndi_name} "
                                f"({len(playlist)} items)")
                if (cmds & SIGNAGE_CMD_SKIP) and fading_from is None:
                    if current.item is not None:
                        fade_start_t = now
                    else:
                        idle_recheck_t = 0.0

                # --- Finish an active crossfade ---
                if fading_from is not None and now >= fade_t0 + fade_dur:
                    fading_from.release()
                    fading_from = None
                    buffer_synced = False

                # --- Video without explicit duration finished → move on now ---
                if (fading_from is None and current.done
                        and not (current.item or {}).get("duration_explicit")
                        and now < fade_start_t):
                    fade_start_t = now

                # --- Preload the upcoming item ahead of its transition ---
                if (fading_from is None and current.item is not None
                        and now >= fade_start_t - SIGNAGE_PRELOAD_LEAD):
                    preload_next()

                # --- Start next transition / leave idle ---
                if fading_from is None:
                    if current.item is None:
                        if now >= idle_recheck_t:
                            idle_recheck_t = now + 0.5
                            begin_transition(now)
                    elif now >= fade_start_t:
                        begin_transition(now)

                # --- Render ---
                if fading_from is not None:
                    a = min(1.0, (now - fade_t0) / fade_dur) if fade_dur > 0 else 1.0
                    src_out = fading_from.frame(now)
                    src_in = current.frame(now)
                    cv2.addWeighted(src_out, 1.0 - a, src_in, a, 0.0, dst=blend_buf)
                    frame_buffer[:, :, :3] = blend_buf
                    frame_dirty = True
                else:
                    buf = current.frame(now)
                    if current.advanced or not buffer_synced:
                        frame_buffer[:, :, :3] = buf
                        buffer_synced = True
                        frame_dirty = True

                # --- Send to NDI ---
                if ndi is not None:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)
                self._update_conn_stats(ndi, ndi_send, now)

                # --- Once a second: expire check + status file ---
                if now - last_status_t >= 1.0:
                    last_status_t = now
                    if (current.item is not None and fading_from is None
                            and not _signage_item_eligible(current.item)):
                        # Schedule window closed mid-slot — transition out now
                        fade_start_t = min(fade_start_t, now)
                    _, next_item = pick_next(cur_index)
                    remaining = (fade_start_t + cur_fade - now) if current.item else 0.0
                    self._write_signage_status(
                        status_path, current.item, remaining, next_item
                    )

                # --- Preview (only when the frame changed, or on boost flip
                # so a popup opened on a static still upgrades to HD) ---
                pv_interval, pv_hd = self._preview_params(now)
                if ((frame_dirty or pv_hd != last_pv_hd)
                        and now - last_preview_t >= pv_interval):
                    self._save_preview(frame_buffer, hd=pv_hd)
                    last_preview_t = now
                    last_pv_hd = pv_hd
                    frame_dirty = False

                self._update_heartbeat()

                # --- Pace to output FPS ---
                sleep_time = (now + output_interval) - time.monotonic()
                if sleep_time > 0.001:
                    time.sleep(sleep_time)
        finally:
            drop_preloaded()
            if fading_from is not None:
                fading_from.release()
            current.release()

    def _run_signage_source(self, frame_buffer, ndi=None, ndi_send=None, video_frame=None):
        """Run the signage loop with shared error handling (real or dummy mode)."""
        try:
            self._run_signage_loop(
                frame_buffer, ndi=ndi, ndi_send=ndi_send, video_frame=video_frame
            )
        except ImportError:
            logger.error(
                "opencv-python-headless not installed — signage source "
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

        # --- Signage source: scheduled playlist with crossfades, no browser ---
        if self.source_type == "signage":
            try:
                self._run_signage_source(
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
                        pv_interval, pv_hd = self._preview_params(frame_start)
                        if frame_start - last_preview_time >= pv_interval:
                            self._save_preview(frame_buffer, hd=pv_hd)
                            last_preview_time = frame_start

                # --- Send to NDI ---
                if frame_ready:
                    video_frame.data = frame_buffer
                    ndi.send_send_video_v2(ndi_send, video_frame)
                    self._update_heartbeat()
                self._update_conn_stats(ndi, ndi_send, frame_start)

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

        if self.source_type == "signage":
            self._run_signage_source(frame_buffer)
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
                    pv_interval, pv_hd = self._preview_params(now)
                    if now - last_preview_time >= pv_interval:
                        self._save_preview(frame_buffer, hd=pv_hd)
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
    """Multiprocessing entry point.

    The worker becomes its own process-group leader so the manager can kill
    the ENTIRE tree (Playwright driver, Chromium and its helpers) with one
    killpg if the worker hangs and has to be force-killed. Without this, a
    SIGKILL to a hung worker orphans the Chromium tree — hundreds of MB per
    occurrence that never get reclaimed."""
    if hasattr(os, "setpgrp"):
        try:
            os.setpgrp()
        except OSError:
            pass
    worker.run()
