"""
Worker Manager — manages lifecycle of NDI worker processes.

Features:
  - Start/stop individual instances or all at once
  - Crash recovery: watchdog detects dead processes and restarts them
  - Hang detection: shared heartbeat values let the watchdog detect workers
    that are alive but stuck (e.g. Playwright blocked on an unresponsive page)
  - Configurable browser recycling interval passed to each worker
"""

import os
import re
import time
import ctypes
import shutil
import signal
import socket
import logging
import threading
import subprocess
import multiprocessing as mp
from typing import Dict, Optional

from app.workers.ndi_worker import (
    NDIWorker, worker_entry, HEARTBEAT_TIMEOUT,
    VIDEO_CMD_NONE, VIDEO_CMD_PLAY, VIDEO_CMD_STOP,
    VIDEO_CMD_LOAD, VIDEO_CMD_LOAD_PLAY,
    VIDEO_STATE_PLAYING,
    VIDEO_HOLD_UNSET, VIDEO_HOLD_FIRST, VIDEO_HOLD_LAST,
    VIDEO_PATH_MAX,
    SIGNAGE_CMD_SKIP, SIGNAGE_CMD_RELOAD,
    TALLY_PROGRAM, TALLY_PREVIEW,
)
from app.logging_config import log_event

logger = logging.getLogger(__name__)

# How often the watchdog checks (seconds)
WATCHDOG_INTERVAL = 5

# Default browser recycle interval (hours)
DEFAULT_RECYCLE_HOURS = 4

# Restart backoff: doubles per consecutive failure, capped; resets after a
# stable run. Prevents a permanently-broken instance from respawning (and
# relaunching Chromium) every watchdog tick forever.
RESTART_BACKOFF_MAX = 300.0
RESTART_STABLE_RESET = 120.0


# --- Receiver identification (socket level) --------------------------------
# The NDI send API reports HOW MANY receivers are connected but not WHO.
# The OS knows: each worker process owns its NDI sender's listening TCP
# socket, and every receiver holds at least one established connection to it
# (the reliable control/metadata connection exists even when video travels
# over UDP or multicast). Enumerating the worker's sockets gives peer IPs;
# reverse DNS turns those into hostnames.

# Transport classification: every NDI receiver holds a TCP control
# connection, so presence alone can't tell TCP media from UDP/multicast
# media. Throughput can: media over TCP moves megabits on the socket, a
# control-only connection idles at a few kbps. A receiver whose TCP rate
# stays below this threshold is getting its media some other way (UDP or
# multicast).
RX_TCP_MEDIA_MBPS = 0.5

_HOSTNAME_TTL = 300.0  # seconds a reverse-DNS answer (or miss) is cached
_hostname_cache: Dict[str, tuple] = {}  # ip -> (hostname|None, expires_at)
_hostname_pending: set = set()
_hostname_lock = threading.Lock()


def _resolve_hostname(ip: str) -> Optional[str]:
    """Cached, non-blocking reverse DNS.

    Returns the cached name immediately (None while unknown) and kicks off a
    background lookup on a cache miss — gethostbyaddr can block for seconds
    on an unresponsive DNS server, which must never stall an API request.
    The name typically fills in by the caller's next poll."""
    now = time.monotonic()
    with _hostname_lock:
        hit = _hostname_cache.get(ip)
        if hit is not None and hit[1] > now:
            return hit[0]
        if ip in _hostname_pending:
            return hit[0] if hit else None
        _hostname_pending.add(ip)

    def lookup():
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            name = None
        with _hostname_lock:
            _hostname_cache[ip] = (name, time.monotonic() + _HOSTNAME_TTL)
            _hostname_pending.discard(ip)

    threading.Thread(target=lookup, daemon=True, name="rdns-lookup").start()
    return hit[0] if hit else None


def _norm_ip(ip: str) -> str:
    """Normalize an address for cross-source matching: IPv4-mapped IPv6
    (::ffff:10.0.0.5) becomes plain IPv4, zone suffixes (%eth0) drop."""
    ip = ip.split("%", 1)[0]
    if ip.lower().startswith("::ffff:") and "." in ip:
        ip = ip[7:]
    return ip


