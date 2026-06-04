from __future__ import annotations

import datetime
import json
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.qa_engine.config import get_service_settings, load_thresholds
from src.qa_engine.janitor import run_parser_janitor_cleanup
from src.qa_engine.performance import RunPerformanceMonitor
from src.qa_engine.processor import QAInputDescriptor, load_json, run_qa_assessment
from src.qa_engine.validation import ensure_json_file, validate_run_id


SERVICE_STATE_BOOTING = "booting"
SERVICE_STATE_READY = "ready"
SERVICE_STATE_PROCESSING = "processing"

RUN_STATE_HANDSHAKE_RECEIVED = "handshake_received"
RUN_STATE_STAGED = "staged"
RUN_STATE_ASSESSED = "assessed"
RUN_STATE_JANITOR_COMPLETED = "janitor_completed"
RUN_STATE_COMPLETED = "completed"
RUN_STATE_FAILED = "failed"


def generate_run_id() -> str:
    now = datetime.datetime.now()
    return validate_run_id(f"QA_{now.strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:4].upper()}")


def _infer_parser_root(parser_output_root: Path) -> Path:
    # Typical layout: <parser_root>/outbox/<run_folder>
    if parser_output_root.parent.name.lower() == "outbox":
        return parser_output_root.parent.parent.resolve()
    return parser_output_root.parent.resolve()


def _resolve_parser_output_folder(job_folder: Path, payload: dict, job_payload: dict) -> Path:
    parser_output_raw = (
        job_payload.get("parser_output_folder")
        or payload.get("parser_output_folder")
        or payload.get("run_output_folder")
        or ""
    )
    if parser_output_raw:
        parser_output_root = Path(str(parser_output_raw)).resolve()
    elif (job_folder / "machine_readable" / "run.json").exists():
        parser_output_root = job_folder
    else:
        # If the provided folder is an inbox folder, allow a single parser output subfolder.
        candidates = sorted(
            [path for path in job_folder.iterdir() if path.is_dir() and (path / "machine_readable" / "run.json").exists()]
        )
        if len(candidates) == 1:
            parser_output_root = candidates[0]
        else:
            raise ValueError("Could not resolve parser output folder from job folder.")
    if not parser_output_root.exists() or not parser_output_root.is_dir():
        raise ValueError(f"Parser output folder not found: {parser_output_root}")
    if not (parser_output_root / "machine_readable" / "run.json").exists():
        raise ValueError(f"Parser output folder missing machine_readable/run.json: {parser_output_root}")
    return parser_output_root


def _resolve_source_pdf_path(
    parser_root: Path,
    parser_run_id: str,
    payload: dict,
    job_payload: dict,
) -> Path | None:
    source_raw = job_payload.get("source_pdf_path") or payload.get("source_pdf_path") or ""
    if source_raw:
        source_path = Path(str(source_raw)).resolve()
        if source_path.exists() and source_path.is_file():
            return source_path

    manifest_run_path = parser_root / "manifests" / "runs" / f"{parser_run_id}.json"
    if manifest_run_path.exists():
        manifest_payload = load_json(manifest_run_path)
        source_drop_path = str(manifest_payload.get("source_drop_path") or "").strip()
        if source_drop_path:
            candidate = Path(source_drop_path).resolve()
            if candidate.exists() and candidate.is_file():
                return candidate

    fallback = parser_root / "ORIGINAL_SOURCE.pdf"
    if fallback.exists() and fallback.is_file():
        return fallback.resolve()
    return None


def load_job_descriptor(payload: dict) -> QAInputDescriptor:
    job_folder_raw = payload.get("job_folder")
    if not job_folder_raw:
        raise ValueError("Handshake requires job_folder.")
    job_folder = Path(str(job_folder_raw)).resolve()
    if not job_folder.exists() or not job_folder.is_dir():
        raise ValueError(f"Job folder not found: {job_folder}")

    job_json_path = job_folder / "qa_job.json"
    job_payload = {}
    if job_json_path.exists():
        ensure_json_file(job_json_path, "qa_job.json")
        job_payload = load_json(job_json_path)

    parser_output_root = _resolve_parser_output_folder(job_folder, payload, job_payload)
    parser_machine_readable_folder = parser_output_root / "machine_readable"
    parser_run_json = load_json(parser_machine_readable_folder / "run.json")
    parser_run_id = str(parser_run_json.get("run_id") or "").strip()
    if not parser_run_id:
        raise ValueError("Parser run id not found in machine_readable/run.json")

    parser_root_raw = job_payload.get("parser_root") or payload.get("parser_root") or ""
    parser_root = Path(str(parser_root_raw)).resolve() if parser_root_raw else _infer_parser_root(parser_output_root)
    source_pdf_path = _resolve_source_pdf_path(parser_root, parser_run_id, payload, job_payload)

    return QAInputDescriptor(
        job_folder=job_folder,
        parser_root=parser_root,
        parser_output_root=parser_output_root,
        parser_machine_readable_folder=parser_machine_readable_folder,
        parser_run_id=parser_run_id,
        source_pdf_path=source_pdf_path,
        source_pdf_hash=parser_run_json.get("source_pdf_hash"),
    )


