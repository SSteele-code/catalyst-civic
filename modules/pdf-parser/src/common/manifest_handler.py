import os
import json
import threading
import tempfile
import shutil
import time
from pathlib import Path

# Platform-specific locking
try:
    import msvcrt
    def lock_file(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    def unlock_file(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
except ImportError:
    try:
        import fcntl
        def lock_file(f):
            fcntl.flock(f, fcntl.LOCK_EX)
        def unlock_file(f):
            fcntl.flock(f, fcntl.LOCK_UN)
    except ImportError:
        # Fallback for systems without flock
        def lock_file(f): pass
        def unlock_file(f): pass

class ManifestHandler:
    def __init__(self, manifest_dir):
        self.manifest_dir = Path(manifest_dir)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir = self.manifest_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()

    def _atomic_write(self, path, data):
        """Write JSON atomically using a temp file and maintain backups."""
        # 1. Prepare Backup (.bak)
        if path.exists():
            bak_path = path.with_suffix(".bak")
            shutil.copy2(path, bak_path)
            
            # 2. Prepare History Snapshot (timestamped)
            run_id = path.stem
            timestamp = int(time.time())
            snap_dir = self.history_dir / run_id
            snap_dir.mkdir(parents=True, exist_ok=True)
            snap_path = snap_dir / f"{run_id}_v{timestamp}.json"
            shutil.copy2(path, snap_path)

        # 3. Atomic Write
        fd, temp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, indent=4)
            shutil.move(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def load(self, run_id):
        path = self.manifest_dir / f"{run_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Manifest not found for Run ID: {run_id}")
        
        with open(path, "r") as f:
            return json.load(f)

    def save(self, run_id, manifest_data):
        path = self.manifest_dir / f"{run_id}.json"
        with self._thread_lock:
            self._atomic_write(path, manifest_data)

    def _deep_merge(self, base, updates):
        """Recursively merge nested dictionaries while replacing scalar values."""
        if not isinstance(base, dict) or not isinstance(updates, dict):
            return updates

        merged = dict(base)
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def update(self, run_id, updates):
        """Perform an atomic update with file-level locking for concurrency safety."""
        path = self.manifest_dir / f"{run_id}.json"
        lock_path = path.with_suffix(".lock")
        
        with self._thread_lock:
            # Multi-process locking
            with open(lock_path, "w") as lf:
                lock_file(lock_lf := lf) # Placeholder for the lock context
                try:
                    # Read-Modify-Write
                    if path.exists():
                        with open(path, "r") as f:
                            manifest = json.load(f)
                    else:
                        manifest = {}
                        
                    manifest = self._deep_merge(manifest, updates)
                    self._atomic_write(path, manifest)
                finally:
                    unlock_file(lf)
        return manifest
