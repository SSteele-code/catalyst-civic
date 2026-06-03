import json
import time
import random
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))


def _debug_boot_log(base_dir: Path, message: str, data: dict) -> None:
    payload = {
        "sessionId": "728be7",
        "runId": "bootstrap",
        "hypothesisId": "H7",
        "location": "scripts/run_state_machine_service.py:22",
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
        "id": f"log_{int(time.time() * 1000)}_{random.randint(1000, 9999)}",
    }
    with (base_dir / "debug-728be7.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def main():
    try:
        from src.state_machine import DropHandshakeService, create_http_handler
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", "") or str(exc)
        print(f"[parser-startup] Missing Python dependency: {missing}")
        print("[parser-startup] Install parser deps with:")
        print(f"  \"{sys.executable}\" -m pip install -r \"{BASE_DIR / 'requirements.txt'}\"")
        raise

    # region agent log
    _debug_boot_log(
        BASE_DIR,
        "State machine boot script entered",
        {"base_dir": str(BASE_DIR), "script_path": str(Path(__file__).resolve())},
    )
    # endregion
    service = DropHandshakeService(BASE_DIR)
    server = ThreadingHTTPServer((service.host, service.port), create_http_handler(service))
    print(f"State-machine service listening on http://{service.host}:{service.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