def _tcp_flow_bytes(ports) -> Optional[dict]:
    """bytes_acked per established TCP connection on the given local ports.

    Returns {(peer_ip, peer_port, local_port): bytes_acked} via `ss -tinO`
    (Linux/iproute2) — bytes_acked is what the peer has acknowledged
    receiving, i.e. data actually delivered to that receiver. None when ss
    isn't available (non-Linux), letting callers report transport unknown.

    Parsing note: with an explicit `state established` filter ss omits the
    State column, so the first two addr:port tokens on a line are the local
    and peer addresses."""
    if not ports or not shutil.which("ss"):
        return None
    filt = " or ".join(f"sport = :{p}" for p in sorted(ports))
    try:
        out = subprocess.run(
            ["ss", "-tinOH", "state", "established", f"( {filt} )"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    flows = {}
    for line in out.splitlines():
        addrs = []
        for tok in line.split():
            host, sep, port = tok.rpartition(":")
            if sep and host and port.isdigit():
                addrs.append((_norm_ip(host.strip("[]")), int(port)))
            if len(addrs) == 2:
                break
        if len(addrs) == 2:
            # ss omits bytes_acked on a connection that has never sent
            # data — that IS the idle control-only case, so count it as 0
            m = re.search(r"bytes_acked:(\d+)", line)
            (l_ip, l_port), (p_ip, p_port) = addrs
            if l_port in ports:
                flows[(p_ip, p_port, l_port)] = int(m.group(1)) if m else 0
    return flows


def _receiver_endpoints_for_pid(pid: int) -> dict:
    """Socket-level receiver list for one worker process.

    Finds the process's TCP LISTEN ports (its NDI sender's port(s)) and
    returns the unique peer IPs of ESTABLISHED connections to them, with a
    per-IP connection count (one receiver typically holds several NDI
    connections: video/audio/metadata). Needs psutil; degrades to
    {'supported': False, 'reason': ...} without it or on access errors."""
    try:
        import psutil
    except ImportError:
        return {"supported": False,
                "reason": "psutil not installed — pip install psutil"}
    try:
        proc = psutil.Process(pid)
        # .connections() was renamed .net_connections() in psutil 6
        get_conns = getattr(proc, "net_connections", None) or proc.connections
        conns = get_conns(kind="tcp")
    except (psutil.Error, OSError) as e:
        return {"supported": False, "reason": str(e)}

    listen_ports = {c.laddr.port for c in conns
                    if c.status == psutil.CONN_LISTEN and c.laddr}
    peers: Dict[str, int] = {}
    for c in conns:
        if (c.status == psutil.CONN_ESTABLISHED and c.laddr and c.raddr
                and c.laddr.port in listen_ports):
            ip = _norm_ip(c.raddr.ip)
            peers[ip] = peers.get(ip, 0) + 1
    return {
        "supported": True,
        "receivers": [
            {"ip": ip, "hostname": _resolve_hostname(ip), "connections": n}
            for ip, n in sorted(peers.items())
        ],
        # internal, for throughput sampling — stripped before serving
        "_listen_ports": listen_ports,
    }


def _kill_process_tree(process: mp.Process):
    """Force-kill a worker AND its descendants (Playwright driver, Chromium).

    Workers call os.setpgrp() on entry, so their pid doubles as their process
    group id — killpg reaps the whole tree in one shot. Falls back to a plain
    kill on platforms without process groups (Windows)."""
    if process.pid and hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    process.kill()


class WorkerManager:
    def __init__(self):
        self._workers: Dict[int, NDIWorker] = {}
        self._processes: Dict[int, mp.Process] = {}
        self._heartbeats: Dict[int, mp.Value] = {}
        self._video_cmds: Dict[int, mp.Value] = {}
        self._video_states: Dict[int, mp.Value] = {}
        self._video_paths: Dict[int, mp.Array] = {}
        self._video_holds: Dict[int, mp.Value] = {}
        self._signage_cmds: Dict[int, mp.Value] = {}
        self._preview_boosts: Dict[int, mp.Value] = {}
        self._ndi_connections: Dict[int, mp.Value] = {}
        self._ndi_tallys: Dict[int, mp.Value] = {}
        # Last bytes_acked sample per receiver connection, for throughput
        self._rx_samples: Dict[int, dict] = {}
        self._configs: Dict[int, dict] = {}
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        # Restart backoff bookkeeping: iid -> (consecutive_failures, last_restart_monotonic)
        self._restart_meta: Dict[int, tuple] = {}
        # Flask serves requests on multiple threads and the watchdog runs on
        # its own — lifecycle ops must not interleave, or a double-clicked
        # Start button races two spawns and orphans one worker untracked
        self._lock = threading.RLock()

    def _forget_instance(self, instance_id: int):
        """Drop every per-instance shared ref (worker, process, control and
        stats Values). One list, used by stop/restart/cleanup alike — so a
        newly added channel can't be forgotten in one of them and leak."""
        for d in (self._workers, self._processes, self._heartbeats,
                  self._video_cmds, self._video_states, self._video_paths,
                  self._video_holds, self._signage_cmds, self._preview_boosts,
                  self._ndi_connections, self._ndi_tallys, self._rx_samples):
            d.pop(instance_id, None)

    def _spawn(self, instance_id: int, config: dict) -> mp.Process:
        """Create a fresh heartbeat + worker + process from a stored config
        and register them. Shared by initial start and watchdog restart."""
        heartbeat = mp.Value(ctypes.c_double, time.monotonic())
        # Preview boost deadline (monotonic, comparable across processes on
        # Linux) — every source type gets one so any output can be popped
        # out into a live preview window
        preview_boost = mp.Value(ctypes.c_double, 0.0)
        self._preview_boosts[instance_id] = preview_boost
        # NDI receiver stats reported back by the worker's send loop:
        # connection count (-1 = unknown, e.g. dummy mode) and tally bits
        ndi_connections = mp.Value(ctypes.c_int, -1)
        ndi_tally = mp.Value(ctypes.c_int, 0)
        self._ndi_connections[instance_id] = ndi_connections
        self._ndi_tallys[instance_id] = ndi_tally
        extra = {"preview_boost": preview_boost,
                 "ndi_connections": ndi_connections, "ndi_tally": ndi_tally}
        if config.get("source_type") == "video":
            # Shared control channel for play/stop/load commands, the file
            # path payload for loads, hold-frame overrides, and playback state
            video_cmd = mp.Value(ctypes.c_int, VIDEO_CMD_NONE)
            video_state = mp.Value(ctypes.c_int, 0)
            video_path = mp.Array(ctypes.c_char, VIDEO_PATH_MAX)
            video_hold = mp.Value(ctypes.c_int, VIDEO_HOLD_UNSET)
            self._video_cmds[instance_id] = video_cmd
            self._video_states[instance_id] = video_state
            self._video_paths[instance_id] = video_path
            self._video_holds[instance_id] = video_hold
            extra.update(video_cmd=video_cmd, video_state=video_state,
                         video_path=video_path, video_hold=video_hold)
        elif config.get("source_type") == "signage":
            # Shared bit-flag channel for skip / playlist-reload commands
            signage_cmd = mp.Value(ctypes.c_int, 0)
            self._signage_cmds[instance_id] = signage_cmd
            extra.update(signage_cmd=signage_cmd)
        worker = NDIWorker(**config, heartbeat=heartbeat, **extra)
        process = mp.Process(
            target=worker_entry, args=(worker,),
            name=f"ndi-worker-{instance_id}", daemon=True,
        )
        process.start()

        self._workers[instance_id] = worker
        self._processes[instance_id] = process
        self._heartbeats[instance_id] = heartbeat
        return process

    def start_instance(
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
        preview_dir: Optional[str] = None,
        preview_interval: float = 2.0,
    ) -> bool:
        with self._lock:
            if instance_id in self._processes and self._processes[instance_id].is_alive():
                logger.warning(f"Instance {instance_id} already running")
                return False

            config = dict(
                instance_id=instance_id, ndi_name=ndi_name,
                source_type=source_type, source_value=source_value,
                width=width, height=height,
                capture_fps=capture_fps, output_fps=output_fps,
                refresh_interval=refresh_interval,
                browser_recycle_hours=browser_recycle_hours,
                text_settings=text_settings,
                video_settings=video_settings,
                signage_settings=signage_settings,
                preview_dir=preview_dir,
                preview_interval=preview_interval,
            )
            self._configs[instance_id] = config
            self._restart_meta.pop(instance_id, None)

            process = self._spawn(instance_id, config)
            log_event("INSTANCE_STARTED", f"id={instance_id} name='{ndi_name}' pid={process.pid}")

            self._ensure_watchdog()
            return True

    def stop_instance(self, instance_id: int) -> bool:
        with self._lock:
            worker = self._workers.get(instance_id)
            process = self._processes.get(instance_id)
            if not worker or not process:
                return False

            if process.is_alive():
                worker.stop()
                process.join(timeout=10)
                if process.is_alive():
                    logger.warning(f"Force killing instance {instance_id}")
                    process.terminate()
                    process.join(timeout=5)
                    if process.is_alive():
                        # Nuke the whole process group so the Playwright
                        # driver + Chromium tree can't outlive the worker
                        _kill_process_tree(process)
                        process.join(timeout=3)

            self._forget_instance(instance_id)
            self._configs.pop(instance_id, None)
            self._restart_meta.pop(instance_id, None)
            log_event("INSTANCE_STOPPED", f"id={instance_id}")
            return True

    def stop_all(self):
        ids = list(self._workers.keys())
        for iid in ids:
            self.stop_instance(iid)
        log_event("ALL_STOPPED", f"count={len(ids)}")

    def is_running(self, instance_id: int) -> bool:
        proc = self._processes.get(instance_id)
        return proc is not None and proc.is_alive()

    def get_running_ids(self) -> list:
        return [iid for iid, proc in self._processes.items() if proc.is_alive()]

    def boost_preview(self, instance_id: int, seconds: float = 6.0) -> bool:
        """Ask a running worker for larger, faster preview saves until
        `seconds` from now. Called repeatedly by the preview stream endpoint
        while a popup is connected; the deadline only ever extends, so
        overlapping viewers can't shorten each other's boost."""
        boost = self._preview_boosts.get(instance_id)
        if boost is None or not self.is_running(instance_id):
            return False
        until = time.monotonic() + seconds
        with boost.get_lock():
            if until > boost.value:
                boost.value = until
        return True

    def get_instance_health(self, instance_id: int) -> Optional[dict]:
        """Return health info for a running instance."""
        proc = self._processes.get(instance_id)
        hb = self._heartbeats.get(instance_id)
        if not proc:
            return None

        alive = proc.is_alive()
        last_hb = hb.value if hb else 0
        now = time.monotonic()
        stale = (now - last_hb) if last_hb > 0 else 0

        return {
            "alive": alive,
            "pid": proc.pid,
            "heartbeat_age_s": round(stale, 1),
            "healthy": alive and stale < HEARTBEAT_TIMEOUT,
        }

    # ------------------------------------------------------------------
    # Video playback control
    # ------------------------------------------------------------------

    _VIDEO_COMMANDS = {
        "play": VIDEO_CMD_PLAY,
        "stop": VIDEO_CMD_STOP,
        "load": VIDEO_CMD_LOAD,
        "load_play": VIDEO_CMD_LOAD_PLAY,
    }

    def video_command(self, instance_id: int, command: str,
                      path: Optional[str] = None,
                      hold: Optional[str] = None) -> bool:
        """Send a playback command to a running video worker.

        `path` (required for load/load_play) is the file to hot-swap to;
        `hold` ("first"/"last") optionally overrides the hold frame from
        this command onward. Returns False if the instance isn't a running
        video source or the command is malformed.

        The cmd Value's lock serializes the whole channel: path and hold are
        written before cmd under the same lock the worker reads them under.
        """
        cmd = self._VIDEO_COMMANDS.get(command)
        cmd_value = self._video_cmds.get(instance_id)
        if cmd is None or cmd_value is None or not self.is_running(instance_id):
            return False

        encoded = None
        if cmd in (VIDEO_CMD_LOAD, VIDEO_CMD_LOAD_PLAY):
            encoded = (path or "").encode("utf-8")
            # ctypes .value needs room for its NUL terminator
            if not encoded or len(encoded) >= VIDEO_PATH_MAX:
                logger.error(f"video_command: bad load path for instance {instance_id}")
                return False

        with cmd_value.get_lock():
            if encoded is not None:
                self._video_paths[instance_id].get_obj().value = encoded
            if hold in ("first", "last"):
                hold_value = self._video_holds.get(instance_id)
                if hold_value is not None:
                    hold_value.value = VIDEO_HOLD_FIRST if hold == "first" else VIDEO_HOLD_LAST
            cmd_value.value = cmd

        # Keep the stored config in sync so a watchdog restart resumes with
        # the swapped file / new hold instead of reverting to the original
        with self._lock:
            config = self._configs.get(instance_id)
            if config is not None:
                if encoded is not None:
                    config["source_value"] = path
                if hold in ("first", "last") and config.get("video_settings"):
                    config["video_settings"]["hold"] = hold
        log_event("VIDEO_COMMAND", f"id={instance_id} cmd={command}"
                  + (f" path={path}" if path else "")
                  + (f" hold={hold}" if hold else ""))
        return True

    # ------------------------------------------------------------------
    # Signage playlist control
    # ------------------------------------------------------------------

    _SIGNAGE_COMMANDS = {
        "skip": SIGNAGE_CMD_SKIP,
        "reload": SIGNAGE_CMD_RELOAD,
    }

    def signage_command(self, instance_id: int, command: str) -> bool:
        """Send a skip or playlist-reload command to a running signage worker.

        Commands are bit flags OR-ed into a shared value, so a skip and a
        reload arriving between two worker polls are both delivered."""
        bit = self._SIGNAGE_COMMANDS.get(command)
        cmd_value = self._signage_cmds.get(instance_id)
        if bit is None or cmd_value is None or not self.is_running(instance_id):
            return False
        with cmd_value.get_lock():
            cmd_value.value |= bit
        log_event("SIGNAGE_COMMAND", f"id={instance_id} cmd={command}")
        return True

    def get_ndi_stats(self, instance_id: int) -> Optional[dict]:
        """Receiver count + tally for a running worker, or None.

        receivers is None when the worker can't report it (dummy mode, or
        an ndi-python build without send_get_no_connections)."""
        conn = self._ndi_connections.get(instance_id)
        if conn is None or not self.is_running(instance_id):
            return None
        tally = self._ndi_tallys.get(instance_id)
        flags = tally.value if tally is not None else 0
        n = conn.value
        return {
            "receivers": n if n >= 0 else None,
            "on_program": bool(flags & TALLY_PROGRAM),
            "on_preview": bool(flags & TALLY_PREVIEW),
        }

    def get_receiver_endpoints(self, instance_id: int) -> Optional[dict]:
        """Socket-level receiver identification for a running worker: the
        peer IPs (and cached hostnames) of established TCP connections to
        the worker's NDI listening ports. None when not running.

        Each receiver also gets a measured TCP throughput (mbps) and an
        inferred transport: presence can't distinguish TCP media from
        UDP/multicast media (the control connection is TCP either way), but
        rate can — media over TCP moves megabits, a control-only connection
        idles. Rates come from bytes_acked deltas between calls, so the
        first call reports "measuring" and the next (the UI polls every 3s)
        carries numbers; "unknown" means no `ss` on this platform."""
        proc = self._processes.get(instance_id)
        if proc is None or not proc.is_alive() or proc.pid is None:
            return None
        info = _receiver_endpoints_for_pid(proc.pid)
        listen_ports = info.pop("_listen_ports", set())
        if not info.get("supported"):
            return info

        flows = _tcp_flow_bytes(listen_ports)
        now = time.monotonic()
        rate_by_ip: Dict[str, float] = {}
        if flows is not None:
            prev = self._rx_samples.get(instance_id, {})
            for key, acked in flows.items():
                p = prev.get(key)
                if p is not None and now > p[1] and acked >= p[0]:
                    bps = (acked - p[0]) * 8 / (now - p[1])
                    rate_by_ip[key[0]] = rate_by_ip.get(key[0], 0.0) + bps
            self._rx_samples[instance_id] = {k: (v, now) for k, v in flows.items()}

        for r in info["receivers"]:
            if flows is None:
                r["transport"], r["mbps"] = "unknown", None
            elif r["ip"] in rate_by_ip:
                mbps = rate_by_ip[r["ip"]] / 1e6
                r["mbps"] = round(mbps, 2)
                r["transport"] = ("tcp" if mbps >= RX_TCP_MEDIA_MBPS
                                  else "udp-multicast")
            else:
                r["transport"], r["mbps"] = "measuring", None
        return info

    def get_video_state(self, instance_id: int) -> Optional[str]:
        """Playback state of a running video worker, or None if not applicable."""
        state_value = self._video_states.get(instance_id)
        if state_value is None or not self.is_running(instance_id):
            return None
        return "playing" if state_value.value == VIDEO_STATE_PLAYING else "stopped"

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _ensure_watchdog(self):
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="worker-watchdog"
        )
        self._watchdog_thread.start()
        logger.info("Watchdog started")

    def _restart_instance(self, iid: int, reason: str):
        """Kill (if needed) and restart a worker from stored config."""
        with self._lock:
            config = self._configs.get(iid)
            if not config:
                return  # manually stopped while we were deciding — leave it

            log_event("INSTANCE_UNHEALTHY", f"id={iid} reason={reason}", level="warning")

            # Kill existing process
            proc = self._processes.get(iid)
            if proc and proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
                if proc.is_alive():
                    # Hung beyond SIGTERM — kill the whole group so the
                    # Playwright driver + Chromium tree die with the worker
                    _kill_process_tree(proc)
                    proc.join(timeout=3)

            # Clean up old refs (_spawn recreates control values as needed)
            self._forget_instance(iid)

            process = self._spawn(iid, config)
            log_event("INSTANCE_RESTARTED", f"id={iid} reason={reason} new_pid={process.pid}")

    def _next_restart_allowed(self, iid: int, now: float) -> bool:
        """Exponential backoff gate for watchdog restarts. A worker that
        keeps dying immediately (bad config, missing NDI, corrupt file)
        must not respawn — and relaunch Chromium — every 5s forever."""
        failures, last_restart = self._restart_meta.get(iid, (0, 0.0))
        if failures and now - last_restart >= RESTART_STABLE_RESET:
            # Survived long enough since the last restart — treat as recovered
            failures = 0
            self._restart_meta[iid] = (0, last_restart)
        delay = min(WATCHDOG_INTERVAL * (2 ** failures), RESTART_BACKOFF_MAX)
        if now - last_restart < delay:
            return False
        self._restart_meta[iid] = (failures + 1, now)
        if failures >= 3:
            log_event(
                "INSTANCE_RESTART_BACKOFF",
                f"id={iid} consecutive_failures={failures} next_delay={min(delay * 2, RESTART_BACKOFF_MAX):.0f}s",
                level="warning",
            )
        return True

    def _watchdog_loop(self):
        """
        Runs every WATCHDOG_INTERVAL seconds. Detects:
          1. Dead processes (crashed)
          2. Hung processes (alive but heartbeat stale)
        Restarts are rate-limited per instance with exponential backoff.
        """
        while not self._watchdog_stop.is_set():
            time.sleep(WATCHDOG_INTERVAL)

            now = time.monotonic()
            issues = []

            for iid in list(self._configs.keys()):
                proc = self._processes.get(iid)
                hb = self._heartbeats.get(iid)

                if proc is None or not proc.is_alive():
                    issues.append((iid, "crashed"))
                    continue

                # Check heartbeat staleness
                if hb is not None:
                    last_beat = hb.value
                    stale = now - last_beat
                    if stale > HEARTBEAT_TIMEOUT:
                        issues.append((iid, f"hung (heartbeat stale {stale:.0f}s)"))

            for iid, reason in issues:
                if self._next_restart_allowed(iid, now):
                    self._restart_instance(iid, reason)

            # If nothing tracked, stop watching
            if not self._configs:
                logger.info("Watchdog: no instances tracked, stopping")
                break

    def cleanup_dead(self):
        """Remove refs to dead processes not in configs (manually stopped)."""
        dead = [
            iid for iid, proc in self._processes.items()
            if not proc.is_alive() and iid not in self._configs
        ]
        for iid in dead:
            self._forget_instance(iid)
        return dead


manager = WorkerManager()
