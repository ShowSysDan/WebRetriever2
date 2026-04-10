"""
Worker Manager — manages lifecycle of NDI worker processes.

Features:
  - Start/stop individual instances or all at once
  - Crash recovery: watchdog detects dead processes and restarts them
  - Hang detection: shared heartbeat values let the watchdog detect workers
    that are alive but stuck (e.g. Playwright blocked on an unresponsive page)
  - Configurable browser recycling interval passed to each worker
"""

import time
import ctypes
import logging
import threading
import multiprocessing as mp
from typing import Dict, Optional

from app.workers.ndi_worker import NDIWorker, worker_entry, HEARTBEAT_TIMEOUT
from app.logging_config import log_event

logger = logging.getLogger(__name__)

# How often the watchdog checks (seconds)
WATCHDOG_INTERVAL = 5

# Default browser recycle interval (hours)
DEFAULT_RECYCLE_HOURS = 4


class WorkerManager:
    def __init__(self):
        self._workers: Dict[int, NDIWorker] = {}
        self._processes: Dict[int, mp.Process] = {}
        self._heartbeats: Dict[int, mp.Value] = {}
        self._configs: Dict[int, dict] = {}
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()

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
        preview_dir: Optional[str] = None,
        preview_interval: float = 2.0,
    ) -> bool:
        if instance_id in self._processes and self._processes[instance_id].is_alive():
            logger.warning(f"Instance {instance_id} already running")
            return False

        # Create shared heartbeat value (double precision monotonic timestamp)
        heartbeat = mp.Value(ctypes.c_double, time.monotonic())

        config = dict(
            instance_id=instance_id, ndi_name=ndi_name,
            source_type=source_type, source_value=source_value,
            width=width, height=height,
            capture_fps=capture_fps, output_fps=output_fps,
            refresh_interval=refresh_interval,
            browser_recycle_hours=browser_recycle_hours,
            text_settings=text_settings,
            preview_dir=preview_dir,
            preview_interval=preview_interval,
        )
        self._configs[instance_id] = config

        worker = NDIWorker(**config, heartbeat=heartbeat)
        process = mp.Process(
            target=worker_entry, args=(worker,),
            name=f"ndi-worker-{instance_id}", daemon=True,
        )
        process.start()

        self._workers[instance_id] = worker
        self._processes[instance_id] = process
        self._heartbeats[instance_id] = heartbeat
        log_event("INSTANCE_STARTED", f"id={instance_id} name='{ndi_name}' pid={process.pid}")

        self._ensure_watchdog()
        return True

    def stop_instance(self, instance_id: int) -> bool:
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
                    process.kill()
                    process.join(timeout=3)

        self._workers.pop(instance_id, None)
        self._processes.pop(instance_id, None)
        self._heartbeats.pop(instance_id, None)
        self._configs.pop(instance_id, None)
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
        config = self._configs.get(iid)
        if not config:
            return

        log_event("INSTANCE_UNHEALTHY", f"id={iid} reason={reason}", level="warning")

        # Kill existing process
        proc = self._processes.get(iid)
        if proc and proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=3)

        # Clean up old refs
        self._workers.pop(iid, None)
        self._processes.pop(iid, None)
        old_hb = self._heartbeats.pop(iid, None)

        # Fresh heartbeat
        heartbeat = mp.Value(ctypes.c_double, time.monotonic())

        worker = NDIWorker(**config, heartbeat=heartbeat)
        process = mp.Process(
            target=worker_entry, args=(worker,),
            name=f"ndi-worker-{iid}", daemon=True,
        )
        process.start()

        self._workers[iid] = worker
        self._processes[iid] = process
        self._heartbeats[iid] = heartbeat
        log_event("INSTANCE_RESTARTED", f"id={iid} reason={reason} new_pid={process.pid}")

    def _watchdog_loop(self):
        """
        Runs every WATCHDOG_INTERVAL seconds. Detects:
          1. Dead processes (crashed)
          2. Hung processes (alive but heartbeat stale)
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
            self._workers.pop(iid, None)
            self._processes.pop(iid, None)
            self._heartbeats.pop(iid, None)
        return dead


manager = WorkerManager()
