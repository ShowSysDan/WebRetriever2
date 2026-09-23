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

Layout: tiles are grouped under a header per source type (SECTION_ORDER)
on one uniform grid of equal 16:9 tiles, as large as fits. Sections flow
into each other like text (a section can start mid-row and continue on
the next) so rows aren't left half empty; each tile carries its instance
name underneath. Headers, labels and STOPPED
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

# Per-tile connection status, written next to the layout file for the API
STATUS_FILENAME = "overview_status.json"
STATUS_INTERVAL_S = 1.0

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
COL_DIVIDER = (88, 108, 150)
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
        "label_h": max(14, round(th * 0.13)),
        "header_h": max(14, round(th * 0.13)),
        "gap": max(3, round(tw * 0.015)),
        "margin": max(6, round(min(W, H) * 0.008)),
    }


def compute_layout(entries: list, W: int, H: int) -> dict:
    """Place every entry's tile on a W×H canvas.

    All tiles share one 16:9 size on a uniform grid; every grid row is a
    header band, the tiles, and a name band. The tile size is the largest
    for which the tiles fit when sections flow into each other like text
    (a section may start mid-row and continue on the next), so no row is
    left mostly empty just because a section ended. If starting every
    section on a fresh row fits at that same size, that tidier arrangement
    is used instead.

    Returns {"sections": [{"key", "title", "tiles": [{**entry, "cell",
    "label"}], "segments": [{"rect", "text", "divider"}]}], "metrics"};
    sections is empty when there is nothing to show. A section has one
    header segment per grid row it occupies."""
    sections = []
    for key, title in SECTION_ORDER:
        tiles = [dict(e) for e in entries if e["source_type"] == key]
        if tiles:
            sections.append({"key": key, "title": title, "tiles": tiles})
    n = sum(len(sec["tiles"]) for sec in sections)
    if not n:
        return {"sections": [], "metrics": None}

    m = cols = None
    newline = False
    for tw in range(W, 15, -1):  # a few hundred cheap passes, on layout changes only
        mt = _metrics(tw, W, H)
        gap = mt["gap"]
        c = min(n, (W - 2 * mt["margin"] + gap) // (tw + gap))
        if c < 1:
            continue
        row_h = mt["header_h"] + mt["th"] + mt["label_h"]
        avail_h = H - 2 * mt["margin"]

        def height(rows):
            return rows * row_h + (rows - 1) * gap

        if height(-(-n // c)) <= avail_h:
            m, cols = mt, c
            newline = height(sum(-(-len(sec["tiles"]) // c) for sec in sections)) <= avail_h
            break
    if m is None:
        return {"sections": [], "metrics": None}

    tw, th, gap = m["tw"], m["th"], m["gap"]
    row_h = m["header_h"] + th + m["label_h"]
    # Grid slots: flow fills cells in order; newline starts each section on
    # a fresh row
    slots, i = [], 0
    for sec in sections:
        if newline and i % cols:
            i += cols - i % cols
        for _ in sec["tiles"]:
            slots.append(divmod(i, cols))
            i += 1
    rows = slots[-1][0] + 1
    grid_w = cols * tw + (cols - 1) * gap
    x0 = (W - grid_w) // 2
    y0 = (H - (rows * row_h + (rows - 1) * gap)) // 2

    k = 0
    for sec in sections:
        by_row = {}
        for tile in sec["tiles"]:
            r, c = slots[k]
            k += 1
            tx = x0 + c * (tw + gap)
            ry = y0 + r * (row_h + gap)
            tile["cell"] = (tx, ry + m["header_h"], tw, th)
            tile["label"] = (tx, ry + m["header_h"] + th, tw, m["label_h"])
            by_row.setdefault(r, []).append((c, tx, ry))
        sec["segments"] = []
        for j, (r, cells) in enumerate(sorted(by_row.items())):
            c0, x_first, ry = cells[0]
            x_last = cells[-1][1]
            text = (f"{sec['title'].upper()}  ·  {len(sec['tiles'])}" if j == 0
                    else f"{sec['title'].upper()}  (cont.)")
            sec["segments"].append({
                "rect": (x_first, ry, x_last + tw - x_first, m["header_h"]),
                "row_h": row_h,
                "text": text,
                # a divider separates this section from the one before it
                # in the same row
                "divider": c0 > 0,
            })
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
    gap = m["gap"]
    for sec in layout["sections"]:
        for seg in sec["segments"]:
            x, y, w, h = seg["rect"]
            title = _fit_text(d, seg["text"], hfont, w)
            d.text((x + 2, y + h * 0.45), title, font=hfont, fill=COL_HEADER, anchor="lm")
            d.line([(x, y + h - 2), (x + w, y + h - 2)], fill=COL_RULE, width=1)
            if seg["divider"]:
                dx = x - (gap + 1) // 2
                d.line([(dx, y), (dx, y + seg["row_h"])], fill=COL_DIVIDER, width=max(1, gap // 3))
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
# NDI helpers
# ----------------------------------------------------------------------

def make_source(ndi, name: Optional[str] = None, url: Optional[str] = None):
    """Build an ndi.Source safely. ndi-python's Source(p_ndi_name=...,
    p_url_address=...) constructor keeps raw pointers to temporary strings
    that are freed when the call returns, so the SDK can read garbage; the
    property setters copy the string into storage that stays alive."""
    src = ndi.Source()
    if name:
        src.ndi_name = name
    if url:
        src.url_address = url
    return src


def capture_releases_gil(ndi) -> bool:
    """Does recv_capture release the GIL while it waits? ndi-python 6.x
    does; 5.x holds it for the whole timeout, which would make every tile
    thread (and the send loop) wait in turn. Measured once: a thread blocks
    in a 300 ms capture on a dead address while this thread sleeps 50 ms —
    if the sleep can't resume until the capture returns, the GIL was held."""
    try:
        recv = ndi.recv_create_v3(ndi.RecvCreateV3())
        if recv is None:
            return True
        ndi.recv_connect(recv, make_source(ndi, url="127.0.0.1:1"))
        t = threading.Thread(target=ndi.recv_capture_v2, args=(recv, 300), daemon=True)
        t0 = time.monotonic()
        t.start()
        time.sleep(0.05)
        held = time.monotonic() - t0 > 0.2
        t.join(timeout=2)
        ndi.recv_destroy(recv)
        return not held
    except Exception:
        logger.exception("Multiview: GIL probe failed — assuming capture holds the GIL")
        return False


_local_ips_cache = None


def local_ipv4s() -> list:
    """This box's non-loopback IPv4 addresses (the NDI sender may not
    accept loopback connections when its NDI config pins it to a NIC)."""
    global _local_ips_cache
    if _local_ips_cache is None:
        ips = []
        try:
            import psutil
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family == socket.AF_INET and not a.address.startswith("127."):
                        ips.append(a.address)
        except Exception:
            pass
        _local_ips_cache = ips
    return _local_ips_cache


class SourceFinder(threading.Thread):
    """Keeps a snapshot of the NDI sources discovery can see (mDNS or a
    discovery server). Uses only the non-blocking get call, so it never
    holds the GIL for long even on bindings that don't release it."""

    INTERVAL = 2.0

    def __init__(self, ndi):
        super().__init__(daemon=True, name="mv-finder")
        self.ndi = ndi
        self.sources: list = []  # [(ndi_name, url_address)]
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        ndi = self.ndi
        try:
            finder = ndi.find_create_v2()
        except Exception:
            logger.exception("Multiview: NDI discovery unavailable")
            return
        if finder is None:
            return
        try:
            while not self._halt.wait(self.INTERVAL):
                try:
                    found = ndi.find_get_current_sources(finder) or []
                    self.sources = [(str(f.ndi_name or ""), str(f.url_address or ""))
                                    for f in found]
                except Exception:
                    logger.debug("Multiview: discovery poll failed", exc_info=True)
        finally:
            try:
                ndi.find_destroy(finder)
            except Exception:
                pass

    def matches(self, inst_name: str, machine: str) -> list:
        """Discovered sources for an output: '<machine> (<name>)', this
        machine first (compared case-insensitively — the SDK's casing of
        the machine part varies by platform)."""
        suffix = f"({inst_name})"
        hits = [(n, u) for n, u in self.sources if n.endswith(suffix)]
        hits.sort(key=lambda nu: nu[0][: -len(suffix)].strip().lower() != machine.lower())
        return hits


# ----------------------------------------------------------------------
# Tile receiver
# ----------------------------------------------------------------------

# A tile still not live after this long logs one warning listing what it
# tried (and again each time the whole candidate list has been exhausted)
CONNECT_WARN_AFTER_S = 20.0


class TileReceiver(threading.Thread):
    """Receives one output over NDI into a tile-sized BGRX buffer.

    Owns all NDI receive calls, so the send loop never stalls on a slow or
    missing source. Connection candidates are tried in turn — the worker's
    published port on 127.0.0.1 and on this box's LAN addresses, then what
    NDI discovery found under the output's name, then the name itself —
    moving on after RECONNECT_AFTER_S without video, and re-read each time
    the list runs out. Any error restarts the session after a pause; the
    thread only ends when stopped."""

    def __init__(self, ndi, entry: dict, cell_w: int, cell_h: int,
                 preview_dir: Optional[str], bandwidth: str, machine: str,
                 min_interval: float = 0.0, finder: Optional[SourceFinder] = None,
                 blocking_capture: bool = True):
        super().__init__(daemon=True, name=f"mv-rx-{entry['id']}")
        self.ndi = ndi
        self.instance_id = entry["id"]
        self.inst_name = entry["name"]
        self.cell_w, self.cell_h = cell_w, cell_h
        self.preview_dir = preview_dir
        self.bandwidth = bandwidth
        self.machine = machine
        self.finder = finder
        # False when the binding holds the GIL inside recv_capture: poll with
        # a zero timeout and sleep (which releases the GIL) in between
        self.blocking_capture = blocking_capture
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
        # Diagnostics, read by the compositor's status report
        self.via = None          # candidate label currently connected/tried
        self.tried: list = []    # labels tried in the current cycle
        self.error = None        # last exception text
        self.frames = 0          # frames received (for a fps estimate)
        self._born = time.monotonic()
        self._warned_at = 0.0

    def stop(self):
        self._halt.set()

    def _candidates(self) -> list:
        """[(label, ndi.Source)] in the order to try them."""
        ndi = self.ndi
        cands = []
        ports = read_endpoint_ports(self.preview_dir, self.instance_id) if self.preview_dir else []
        for port in ports:
            for host in ["127.0.0.1"] + local_ipv4s():
                url = f"{host}:{port}"
                cands.append((url, make_source(ndi, url=url)))
        if self.finder is not None:
            for name, url in self.finder.matches(self.inst_name, self.machine):
                cands.append((f"discovered '{name}' @ {url or '?'}",
                              make_source(ndi, name=name, url=url or None)))
        for machine in dict.fromkeys([self.machine, socket.gethostname().split(".")[0]]):
            name = f"{machine} ({self.inst_name})"
            cands.append((f"name '{name}'", make_source(ndi, name=name)))
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
        while not self._halt.is_set():
            try:
                self._session()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                logger.exception(f"Multiview receiver for '{self.inst_name}' failed — retrying")
                self._halt.wait(2.0)

    def _session(self):
        ndi = self.ndi
        rc = ndi.RecvCreateV3()
        rc.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        rc.bandwidth = (ndi.RECV_BANDWIDTH_LOWEST if self.bandwidth == "lowest"
                        else ndi.RECV_BANDWIDTH_HIGHEST)
        rc.allow_video_fields = False
        rc.ndi_recv_name = "Overview"
        recv = ndi.recv_create_v3(rc)
        if recv is None:
            raise RuntimeError("recv_create_v3 returned None")
        timeout_ms = RECV_TIMEOUT_MS if self.blocking_capture else 0
        cands, idx, switched_at = [], -1, float("-inf")
        was_live = False
        try:
            while not self._halt.is_set():
                now = time.monotonic()
                if now - max(self.last_frame, switched_at) > RECONNECT_AFTER_S:
                    if was_live:
                        logger.warning(f"Overview: '{self.inst_name}' lost video on {self.via}")
                        was_live = False
                    idx += 1
                    if idx >= len(cands):
                        if self.tried and now - self._born > CONNECT_WARN_AFTER_S \
                                and now - self._warned_at > CONNECT_WARN_AFTER_S:
                            self._warned_at = now
                            logger.warning(
                                f"Overview: no video yet from '{self.inst_name}' — tried "
                                f"{', '.join(self.tried)}"
                            )
                        cands, idx = self._candidates(), 0
                        self.tried = []
                    self.via = cands[idx][0]
                    self.tried.append(self.via)
                    ndi.recv_connect(recv, cands[idx][1])
                    switched_at = now
                t, v, a, md = ndi.recv_capture_v2(recv, timeout_ms)
                if t == ndi.FRAME_TYPE_VIDEO:
                    try:
                        got = time.monotonic()
                        if (got - self._last_blit >= self.min_interval
                                and v.data is not None and v.data.ndim == 3
                                and v.data.shape[2] == 4):
                            self._blit(v.data)
                            self._last_blit = got
                        self.last_frame = got
                        self.frames += 1
                    finally:
                        ndi.recv_free_video_v2(recv, v)
                    if not was_live:
                        was_live = True
                        self.error = None
                        logger.info(f"Overview: '{self.inst_name}' live via {self.via}")
                elif t == ndi.FRAME_TYPE_AUDIO:
                    ndi.recv_free_audio_v2(recv, a)
                elif t == ndi.FRAME_TYPE_METADATA:
                    ndi.recv_free_metadata(recv, md)
                elif not self.blocking_capture and t == ndi.FRAME_TYPE_NONE:
                    time.sleep(0.003)  # releases the GIL between polls
        finally:
            try:
                ndi.recv_destroy(recv)
            except Exception:
                pass

    def status(self, now: float) -> dict:
        if self.last_frame and now - self.last_frame <= NO_SIGNAL_AFTER_S:
            state = "live"
        elif self.last_frame:
            state = "no_signal"
        else:
            state = "connecting"
        return {"id": self.instance_id, "name": self.inst_name, "state": state,
                "via": self.via, "tried": list(self.tried), "frames": self.frames,
                "error": self.error}


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
        self.status_path = os.path.join(os.path.dirname(layout_path) or ".",
                                        STATUS_FILENAME) if layout_path else None
        self._last_status = float("-inf")
        self.finder = None
        self.blocking_capture = True
        if ndi is not None:
            self.blocking_capture = capture_releases_gil(ndi)
            self.finder = SourceFinder(ndi)
            self.finder.start()
            logger.info(
                "Overview: recv_capture "
                + ("releases the GIL — blocking capture" if self.blocking_capture
                   else "holds the GIL (older ndi-python) — polling capture")
                + f"; local addresses {['127.0.0.1'] + local_ipv4s()}"
            )

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
                                  self.min_interval, finder=self.finder,
                                  blocking_capture=self.blocking_capture)
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

    def write_status(self, now: float):
        """Atomically write per-tile connection state (for /api/overview),
        at most once per STATUS_INTERVAL_S."""
        if not self.status_path or now - self._last_status < STATUS_INTERVAL_S:
            return
        self._last_status = now
        data = {
            "blocking_capture": self.blocking_capture,
            "discovered": len(self.finder.sources) if self.finder else 0,
            "tiles": [rx.status(now) for rx in self.receivers.values()],
        }
        try:
            fd, tmp = tempfile.mkstemp(suffix=".json", dir=os.path.dirname(self.status_path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.status_path)
        except OSError:
            pass

    def close(self):
        if self.finder is not None:
            self.finder.stop()
        for rx in self.receivers.values():
            rx.stop()
        for rx in self.receivers.values():
            rx.join(timeout=2)
        self.receivers.clear()
