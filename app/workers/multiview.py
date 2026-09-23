"""
Multiview — composites every output into one real-time NDI source.

The app runs one as the built-in "Overview" stream (toggled on the Overview
tab). It is an ordinary worker process (same watchdog,
heartbeat, preview and NDI sender as any output) whose "capture" is a grid
of the other outputs, received back over NDI at full bandwidth:

  - One receiver thread per tile. Each pulls its output's NDI stream,
    letterboxes frames into the tile's own buffer, and stamps the time the
    latest arrived. The send loop composites the newest frame of every tile
    at the Overview's own rate (OVERVIEW_FPS, 30 by default), so tiles are
    real time with at most one output frame of compositing latency; frames
    arriving faster than that are received but not scaled.
  - Outputs stay independent. A multiview only *receives*: an output that
    stops or crashes just turns its tile to STOPPED / NO SIGNAL, and a
    multiview that dies takes no output down with it.
  - Local, discovery-free connections. Every worker publishes its NDI
    sender's TCP port to <preview_dir>/<id>.ndi.json; the multiview connects
    straight to 127.0.0.1:<port> (validated against the owning pid so a
    stale file can never show the wrong output). If that yields no video it
    falls back to connecting by NDI name, then keeps cycling.
  - Live layout. The app rewrites one layout JSON (instance id, name, type,
    running) on every create/edit/delete/start/stop; the multiview polls its
    mtime and re-lays out within LAYOUT_POLL_S — no restart, no dropped
    stream. Disabled instances are left out entirely; stopped ones keep
    their tile with STOPPED on it.

Layout: tiles are grouped under a header per source type (SECTION_ORDER).
All tiles share one 16:9 size — the largest that fits every section on the
canvas when sections flow left-to-right and wrap like lines of text. Each
tile carries its instance name underneath. Headers, labels and STOPPED
tiles are drawn once per layout change; per frame only live tile regions
are copied.
"""

import os
import json
import time
import socket
import logging
import tempfile
import threading
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# The Overview stream: one built-in multiview per box, switched on and off
# from the app (GlobalSettings.overview_enabled). It runs under a reserved
# worker id that no database row can have (ids start at 1), so it gets the
# watchdog, heartbeat and preview plumbing of any output for free.
OVERVIEW_INSTANCE_ID = 0
OVERVIEW_NAME = "Overview"

# Section order and header titles. Types not listed (and multiview
# instances themselves) never get a tile.
SECTION_ORDER = [
    ("image", "Images"),
    ("video", "Video"),
    ("signage", "Signage"),
    ("webpage", "Webpage"),
    ("text", "Text"),
    ("webcam", "Webcam"),
]
SECTION_TITLES = dict(SECTION_ORDER)

# How often the layout file's mtime is checked (seconds)
LAYOUT_POLL_S = 0.5

# A running tile whose receiver has had no video this long tries the next
# connection candidate (local port → NDI name → re-read endpoint → ...)
RECONNECT_AFTER_S = 3.0

# After live frames stop arriving, the tile switches to NO SIGNAL
NO_SIGNAL_AFTER_S = 2.0

# recv_capture timeout (ms) — also bounds how long a receiver takes to stop
RECV_TIMEOUT_MS = 100

# The NDI SDK's per-host messaging server listens here; it is never a
# sender's port, so it is skipped when picking connection candidates
NDI_MESSAGING_PORT = 5960

# Receive quality. "highest" = the full-quality stream (real time, full
# resolution); "lowest" = NDI's low-bandwidth preview stream, for boxes
# that can't afford decoding every output at full quality.
BANDWIDTHS = ("highest", "lowest")

# Palette (RGB)
COL_BG = (14, 15, 18)
COL_HEADER = (138, 180, 248)
COL_RULE = (52, 58, 70)
COL_LABEL = (225, 228, 234)
COL_TILE = (0, 0, 0)
COL_STOPPED_BG = (28, 29, 33)
COL_STOPPED_TX = (120, 124, 134)
COL_WAIT_TX = (110, 116, 128)
COL_NOSIG_TX = (240, 170, 60)


