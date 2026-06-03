from __future__ import annotations

import datetime
import os
import platform
import threading
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - optional fallback
    psutil = None

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


class RunPerformanceMonitor:
    def __init__(self, base_dir: Path, sample_interval_seconds: float = 0.5):
        self.base_dir = Path(base_dir)
        self.sample_interval_seconds = max(0.2, float(sample_interval_seconds))
        self.started_at = datetime.datetime.now()
        self._started_perf = time.perf_counter()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False

        self._process = psutil.Process() if psutil else None
        self._sample_count = 0
        self._peak_process_rss_mb = 0.0
        self._process_rss_sum_mb = 0.0
        self._peak_process_cpu_percent = 0.0
        self._process_cpu_sum = 0.0
        self._peak_thread_count = 0
        self._peak_system_cpu_percent = 0.0
        self._system_cpu_sum = 0.0
        self._min_available_memory_mb: float | None = None
        self._available_memory_sum_mb = 0.0
        self._host_snapshot = self._build_host_snapshot()

    def _build_host_snapshot(self) -> dict:
        logical_cpus = os.cpu_count()
        physical_cpus = psutil.cpu_count(logical=False) if psutil else None
        cpu_model = None
        cpu_freq_mhz = None
        total_memory_mb = None
        disk_total_gb = None
        disk_free_gb = None

        if winreg:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                ) as key:
                    cpu_model = cpu_model or winreg.QueryValueEx(key, "ProcessorNameString")[0]
            except OSError:
                pass
        if not cpu_model:
            cpu_model = platform.processor() or None

        if psutil:
            try:
                cpu_freq = psutil.cpu_freq()
                if cpu_freq:
                    cpu_freq_mhz = cpu_freq.max or cpu_freq.current
            except Exception:
                pass
            try:
                total_memory_mb = psutil.virtual_memory().total / (1024 * 1024)
            except Exception:
                pass
            try:
                disk_usage = psutil.disk_usage(str(self.base_dir.anchor or self.base_dir))
                disk_total_gb = disk_usage.total / (1024 * 1024 * 1024)
                disk_free_gb = disk_usage.free / (1024 * 1024 * 1024)
            except Exception:
                pass

        return {
            "cpu_model": cpu_model,
            "logical_cpus": logical_cpus,
            "physical_cpus": physical_cpus,
            "cpu_frequency_mhz": _round(cpu_freq_mhz),
            "total_memory_mb": _round(total_memory_mb),
            "disk_total_gb": _round(disk_total_gb),
            "disk_free_gb": _round(disk_free_gb),
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        if psutil and self._process:
            try:
                self._process.cpu_percent(None)
                psutil.cpu_percent(None)
            except Exception:
                pass
        self._thread = threading.Thread(target=self._sample_loop, name="run-perf-monitor", daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self.sample_interval_seconds):
            self.sample_once()

    def sample_once(self) -> None:
        if not psutil or not self._process:
            return
        try:
            process_rss_mb = self._process.memory_info().rss / (1024 * 1024)
            process_cpu_percent = self._process.cpu_percent(None)
            thread_count = self._process.num_threads()
            system_cpu_percent = psutil.cpu_percent(None)
            available_memory_mb = psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            return

        with self._lock:
            self._sample_count += 1
            self._peak_process_rss_mb = max(self._peak_process_rss_mb, process_rss_mb)
            self._process_rss_sum_mb += process_rss_mb
            self._peak_process_cpu_percent = max(self._peak_process_cpu_percent, process_cpu_percent)
            self._process_cpu_sum += process_cpu_percent
            self._peak_thread_count = max(self._peak_thread_count, thread_count)
            self._peak_system_cpu_percent = max(self._peak_system_cpu_percent, system_cpu_percent)
            self._system_cpu_sum += system_cpu_percent
            self._available_memory_sum_mb += available_memory_mb
            if self._min_available_memory_mb is None:
                self._min_available_memory_mb = available_memory_mb
            else:
                self._min_available_memory_mb = min(self._min_available_memory_mb, available_memory_mb)

    def stop(self, page_count: int = 0) -> dict:
        if not self._stopped:
            self._stop_event.set()
            if self._thread is not None:
                self._thread.join(timeout=self.sample_interval_seconds * 2)
            self.sample_once()
            self._stopped = True

        completed_at = datetime.datetime.now()
        duration_seconds = max(0.0, time.perf_counter() - self._started_perf)
        with self._lock:
            sample_count = self._sample_count
            avg_process_rss_mb = (self._process_rss_sum_mb / sample_count) if sample_count else None
            avg_process_cpu_percent = (self._process_cpu_sum / sample_count) if sample_count else None
            avg_system_cpu_percent = (self._system_cpu_sum / sample_count) if sample_count else None
            avg_available_memory_mb = (self._available_memory_sum_mb / sample_count) if sample_count else None

        pages_per_minute = (page_count / (duration_seconds / 60.0)) if duration_seconds > 0 and page_count else None
        seconds_per_page = (duration_seconds / page_count) if duration_seconds > 0 and page_count else None

        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "sample_interval_seconds": self.sample_interval_seconds,
            "sample_count": sample_count,
            "duration_seconds": _round(duration_seconds),
            "pages_per_minute": _round(pages_per_minute),
            "seconds_per_page": _round(seconds_per_page),
            "host": self._host_snapshot,
            "process": {
                "peak_rss_mb": _round(self._peak_process_rss_mb),
                "avg_rss_mb": _round(avg_process_rss_mb),
                "peak_cpu_percent": _round(self._peak_process_cpu_percent),
                "avg_cpu_percent": _round(avg_process_cpu_percent),
                "peak_thread_count": self._peak_thread_count or None,
            },
            "system": {
                "peak_cpu_percent": _round(self._peak_system_cpu_percent),
                "avg_cpu_percent": _round(avg_system_cpu_percent),
                "min_available_memory_mb": _round(self._min_available_memory_mb),
                "avg_available_memory_mb": _round(avg_available_memory_mb),
            },
        }