class QAHandshakeService:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir or BASE_DIR)
        self.thresholds = load_thresholds(self.base_dir)
        self.service_settings = get_service_settings(self.thresholds)
        self.run_manifest_handler = ManifestHandler(self.base_dir / "manifests" / "runs")
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=int(self.service_settings.get("max_concurrent_runs", 1)))
        self._jobs: dict[str, dict] = {}
        self._active_runs: set[str] = set()
        self._service_state = SERVICE_STATE_BOOTING
        self.logger = PipelineLogger(self.base_dir / "logs" / "service", "qa_state_machine_service")
        self._set_service_state(SERVICE_STATE_READY)

    @property
    def host(self) -> str:
        return str(self.service_settings.get("host", "127.0.0.1"))

    @property
    def port(self) -> int:
        return int(self.service_settings.get("port", 8093))

    def _set_service_state(self, state: str) -> None:
        with self._lock:
            self._service_state = state
        self.logger.info("SERVICE_STATE", "SUCCESS", message=state)

    def health_payload(self) -> dict:
        with self._lock:
            return {
                "service_state": self._service_state,
                "active_runs": sorted(self._active_runs),
                "known_runs": sorted(self._jobs),
            }

    def run_payload(self, run_id: str) -> dict:
        with self._lock:
            payload = dict(self._jobs.get(run_id, {}))
        manifest_path = self.base_dir / "manifests" / "runs" / f"{run_id}.json"
        if manifest_path.exists():
            payload["manifest"] = load_json(manifest_path)
        return payload

    def submit_handshake(self, payload: dict) -> dict:
        descriptor = load_job_descriptor(payload)
        run_id = generate_run_id()
        with self._lock:
            self._jobs[run_id] = {
                "run_id": run_id,
                "status": "accepted",
                "job_folder": str(descriptor.job_folder),
                "parser_run_id": descriptor.parser_run_id,
                "parser_output_root": str(descriptor.parser_output_root),
                "submitted_at": datetime.datetime.now().isoformat(),
            }
        self._executor.submit(self._process_handshake, run_id, descriptor)
        self.logger.info("HANDSHAKE_ACCEPTED", "SUCCESS", run_id=run_id, message=str(descriptor.job_folder))
        return {"run_id": run_id, "status": "accepted"}

    def _append_state_history(self, run_id: str, state: str, message: str = "") -> None:
        manifest = self.run_manifest_handler.load(run_id)
        history = manifest.get("state_history", [])
        history.append(
            {
                "state": state,
                "timestamp": datetime.datetime.now().isoformat(),
                "message": message,
            }
        )
        self.run_manifest_handler.update(
            run_id,
            {
                "current_state": state,
                "state_history": history,
                "status": state,
            },
        )

    def _create_run_manifest(self, run_id: str, descriptor: QAInputDescriptor) -> None:
        manifest = {
            "qa_run_id": run_id,
            "parser_run_id": descriptor.parser_run_id,
            "job_folder": str(descriptor.job_folder),
            "parser_root": str(descriptor.parser_root),
            "parser_output_root": str(descriptor.parser_output_root),
            "parser_machine_readable_folder": str(descriptor.parser_machine_readable_folder),
            "source_pdf_path": str(descriptor.source_pdf_path) if descriptor.source_pdf_path else None,
            "source_pdf_hash": descriptor.source_pdf_hash,
            "created_at": datetime.datetime.now().isoformat(),
            "status": RUN_STATE_HANDSHAKE_RECEIVED,
            "current_state": RUN_STATE_HANDSHAKE_RECEIVED,
            "state_history": [
                {
                    "state": RUN_STATE_HANDSHAKE_RECEIVED,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "message": "handshake accepted",
                }
            ],
        }
        self.run_manifest_handler.save(run_id, manifest)

    def _sync_runtime_metrics(self, result: dict, runtime_metrics: dict) -> None:
        machine_root = Path(str(result.get("machine_readable_folder") or ""))
        if not machine_root.exists():
            return
        for relative in ["run.json", "qa_run.json", "handoff.json"]:
            target = machine_root / relative
            if not target.exists():
                continue
            payload = load_json(target)
            payload["qa_runtime_metrics"] = runtime_metrics
            write_path = target
            write_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _run_pass_janitor(self, run_id: str, descriptor: QAInputDescriptor) -> dict | None:
        janitor_settings = dict(self.thresholds.get("janitor", {}))
        if not janitor_settings:
            return None
        report_path = self.base_dir / "reports" / f"{run_id}_janitor.json"
        summary = run_parser_janitor_cleanup(
            parser_root=descriptor.parser_root,
            janitor_settings=janitor_settings,
            explicit_keep_outbox=descriptor.parser_output_root,
            report_path=report_path,
        )
        return summary.as_dict()

    def _process_handshake(self, run_id: str, descriptor: QAInputDescriptor) -> None:
        monitor = RunPerformanceMonitor(
            self.base_dir,
            sample_interval_seconds=float(self.service_settings.get("performance_sample_interval_seconds", 0.5)),
        )
        result: dict | None = None
        with self._lock:
            self._active_runs.add(run_id)
            self._service_state = SERVICE_STATE_PROCESSING

        try:
            self._create_run_manifest(run_id, descriptor)
            self._append_state_history(run_id, RUN_STATE_STAGED, "qa run staged")
            monitor.start()

            result = run_qa_assessment(
                base_dir=self.base_dir,
                qa_run_id=run_id,
                descriptor=descriptor,
                thresholds=self.thresholds,
                logger=self.logger,
            )
            self._append_state_history(run_id, RUN_STATE_ASSESSED, f"qa assessment complete: {result.get('status')}")
            self.run_manifest_handler.update(
                run_id,
                {
                    "qa_result": result,
                    "status": RUN_STATE_ASSESSED,
                    "current_state": RUN_STATE_ASSESSED,
                },
            )

            janitor_summary = None
            janitor_enabled = bool(self.service_settings.get("janitor_on_pass", True))
            if janitor_enabled and str(result.get("status")) == "pass":
                janitor_summary = self._run_pass_janitor(run_id, descriptor)
                self._append_state_history(run_id, RUN_STATE_JANITOR_COMPLETED, "parser runtime janitor completed")
                self.run_manifest_handler.update(run_id, {"janitor": janitor_summary})

            self._append_state_history(run_id, RUN_STATE_COMPLETED, "qa run completed")
            self.run_manifest_handler.update(
                run_id,
                {
                    "status": RUN_STATE_COMPLETED,
                    "current_state": RUN_STATE_COMPLETED,
                },
            )
            with self._lock:
                self._jobs[run_id]["status"] = "completed"
                self._jobs[run_id]["result"] = result
                if janitor_summary is not None:
                    self._jobs[run_id]["janitor"] = janitor_summary
        except Exception as exc:
            reason = f"{exc}\n{traceback.format_exc()}"
            self.logger.error("QA_RUN_FAILED", "FAILURE", run_id=run_id, message=str(exc))
            try:
                self._append_state_history(run_id, RUN_STATE_FAILED, str(exc))
            except Exception:
                pass
            self.run_manifest_handler.update(
                run_id,
                {
                    "status": RUN_STATE_FAILED,
                    "current_state": RUN_STATE_FAILED,
                    "failure_reason": reason,
                },
            )
            with self._lock:
                self._jobs[run_id]["status"] = "failed"
                self._jobs[run_id]["error"] = str(exc)
        finally:
            page_count = 0
            if result and isinstance(result.get("summary"), dict):
                page_count = int(result["summary"].get("page_count") or 0)
            runtime_metrics = monitor.stop(page_count=page_count)
            self.run_manifest_handler.update(run_id, {"runtime_metrics": runtime_metrics})
            if result:
                self._sync_runtime_metrics(result, runtime_metrics)
                with self._lock:
                    if run_id in self._jobs:
                        self._jobs[run_id]["runtime_metrics"] = runtime_metrics

            with self._lock:
                self._active_runs.discard(run_id)
                self._service_state = SERVICE_STATE_READY if not self._active_runs else SERVICE_STATE_PROCESSING


def create_http_handler(service: QAHandshakeService):
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length) if content_length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(service.health_payload())
                return
            if self.path.startswith("/runs/"):
                run_id = self.path.split("/", 2)[2]
                payload = service.run_payload(run_id)
                if not payload:
                    self._send_json({"error": "run not found"}, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_json(payload)
                return
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/handshake/start":
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                return
            try:
                payload = self._read_json()
                response = service.submit_handshake(payload)
                self._send_json(response, status=HTTPStatus.ACCEPTED)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args) -> None:
            service.logger.info("HTTP", "SUCCESS", message=format % args)

    return Handler
