"""Resident state-machine runtime for the PDF Parser rewrite."""

import json
import random
import time
from pathlib import Path

# region agent log
try:
    _base_dir = Path(__file__).resolve().parents[2]
    _payload = {
        "sessionId": "728be7",
        "runId": "bootstrap",
        "hypothesisId": "H10",
        "location": "src/state_machine/__init__.py:6",
        "message": "state_machine package import executed",
        "data": {"base_dir": str(_base_dir)},
        "timestamp": int(time.time() * 1000),
        "id": f"log_{int(time.time() * 1000)}_{random.randint(1000, 9999)}",
    }
    with (_base_dir / "debug-728be7.log").open("a", encoding="utf-8") as _handle:
        _handle.write(json.dumps(_payload, ensure_ascii=True) + "\n")
except Exception:
    pass
# endregion

from .service import DropHandshakeService, create_http_handler

__all__ = ["DropHandshakeService", "create_http_handler"]