# ----------------------------------------------------------------------
# Endpoint publishing (worker side) and lookup (multiview side)
# ----------------------------------------------------------------------

def endpoint_path(preview_dir: str, instance_id: int) -> str:
    return os.path.join(preview_dir, f"{instance_id}.ndi.json")


def own_listen_ports() -> set:
    """Every TCP port this process is listening on (empty without psutil)."""
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        get_conns = getattr(proc, "net_connections", None) or proc.connections
        conns = get_conns(kind="tcp")
    except Exception:
        return set()
    return {c.laddr.port for c in conns if c.status == "LISTEN" and c.laddr}


def own_sender_ports(inherited: set) -> list:
    """TCP ports this process's NDI sender listens on: everything listening
    now, minus the SDK's shared messaging port and minus `inherited` — the
    snapshot taken before NDI started. Workers are forked from the web
    server and inherit its listening socket (e.g. :5000); without the
    snapshot that port would be published as an NDI endpoint."""
    return sorted(own_listen_ports() - set(inherited) - {NDI_MESSAGING_PORT})


def publish_endpoint(preview_dir: str, instance_id: int, ports: list) -> bool:
    """Atomically write this worker's NDI endpoint for multiviews to find."""
    if not preview_dir or not ports:
        return False
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=preview_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "ports": ports}, f)
        os.replace(tmp, endpoint_path(preview_dir, instance_id))
        return True
    except OSError as e:
        logger.debug(f"NDI endpoint publish failed for {instance_id}: {e}")
        return False


