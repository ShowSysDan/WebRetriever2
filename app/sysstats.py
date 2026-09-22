"""Host health for the dashboard: CPU load (per core + average), memory, and
free space on the media drive.

CPU is sampled by one background thread rather than per request:
psutil.cpu_percent() measures the interval since its previous call, so
letting each browser poll drive it would give every viewer a different
(and, with several tabs open, meaninglessly short) window. The thread also
keeps a rolling history, so the dashboard graph is full the moment a page
opens and every viewer sees the same curve.
"""

import shutil
import threading
import time
from collections import deque

SAMPLE_INTERVAL_S = 2.0
HISTORY_SAMPLES = 150  # 5 minutes at 2s

_lock = threading.Lock()
_started = False
_history = deque(maxlen=HISTORY_SAMPLES)  # (epoch_ms, avg_percent)
_latest = {"per_core": None, "avg": None}


def _sampler():
    import psutil
    psutil.cpu_percent(percpu=True)  # prime: the first reading is meaningless
    while True:
        time.sleep(SAMPLE_INTERVAL_S)
        try:
            per_core = psutil.cpu_percent(percpu=True)
        except Exception:
            continue
        avg = round(sum(per_core) / len(per_core), 1) if per_core else None
        with _lock:
            _latest["per_core"] = [round(p, 1) for p in per_core]
            _latest["avg"] = avg
            _history.append((int(time.time() * 1000), avg))


def ensure_sampler():
    """Start the CPU sampler once per process (lazily, from the first
    request — worker processes never import the routes, so they never
    start one). No-op without psutil."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    try:
        import psutil  # noqa: F401
    except ImportError:
        return
    threading.Thread(target=_sampler, name="sysstats-cpu", daemon=True).start()


def snapshot(disk_path):
    """Current host stats. Every section degrades to null independently
    (no psutil, sampler still warming up, unreadable path)."""
    ensure_sampler()
    with _lock:
        cpu = {
            "avg": _latest["avg"],
            "per_core": _latest["per_core"],
            "history": list(_history),
            "interval_s": SAMPLE_INTERVAL_S,
        }
    memory = None
    try:
        import psutil
        cpu["cores_logical"] = psutil.cpu_count(logical=True)
        cpu["cores_physical"] = psutil.cpu_count(logical=False)
        try:
            cpu["load_avg"] = [round(x, 2) for x in psutil.getloadavg()]
        except (AttributeError, OSError):
            cpu["load_avg"] = None
        vm = psutil.virtual_memory()
        memory = {"total": vm.total, "used": vm.total - vm.available,
                  "percent": vm.percent}
    except ImportError:
        pass
    disk = None
    try:
        du = shutil.disk_usage(disk_path)
        # Percent the way df reports it: of the space usable by the app
        # (used + free), not the raw total, which includes root-reserved
        # blocks the upload folder can never fill
        usable = du.used + du.free
        disk = {"path": disk_path, "total": du.total, "used": du.used,
                "free": du.free,
                "percent": round(du.used / usable * 100, 1) if usable else None}
    except OSError:
        pass
    return {"cpu": cpu, "memory": memory, "disk": disk}
