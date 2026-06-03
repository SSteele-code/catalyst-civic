import os
import datetime
import json
from pathlib import Path

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

class PipelineLogger:
    def __init__(self, log_dir, script_name, script_version="1.0.0"):
        self.log_dir = Path(log_dir)
        self.script_name = script_name
        self.script_version = script_version
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{script_name.lower()}.log"

    def _get_system_metrics(self):
        """MED-003: Collect RAM/CPU metrics."""
        if not PSUTIL_AVAILABLE:
            return {}
        
        try:
            return {
                "cpu_percent": psutil.cpu_percent(),
                "ram_percent": psutil.virtual_memory().percent,
                "ram_available_mb": psutil.virtual_memory().available / (1024 * 1024)
            }
        except Exception:
            return {}

    def _write(self, severity, action, result, run_id=None, page_id=None, message="", extra=None):
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "script_name": self.script_name,
            "script_version": self.script_version,
            "run_id": run_id,
            "page_id": page_id,
            "action": action,
            "result": result,
            "severity": severity,
            "message": message,
            "system_metrics": self._get_system_metrics() # MED-008
        }
        if extra:
            entry.update(extra)
        
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        
        # Also print to console for visibility
        print(f"[{severity}] {self.script_name}: {action} - {result} | {message}")

    def info(self, action, result, run_id=None, page_id=None, message="", extra=None):
        self._write("INFO", action, result, run_id, page_id, message, extra)

    def warning(self, action, result, run_id=None, page_id=None, message="", extra=None):
        self._write("WARNING", action, result, run_id, page_id, message, extra)

    def error(self, action, result, run_id=None, page_id=None, message="", extra=None):
        self._write("ERROR", action, result, run_id, page_id, message, extra)