def read_endpoint_ports(preview_dir: str, instance_id: int) -> list:
    """Ports another worker published, kept only if its pid is alive and
    still listening on them — a file left by a dead worker could otherwise
    point at a port since reused by a different output."""
    try:
        with open(endpoint_path(preview_dir, instance_id), encoding="utf-8") as f:
            data = json.load(f)
        pid, ports = int(data["pid"]), [int(p) for p in data["ports"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    try:
        import psutil
    except ImportError:
        return ports  # can't verify — trust the file
    try:
        proc = psutil.Process(pid)
        get_conns = getattr(proc, "net_connections", None) or proc.connections
        listening = {c.laddr.port for c in get_conns(kind="tcp")
                     if c.status == "LISTEN" and c.laddr}
    except Exception:
        return []
    return [p for p in ports if p in listening]


def ndi_machine_name() -> str:
    """The MACHINE part of 'MACHINE (Source)' — the SDK uses the OS
    hostname, upper-cased, without any domain."""
    return socket.gethostname().split(".")[0].upper()


# ----------------------------------------------------------------------
# Layout
# ----------------------------------------------------------------------

def load_layout_entries(path: str, exclude_id: Optional[int] = None) -> list:
    """Instance entries from the layout JSON the app writes, filtered to
    types that get a tile."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in data.get("instances", []):
        try:
            iid = int(e["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if iid == exclude_id or e.get("source_type") not in SECTION_TITLES:
            continue
        out.append({
            "id": iid,
            "name": str(e.get("name") or f"#{iid}"),
            "source_type": e["source_type"],
            "running": bool(e.get("running")),
        })
    return out


def _metrics(tw: int, W: int, H: int) -> dict:
    th = max(1, round(tw * 9 / 16))
    return {
        "tw": tw, "th": th,
        "label_h": max(14, round(th * 0.15)),
        "header_h": max(16, round(th * 0.17)),
        "gap": max(4, round(tw * 0.025)),
        "margin": max(8, round(min(W, H) * 0.015)),
    }


def _pack(sections: list, m: dict, W: int, H: int) -> Optional[list]:
    """Shelf-pack section blocks for tile metrics m. Returns
    [(shelf_blocks, shelf_height)] or None if it doesn't fit."""
    tw, th, gap = m["tw"], m["th"], m["gap"]
    avail_w, avail_h = W - 2 * m["margin"], H - 2 * m["margin"]
    cols_max = (avail_w + gap) // (tw + gap)
    if cols_max < 1:
        return None
    sgap = gap * 3  # between sections on one shelf
    shelves, cur, cur_w, cur_h = [], [], 0, 0
    for sec in sections:
        n = len(sec["tiles"])
        cols = min(n, cols_max)
        rows = -(-n // cols)
        bw = cols * tw + (cols - 1) * gap
        bh = m["header_h"] + rows * (th + m["label_h"]) + (rows - 1) * gap
        need = bw if not cur else cur_w + sgap + bw
        if cur and need > avail_w:
            shelves.append((cur, cur_w, cur_h))
            cur, cur_w, cur_h = [], 0, 0
            need = bw
        cur.append((sec, cols, bw, bh))
        cur_w, cur_h = need, max(cur_h, bh)
    if cur:
        shelves.append((cur, cur_w, cur_h))
    total_h = sum(s[2] for s in shelves) + (len(shelves) - 1) * gap * 2
    return shelves if total_h <= avail_h else None


def compute_layout(entries: list, W: int, H: int) -> dict:
    """Place every entry's tile on a W×H canvas.

    Returns {"sections": [{"key", "title", "header": (x, y, w, h),
    "tiles": [{**entry, "cell": (x, y, w, h), "label": (x, y, w, h)}]}],
    "metrics": {...}}; sections is empty when there is nothing to show."""
    sections = []
    for key, title in SECTION_ORDER:
        tiles = [dict(e) for e in entries if e["source_type"] == key]
        if tiles:
            sections.append({"key": key, "title": title, "tiles": tiles})
    if not sections:
        return {"sections": [], "metrics": None}

    # Largest tile width that fits everything (a few hundred cheap passes,
    # only on layout changes)
    m = shelves = None
    for tw in range(W, 15, -1):
        m = _metrics(tw, W, H)
        shelves = _pack(sections, m, W, H)
        if shelves:
            break
    if not shelves:
        return {"sections": [], "metrics": None}

    gap, th, tw = m["gap"], m["th"], m["tw"]
    total_h = sum(s[2] for s in shelves) + (len(shelves) - 1) * gap * 2
    y = (H - total_h) // 2
    for blocks, shelf_w, shelf_h in shelves:
        x = (W - shelf_w) // 2
        for sec, cols, bw, _bh in blocks:
            sec["header"] = (x, y, bw, m["header_h"])
            for i, tile in enumerate(sec["tiles"]):
                r, c = divmod(i, cols)
                tx = x + c * (tw + gap)
                ty = y + m["header_h"] + r * (th + m["label_h"] + gap)
                tile["cell"] = (tx, ty, tw, th)
                tile["label"] = (tx, ty + th, tw, m["label_h"])
            x += bw + gap * 3
        y += shelf_h + gap * 2
    return {"sections": sections, "metrics": m}


# ----------------------------------------------------------------------
# Drawing
# ----------------------------------------------------------------------

_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
_font_cache: dict = {}


def _font(size: int):
    size = max(8, int(size))
    f = _font_cache.get(size)
    if f is None:
        for cand in _FONT_CANDIDATES:
            try:
                f = ImageFont.truetype(cand, size)
                break
            except OSError:
                continue
        if f is None:
            f = ImageFont.load_default(size=size)
        _font_cache[size] = f
    return f


def _fit_text(draw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _centered(draw, box, text, font, fill):
    x, y, w, h = box
    text = _fit_text(draw, text, font, w - 4)
    draw.text((x + w / 2, y + h / 2), text, font=font, fill=fill, anchor="mm")


def _to_bgrx(img: Image.Image) -> np.ndarray:
    rgb = np.asarray(img.convert("RGB"))
    out = np.empty(rgb.shape[:2] + (4,), dtype=np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = 255
    return out


def render_base(layout: dict, W: int, H: int, empty_text: str = "No outputs") -> np.ndarray:
    """Static canvas for a layout: background, section headers, name
    labels, black tiles for running outputs and STOPPED tiles."""
    img = Image.new("RGB", (W, H), COL_BG)
    d = ImageDraw.Draw(img)
    m = layout["metrics"]
    if not layout["sections"]:
        _centered(d, (0, 0, W, H), empty_text, _font(H * 0.04), COL_WAIT_TX)
        return _to_bgrx(img)

    hfont = _font(m["header_h"] * 0.62)
    lfont = _font(m["label_h"] * 0.62)
    sfont = _font(m["th"] * 0.12)
    for sec in layout["sections"]:
        x, y, w, h = sec["header"]
        title = _fit_text(d, f"{sec['title'].upper()}  ·  {len(sec['tiles'])}", hfont, w)
        d.text((x, y + h * 0.42), title, font=hfont, fill=COL_HEADER, anchor="lm")
        d.line([(x, y + h - 3), (x + w, y + h - 3)], fill=COL_RULE, width=1)
        for t in sec["tiles"]:
            cx, cy, cw, ch = t["cell"]
            if t["running"]:
                d.rectangle([cx, cy, cx + cw - 1, cy + ch - 1], fill=COL_TILE)
            else:
                d.rectangle([cx, cy, cx + cw - 1, cy + ch - 1], fill=COL_STOPPED_BG)
                _centered(d, t["cell"], "STOPPED", sfont, COL_STOPPED_TX)
            _centered(d, t["label"], t["name"], lfont, COL_LABEL)
    return _to_bgrx(img)


def render_placeholder(w: int, h: int, text: str, color) -> np.ndarray:
    img = Image.new("RGB", (w, h), COL_TILE)
    _centered(ImageDraw.Draw(img), (0, 0, w, h), text, _font(h * 0.12), color)
    return _to_bgrx(img)


# ----------------------------------------------------------------------
# Tile receiver
# ----------------------------------------------------------------------

class TileReceiver(threading.Thread):
    """Receives one output over NDI into a tile-sized BGRX buffer.

    Owns all blocking NDI receive calls (the SDK releases the GIL while
    waiting), so the send loop never stalls on a slow or missing source."""

    def __init__(self, ndi, entry: dict, cell_w: int, cell_h: int,
                 preview_dir: Optional[str], bandwidth: str, machine: str,
                 min_interval: float = 0.0):
        super().__init__(daemon=True, name=f"mv-rx-{entry['id']}")
        self.ndi = ndi
        self.instance_id = entry["id"]
        self.inst_name = entry["name"]
        self.cell_w, self.cell_h = cell_w, cell_h
        self.preview_dir = preview_dir
        self.bandwidth = bandwidth
        self.machine = machine
        self.buf = np.zeros((cell_h, cell_w, 4), dtype=np.uint8)
        self.buf[..., 3] = 255
        self.lock = threading.Lock()
        self.last_frame = 0.0  # monotonic time of the latest frame, 0 = none yet
        # Frames arriving faster than the compositor uses them are received
        # (NDI must decode them anyway) but not scaled: a 60fps source on a
        # 30fps Overview contributes every other frame
        self.min_interval = min_interval
        self._last_blit = float("-inf")
        self._halt = threading.Event()
        self._src_size = None
        self._fit = None  # (fw, fh, ox, oy)
        self._scratch = None

    def stop(self):
        self._halt.set()

    def _candidates(self) -> list:
        ndi = self.ndi
        cands = []
        if self.preview_dir:
            for port in read_endpoint_ports(self.preview_dir, self.instance_id):
                cands.append(ndi.Source(p_url_address=f"127.0.0.1:{port}"))
        cands.append(ndi.Source(p_ndi_name=f"{self.machine} ({self.inst_name})"))
        return cands

    def _blit(self, src: np.ndarray):
        import cv2
        sh, sw = src.shape[:2]
        if (sw, sh) != self._src_size:
            scale = min(self.cell_w / sw, self.cell_h / sh)
            fw = max(1, min(self.cell_w, round(sw * scale)))
            fh = max(1, min(self.cell_h, round(sh * scale)))
            self._fit = (fw, fh, (self.cell_w - fw) // 2, (self.cell_h - fh) // 2)
            self._scratch = np.empty((fh, fw, 4), dtype=np.uint8)
            self._src_size = (sw, sh)
            with self.lock:
                self.buf[..., :3] = 0  # clear stale letterbox bars
        fw, fh, ox, oy = self._fit
        if (fw, fh) == (sw, sh):
            self._scratch[:] = src
        else:
            # Halve with a 2x box filter while the frame is still at least
            # twice the tile, then one linear step to the exact size —
            # mipmap-quality downscaling at ~1/10 the cost of a direct
            # INTER_AREA (which has no fast path for non-integer ratios and
            # dominated CPU with many 60fps tiles)
            m = src
            while m.shape[1] // 2 >= fw and m.shape[0] // 2 >= fh:
                m = cv2.resize(m, (m.shape[1] // 2, m.shape[0] // 2),
                               interpolation=cv2.INTER_AREA)
            cv2.resize(m, (fw, fh), dst=self._scratch, interpolation=cv2.INTER_LINEAR)
        with self.lock:
            self.buf[oy:oy + fh, ox:ox + fw, :3] = self._scratch[..., :3]

    def run(self):
        ndi = self.ndi
        rc = ndi.RecvCreateV3()
        rc.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        rc.bandwidth = (ndi.RECV_BANDWIDTH_LOWEST if self.bandwidth == "lowest"
                        else ndi.RECV_BANDWIDTH_HIGHEST)
        rc.allow_video_fields = False
        rc.ndi_recv_name = "Multiview"
        recv = ndi.recv_create_v3(rc)
        if recv is None:
            logger.error(f"Multiview: could not create receiver for '{self.inst_name}'")
            return
        cands, idx, switched_at = [], -1, float("-inf")
        try:
            while not self._halt.is_set():
                now = time.monotonic()
                if now - max(self.last_frame, switched_at) > RECONNECT_AFTER_S:
                    idx += 1
                    if idx >= len(cands):
                        cands, idx = self._candidates(), 0
                    ndi.recv_connect(recv, cands[idx])
                    switched_at = now
                t, v, a, md = ndi.recv_capture_v2(recv, RECV_TIMEOUT_MS)
                if t == ndi.FRAME_TYPE_VIDEO:
                    try:
                        got = time.monotonic()
                        if (got - self._last_blit >= self.min_interval
                                and v.data is not None and v.data.ndim == 3
                                and v.data.shape[2] == 4):
                            self._blit(v.data)
                            self._last_blit = got
                        self.last_frame = got
                    finally:
                        ndi.recv_free_video_v2(recv, v)
                elif t == ndi.FRAME_TYPE_AUDIO:
                    ndi.recv_free_audio_v2(recv, a)
                elif t == ndi.FRAME_TYPE_METADATA:
                    ndi.recv_free_metadata(recv, md)
        except Exception:
            logger.exception(f"Multiview receiver for '{self.inst_name}' failed")
        finally:
            try:
                ndi.recv_destroy(recv)
            except Exception:
                pass


# ----------------------------------------------------------------------
# Compositor
# ----------------------------------------------------------------------

class MultiviewCompositor:
    """Keeps the layout, the receivers and the frame buffer in sync.

    poll_layout() re-reads the layout file when it changes; compose()
    copies each running tile's latest frame (or a placeholder) into the
    frame buffer. Pass ndi=None for dummy mode: the layout still renders,
    running tiles read NDI UNAVAILABLE."""

    def __init__(self, width: int, height: int, layout_path: str,
                 preview_dir: Optional[str], bandwidth: str = "highest",
                 exclude_id: Optional[int] = None, ndi=None, fps: float = 30.0):
        self.W, self.H = width, height
        # Scale at most one frame per output frame (with headroom, so a
        # 60fps source on 30fps lands on every other frame, not every third)
        self.min_interval = 0.8 / max(1.0, float(fps))
        self.layout_path = layout_path
        self.preview_dir = preview_dir
        self.bandwidth = bandwidth if bandwidth in BANDWIDTHS else "highest"
        self.exclude_id = exclude_id
        self.ndi = ndi
        self.machine = ndi_machine_name()
        self.layout = {"sections": [], "metrics": None}
        self.tiles: list = []           # running tiles: entry dicts with "cell"
        self.receivers: dict = {}       # instance id -> TileReceiver
        self._entries = None
        self._mtime = None
        self._last_poll = float("-inf")
        self._wait = self._nosig = None

    def poll_layout(self, frame_buffer: np.ndarray, now: float, force: bool = False) -> bool:
        """Re-read the layout file if it changed; on a real change re-render
        the base into frame_buffer and reconcile receivers. True if the
        layout changed."""
        if not force and now - self._last_poll < LAYOUT_POLL_S:
            return False
        self._last_poll = now
        try:
            mtime = os.stat(self.layout_path).st_mtime_ns
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        entries = load_layout_entries(self.layout_path, self.exclude_id)
        if not force and entries == self._entries:
            return False
        self._entries = entries
        self._apply(entries, frame_buffer)
        return True

    def _apply(self, entries: list, frame_buffer: np.ndarray):
        layout = compute_layout(entries, self.W, self.H)
        frame_buffer[:] = render_base(layout, self.W, self.H)
        self.layout = layout
        tiles = [t for s in layout["sections"] for t in s["tiles"] if t["running"]]
        self.tiles = tiles
        m = layout["metrics"]
        if m:
            wait_txt = "CONNECTING…" if self.ndi is not None else "NDI UNAVAILABLE"
            self._wait = render_placeholder(m["tw"], m["th"], wait_txt, COL_WAIT_TX)
            self._nosig = render_placeholder(m["tw"], m["th"], "NO SIGNAL", COL_NOSIG_TX)

        if self.ndi is None:
            return
        wanted = {t["id"]: t for t in tiles}
        stale = []
        for iid, rx in self.receivers.items():
            t = wanted.get(iid)
            if t is None or (rx.cell_w, rx.cell_h) != t["cell"][2:] or rx.inst_name != t["name"]:
                stale.append(iid)
        for iid in stale:
            self.receivers[iid].stop()
        for iid in stale:
            self.receivers.pop(iid).join(timeout=2)
        for iid, t in wanted.items():
            if iid not in self.receivers:
                rx = TileReceiver(self.ndi, t, t["cell"][2], t["cell"][3],
                                  self.preview_dir, self.bandwidth, self.machine,
                                  self.min_interval)
                rx.start()
                self.receivers[iid] = rx
        logger.info(
            f"Multiview layout: {len(entries)} output(s), {len(tiles)} running, "
            f"tile {m['tw']}x{m['th']}" if m else "Multiview layout: no outputs"
        )

    def compose(self, frame_buffer: np.ndarray, now: float):
        for t in self.tiles:
            x, y, w, h = t["cell"]
            rx = self.receivers.get(t["id"])
            if rx is None or rx.last_frame == 0.0:
                frame_buffer[y:y + h, x:x + w] = self._wait
            elif now - rx.last_frame > NO_SIGNAL_AFTER_S:
                frame_buffer[y:y + h, x:x + w] = self._nosig
            else:
                with rx.lock:
                    frame_buffer[y:y + h, x:x + w] = rx.buf

    def close(self):
        for rx in self.receivers.values():
            rx.stop()
        for rx in self.receivers.values():
            rx.join(timeout=2)
        self.receivers.clear()
