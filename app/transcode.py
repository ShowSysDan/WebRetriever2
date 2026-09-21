"""
Background video optimizer.

4K (and other oversized) uploads choke playback: the NDI workers CPU-decode
every frame at native resolution and then downscale it per frame. This
module transcodes such videos once, in the background, into an H.264
playback copy sized for the outputs (default 1920x1080 box). Workers decode
the copy; the original upload stays on disk untouched.

Design:
  - One queue + one worker thread: ffmpeg saturates cores, and playback
    shares this machine, so transcodes run strictly one at a time and are
    started under `nice` so live outputs keep priority.
  - Status lives on the MediaFile row (pending/processing/done/failed/
    skipped) so the UI can show progress and the queue survives inspection.
  - Jobs found stuck in pending/processing at boot (crash/restart mid-job)
    are re-queued automatically.
  - When a transcode finishes, signage playlists using the file are
    rewritten + hot-reloaded so running players switch to the light copy at
    their next item transition; plain video instances pick it up on their
    next start/play/load (those resolve the path per command).
"""

import os
import queue
import shutil
import logging
import threading
import subprocess

logger = logging.getLogger(__name__)

# Safety net for runaway encodes: generous (a long movie at veryfast still
# fits), but bounded so a wedged ffmpeg can't hold the queue forever
TRANSCODE_TIMEOUT_S = 4 * 3600


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_video(path):
    """First video stream's {codec_name, pix_fmt, width, height} via
    ffprobe, or None when ffprobe is missing or the probe fails."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt,width,height",
             "-of", "json", path],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            return None
        import json
        streams = json.loads(result.stdout or b"{}").get("streams") or []
        return streams[0] if streams else None
    except Exception:
        return None


# The "perfect" playback format: cheapest mainstream decode, sized for the
# outputs. Files already matching are marked playback-ready, never re-encoded.
_READY_CONTAINERS = {"mp4", "m4v", "mov"}  # same MP4-family demux path
_READY_PIX_FMTS = {"yuv420p", "yuvj420p"}


def is_playback_ready(media, path, target_w, target_h) -> bool:
    """Whether a video already matches the ideal playback format:
    H.264 + 4:2:0 in an MP4-family container, within the target box."""
    ext = media.filename.rsplit(".", 1)[-1].lower() if "." in media.filename else ""
    if ext not in _READY_CONTAINERS:
        return False
    info = probe_video(path)
    if info is None:
        # No ffprobe / unreadable — fall back to a resolution-only check so
        # a box without ffmpeg still avoids queueing everything as pending
        return ((media.width_px or 0) <= target_w
                and (media.height_px or 0) <= target_h)
    if info.get("codec_name") != "h264":
        return False
    if info.get("pix_fmt") not in _READY_PIX_FMTS:
        return False
    w, h = info.get("width") or 0, info.get("height") or 0
    return 0 < w <= target_w and 0 < h <= target_h


class Transcoder:
    def __init__(self):
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._thread = None
        self._app = None
        self._lock = threading.Lock()
        self.active_id = None  # media id currently being transcoded

    def init_app(self, app):
        """Bind to the Flask app, re-queue jobs interrupted by a restart,
        and — in "all" mode — sweep the library for videos that were never
        checked (uploaded before this feature, or while it was off)."""
        self._app = app
        from app.models import db, MediaFile
        with app.app_context():
            stuck = MediaFile.query.filter(
                MediaFile.optimize_status.in_(["pending", "processing"])
            ).all()
            for media in stuck:
                media.optimize_status = "pending"
            if stuck:
                db.session.commit()
                logger.info(f"Re-queueing {len(stuck)} interrupted transcode(s)")
                for media in stuck:
                    self._queue.put(media.id)
                self._ensure_thread()

            if app.config.get("VIDEO_OPTIMIZE") == "all":
                unchecked = [
                    m for m in MediaFile.query.filter(
                        MediaFile.optimize_status.is_(None)).all()
                    if m.is_video
                ]
                if unchecked:
                    logger.info(
                        f"Video optimize sweep: checking {len(unchecked)} "
                        f"existing video(s) for playback format"
                    )
                    for media in unchecked:
                        self.enqueue(media.id)
                    db.session.commit()

    def enqueue(self, media_id: int) -> str:
        """Queue a media file for optimization. Returns the resulting status
        ("pending", or "skipped" when ffmpeg isn't installed). The caller
        must be inside an app context and commit the session afterwards."""
        from flask import current_app
        from app.models import db, MediaFile
        media = db.session.get(MediaFile, media_id)
        if media is None:
            return "skipped"
        if not ffmpeg_available():
            # No converter available — still classify: a file already within
            # the box in an MP4-family container plays fine as-is
            upload_dir = current_app.config["UPLOAD_FOLDER"]
            tw = current_app.config.get("VIDEO_TARGET_WIDTH", 1920)
            th = current_app.config.get("VIDEO_TARGET_HEIGHT", 1080)
            if is_playback_ready(media, os.path.join(upload_dir, media.filename), tw, th):
                media.optimize_status = "native"
                return "native"
            media.optimize_status = "skipped"
            logger.warning(
                "ffmpeg not installed — cannot optimize video "
                f"'{media.original_name}'. Install it (apt install ffmpeg) "
                "for smooth playback."
            )
            return "skipped"
        media.optimize_status = "pending"
        self._queue.put(media.id)
        self._ensure_thread()
        return "pending"

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="video-transcoder"
                )
                self._thread.start()

    # ------------------------------------------------------------------

    def _run(self):
        while True:
            media_id = self._queue.get()
            self.active_id = media_id
            try:
                with self._app.app_context():
                    self._transcode_one(media_id)
            except Exception:
                logger.exception(f"Transcoder crashed on media {media_id}")
                try:
                    with self._app.app_context():
                        self._set_status(media_id, "failed")
                except Exception:
                    pass
            finally:
                self.active_id = None
                self._queue.task_done()

    def _set_status(self, media_id, status):
        from app.models import db, MediaFile
        media = db.session.get(MediaFile, media_id)
        if media is not None:
            media.optimize_status = status
            db.session.commit()

    def _transcode_one(self, media_id):
        from flask import current_app
        from app.models import db, MediaFile
        from app.logging_config import log_event

        media = db.session.get(MediaFile, media_id)
        if media is None:
            return
        upload_dir = current_app.config["UPLOAD_FOLDER"]
        src = os.path.join(upload_dir, media.filename)
        if not os.path.exists(src):
            media.optimize_status = "failed"
            db.session.commit()
            return

        tw = current_app.config.get("VIDEO_TARGET_WIDTH", 1920)
        th = current_app.config.get("VIDEO_TARGET_HEIGHT", 1080)
        crf = current_app.config.get("VIDEO_CRF", 20)
        preset = current_app.config.get("VIDEO_PRESET", "veryfast")

        # Already the perfect playback format? Mark it ready — no re-encode
        if is_playback_ready(media, src, tw, th):
            media.optimize_status = "native"
            db.session.commit()
            logger.info(f"Video already playback-ready, not converting: "
                        f"'{media.original_name}'")
            return

        # Compute the exact even-sized fit in Python when source dims are
        # known (deterministic, no filter-expression pitfalls); otherwise
        # let ffmpeg fit-to-box without upscaling
        if media.width_px and media.height_px:
            scale = min(tw / media.width_px, th / media.height_px, 1.0)
            nw = max(2, int(media.width_px * scale / 2) * 2)
            nh = max(2, int(media.height_px * scale / 2) * 2)
            vf = f"scale={nw}:{nh}"
        else:
            vf = (f"scale=w={tw}:h={th}:force_original_aspect_ratio=decrease"
                  f":force_divisible_by=2")

        out_name = f"{media.filename.rsplit('.', 1)[0]}_opt.mp4"
        out_path = os.path.join(upload_dir, out_name)
        tmp_path = out_path + ".part.mp4"

        media.optimize_status = "processing"
        db.session.commit()
        logger.info(f"Optimizing video: '{media.original_name}' "
                    f"({media.width_px}x{media.height_px}) → {vf}")

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src,
            "-vf", vf,
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            # fastdecode drops CABAC/deblocking: slightly larger files that
            # are measurably cheaper to decode — exactly this box's tradeoff
            "-tune", "fastdecode",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-an",  # outputs are video-only; a silent copy decodes lighter
            tmp_path,
        ]
        # Keep live playback smooth: the encode runs at low priority
        if shutil.which("nice"):
            cmd = ["nice", "-n", "10"] + cmd

        try:
            result = subprocess.run(cmd, capture_output=True,
                                    timeout=TRANSCODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            result = None
        if result is None or result.returncode != 0 or not os.path.exists(tmp_path):
            if result is not None:
                logger.error(f"ffmpeg failed for '{media.original_name}': "
                             f"{(result.stderr or b'')[:500]}")
            else:
                logger.error(f"ffmpeg timed out for '{media.original_name}'")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            media.optimize_status = "failed"
            db.session.commit()
            return

        os.replace(tmp_path, out_path)

        # Probe the copy's real dimensions for the UI badge
        ow = oh = None
        try:
            import cv2
            cap = cv2.VideoCapture(out_path)
            if cap.isOpened():
                ow = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
                oh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
            cap.release()
        except Exception:
            pass

        media.optimized_filename = out_name
        media.optimized_width = ow
        media.optimized_height = oh
        media.optimize_status = "done"
        db.session.commit()
        log_event("MEDIA_OPTIMIZED",
                  f"id={media.id} name='{media.original_name}' "
                  f"{media.width_px}x{media.height_px}→{ow}x{oh}")

        # Running signage players switch to the light copy at their next
        # transition via a live playlist reload; plain video instances pick
        # it up on their next start/play/load command
        from app.models import SignageItem, OutputInstance
        from app.routes import _sync_signage
        affected = {it.instance_id for it in
                    SignageItem.query.filter_by(media_file_id=media.id).all()}
        for iid in affected:
            inst = db.session.get(OutputInstance, iid)
            if inst is not None:
                _sync_signage(inst)


transcoder = Transcoder()
