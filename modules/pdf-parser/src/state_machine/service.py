from __future__ import annotations

import datetime
import json
import shutil
import sys
import threading
import time
import traceback
import uuid
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from scripts.workers.detect.detect_native_text_presence import main as detect_native_text_main
from scripts.workers.detect.score_native_text_quality import main as score_native_text_quality_main
from scripts.workers.extract.extract_native_text import main as extract_native_text_main
from scripts.workers.extract.run_text_ocr import main as run_text_ocr_main
from scripts.workers.ingest.hash_input_pdf import main as hash_input_pdf_main
from scripts.workers.layout.detect_handwriting import main as detect_handwriting_main
from scripts.workers.layout.detect_table_regions import main as detect_table_regions_main
from scripts.workers.layout.detect_text_regions import main as detect_text_regions_main
from scripts.workers.preprocess.correct_skew import main as correct_skew_main
from scripts.workers.preprocess.detect_skew import main as detect_skew_main
from scripts.workers.render.render_pdf_page_to_image import main as render_pdf_page_to_image_main
from scripts.workers.route.select_extraction_route import main as select_extraction_route_main
from scripts.workers.split.register_split_pages import main as register_split_pages_main
from scripts.workers.split.split_pdf_to_single_page_pdfs import main as split_pdf_to_single_page_pdfs_main
from src.common.constants import (
    QUARANTINE_AUTHORITY,
    RUN_STATE_COMPLETED,
    RUN_STATE_DROP_VERIFIED,
    RUN_STATE_EXTRACTED,
    RUN_STATE_FAILED,
    RUN_STATE_FEATURES_COMPUTED,
    RUN_STATE_GEOMETRY_NORMALIZED,
    RUN_STATE_HANDOFF_READY,
    RUN_STATE_HANDSHAKE_RECEIVED,
    RUN_STATE_PACKAGED,
    RUN_STATE_PREPARED,
    RUN_STATE_SPLIT,
    RUN_STATE_TYPED,
    SERVICE_STATE_BOOTING,
    SERVICE_STATE_PROCESSING,
    SERVICE_STATE_READY,
)
from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.validation import validate_filename, validate_pdf, validate_provenance_filename, validate_run_id
from src.review_engine.service import dispatch_review_engine_handshake
from src.state_machine.config import get_service_settings, load_thresholds
from src.state_machine.extractors import run_page_type_extraction
from src.state_machine.packager import package_run
from src.state_machine.page_feature_pipeline import run_page_feature_pipeline, run_page_geometry_normalization
from src.state_machine.page_typer import run_page_typing
from src.state_machine.performance import RunPerformanceMonitor


@dataclass
class JobDescriptor:
    job_folder: Path
    source_path: Path
    source_file: str
    job_id: str
    profile: str
    intake_path: Path | None
    source_original_name: str | None
    source_alias_name: str | None


def generate_run_id() -> str:
    now = datetime.datetime.now()
    run_id = f"RUN_{now.strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:4].upper()}"
    return validate_run_id(run_id)


def load_job_descriptor(job_folder_raw: str) -> JobDescriptor:
    job_folder = Path(job_folder_raw).resolve()
    if not job_folder.exists() or not job_folder.is_dir():
        raise ValueError(f"Job folder not found: {job_folder}")

    intake_path = job_folder / "intake.json"
    if intake_path.exists():
        with open(intake_path, "r", encoding="utf-8") as f:
            intake = json.load(f)
        source_file = validate_filename(intake.get("source_file", ""))
        job_id = str(intake.get("job_id") or job_folder.name)
        profile = str(intake.get("profile") or "default")
        original_name_raw = intake.get("source_original_name") or intake.get("original_filename") or intake.get("source_display_name")
        source_original_name = validate_provenance_filename(original_name_raw) if original_name_raw else None
        alias_name_raw = intake.get("source_alias_name") or intake.get("source_alias") or intake.get("source_label")
        source_alias_name = str(alias_name_raw).strip() if alias_name_raw else job_id
    else:
        pdfs = sorted(path for path in job_folder.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")
        if len(pdfs) != 1:
            raise ValueError("Job folder must contain exactly one PDF when intake.json is absent.")
        source_file = validate_filename(pdfs[0].name)
        job_id = job_folder.name
        profile = "default"
        intake_path = None
        source_original_name = None
        source_alias_name = job_id

    source_path = job_folder / source_file
    if not source_path.exists():
        raise ValueError(f"Source PDF missing from job folder: {source_path}")

    return JobDescriptor(
        job_folder=job_folder,
        source_path=source_path,
        source_file=source_file,
        job_id=job_id,
        profile=profile,
        intake_path=intake_path,
        source_original_name=source_original_name,
        source_alias_name=source_alias_name,
    )


# Resident service: boots once, accepts folder-drop jobs via HTTP POST /handshake/start,
# and runs each through the 10-state processing pipeline in a background thread pool.
class DropHandshakeService:
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
        self.logger = PipelineLogger(self.base_dir / "logs" / "service", "state_machine_service")
        self._set_service_state(SERVICE_STATE_READY)

    @property
    def host(self) -> str:
        return str(self.service_settings.get("host", "127.0.0.1"))

    @property
    def port(self) -> int:
        return int(self.service_settings.get("port", 8091))

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
            payload["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        return payload

    def submit_handshake(self, payload: dict) -> dict:
        job_folder = payload.get("job_folder")
        if not job_folder:
            raise ValueError("Handshake requires job_folder.")

        descriptor = load_job_descriptor(str(job_folder))
        run_id = generate_run_id()
        with self._lock:
            self._jobs[run_id] = {
                "run_id": run_id,
                "status": "accepted",
                "job_id": descriptor.job_id,
                "job_folder": str(descriptor.job_folder),
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
        with self._lock:
            if run_id in self._jobs:
                self._jobs[run_id]["status"] = state
                self._jobs[run_id]["updated_at"] = datetime.datetime.now().isoformat()

    def _create_run_manifest(self, run_id: str, descriptor: JobDescriptor) -> None:
        run_path = self.base_dir / "work" / "runs" / run_id
        (run_path / "input").mkdir(parents=True, exist_ok=True)
        source_internal_name = f"{run_id}.pdf"
        copied_input_path = run_path / "input" / source_internal_name
        shutil.copy2(descriptor.source_path, copied_input_path)
        validate_pdf(copied_input_path)

        sha256_hash = hashlib.sha256()
        with open(descriptor.source_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        source_hash = sha256_hash.hexdigest()
        document_machine_code = f"DOC_{source_hash[:16].upper()}"
        source_display_name = descriptor.source_original_name
        if not source_display_name and descriptor.source_alias_name:
            source_display_name = descriptor.source_alias_name
            if not source_display_name.lower().endswith(".pdf"):
                source_display_name = f"{source_display_name}.pdf"
        if not source_display_name:
            source_display_name = descriptor.source_file

        manifest = {
            "run_id": run_id,
            "job_id": descriptor.job_id,
            "profile": descriptor.profile,
            "job_folder": str(descriptor.job_folder),
            "job_folder_name": descriptor.job_folder.name,
            "source_drop_path": str(descriptor.source_path),
            "source_pdf_intake_name": descriptor.source_file,
            "source_pdf_original_name": descriptor.source_original_name,
            "source_pdf_alias_name": descriptor.source_alias_name,
            "source_pdf_display_name": source_display_name,
            "source_pdf_internal_name": source_internal_name,
            "source_pdf_hash": source_hash,
            "document_machine_code": document_machine_code,
            "created_at": datetime.datetime.now().isoformat(),
            "page_count": 0,
            "page_ids": [],
            "page_type_counts": {},
            "state_history": [
                {
                    "state": RUN_STATE_HANDSHAKE_RECEIVED,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "message": "handshake accepted",
                }
            ],
            "current_state": RUN_STATE_HANDSHAKE_RECEIVED,
            "status": RUN_STATE_HANDSHAKE_RECEIVED,
            "completed_stages": [],
            "worker_timings": [],
            "quarantine_flag": False,
        }
        self.run_manifest_handler.save(run_id, manifest)

    def _append_worker_timing(
        self,
        run_id: str,
        worker_name: str,
        started_at: datetime.datetime,
        completed_at: datetime.datetime,
        duration_seconds: float,
        status: str,
        error_message: str | None = None,
    ) -> None:
        manifest = self.run_manifest_handler.load(run_id)
        worker_timings = manifest.get("worker_timings", [])
        worker_timings.append(
            {
                "worker": worker_name,
                "status": status,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_seconds": round(duration_seconds, 2),
                "error_message": error_message,
            }
        )
        self.run_manifest_handler.update(run_id, {"worker_timings": worker_timings})

    def _sync_packaged_runtime_metrics(self, run_id: str) -> None:
        manifest = self.run_manifest_handler.load(run_id)
        packaged_output = manifest.get("packaged_output", {})
        runtime_metrics = manifest.get("runtime_metrics")
        worker_timings = manifest.get("worker_timings", [])
        if not packaged_output:
            return

        run_json_path = Path(packaged_output.get("machine_readable", "")) / "run.json"
        if run_json_path.exists():
            run_export = json.loads(run_json_path.read_text(encoding="utf-8"))
            run_export["runtime_metrics"] = runtime_metrics
            run_export["worker_timings"] = worker_timings
            run_json_path.write_text(json.dumps(run_export, indent=2) + "\n", encoding="utf-8")

        handoff_path = Path(packaged_output.get("handoff", ""))
        if handoff_path.exists():
            handoff_payload = json.loads(handoff_path.read_text(encoding="utf-8"))
            handoff_payload["runtime_metrics"] = runtime_metrics
            handoff_payload["worker_timings"] = worker_timings
            handoff_path.write_text(json.dumps(handoff_payload, indent=2) + "\n", encoding="utf-8")

        machine_readable_handoff_path = Path(packaged_output.get("machine_readable_handoff", ""))
        if machine_readable_handoff_path.exists():
            handoff_payload = json.loads(machine_readable_handoff_path.read_text(encoding="utf-8"))
            handoff_payload["runtime_metrics"] = runtime_metrics
            handoff_payload["worker_timings"] = worker_timings
            machine_readable_handoff_path.write_text(json.dumps(handoff_payload, indent=2) + "\n", encoding="utf-8")

    def _call_worker(self, fn, run_id: str, worker_name: str) -> None:
        self.logger.info("WORKER_START", "SUCCESS", run_id=run_id, message=worker_name)
        started_at = datetime.datetime.now()
        started_perf = time.perf_counter()
        error_message = None
        status = "success"
        try:
            fn(run_id)
        except SystemExit as exc:
            status = "failure"
            error_message = f"{worker_name} exited with code {exc.code}"
            raise RuntimeError(f"{worker_name} exited with code {exc.code}") from exc
        except Exception as exc:
            status = "failure"
            error_message = str(exc)
            raise
        finally:
            completed_at = datetime.datetime.now()
            duration_seconds = time.perf_counter() - started_perf
            self._append_worker_timing(
                run_id,
                worker_name,
                started_at,
                completed_at,
                duration_seconds,
                status,
                error_message,
            )
        self.logger.info("WORKER_COMPLETE", "SUCCESS", run_id=run_id, message=worker_name)

    def _quarantine_run(self, run_id: str, reason: str) -> None:
        run_path = self.base_dir / "work" / "runs" / run_id
        if not run_path.exists():
            return
        quarantine_root = self.base_dir / "quarantine" / run_id
        quarantine_root.mkdir(parents=True, exist_ok=True)
        if (quarantine_root / "work").exists():
            shutil.rmtree(quarantine_root / "work")
        shutil.move(str(run_path), str(quarantine_root / "work"))
        with open(quarantine_root / "FAILURE_REASON.txt", "w", encoding="utf-8") as f:
            f.write(f"Authority: {QUARANTINE_AUTHORITY}\nReason: {reason}\n")
        self.run_manifest_handler.update(
            run_id,
            {
                "quarantine_flag": True,
                "status": RUN_STATE_FAILED,
                "current_state": RUN_STATE_FAILED,
                "failure_reason": reason,
            },
        )

    def _dispatch_review_engine(self, run_id: str) -> dict:
        started_at = datetime.datetime.now()
        started_perf = time.perf_counter()
        status = "success"
        error_message = None
        result: dict = {}
        try:
            result = dispatch_review_engine_handshake(self.base_dir, run_id, self.thresholds)
            return result
        except Exception as exc:
            status = "failure"
            error_message = str(exc)
            self.logger.error("REVIEW_ENGINE_DISPATCH_FAILED", "FAILURE", run_id=run_id, message=str(exc))
            self.run_manifest_handler.update(
                run_id,
                {
                    "secondary_review": {
                        "status": "failed",
                        "error": str(exc),
                    }
                },
            )
            return {"status": "failed", "error": str(exc)}
        finally:
            completed_at = datetime.datetime.now()
            duration_seconds = time.perf_counter() - started_perf
            self._append_worker_timing(
                run_id,
                "dispatch_review_engine",
                started_at,
                completed_at,
                duration_seconds,
                status,
                error_message,
            )

    def _process_handshake(self, run_id: str, descriptor: JobDescriptor) -> None:
        monitor = RunPerformanceMonitor(
            self.base_dir,
            sample_interval_seconds=float(self.service_settings.get("performance_sample_interval_seconds", 0.5)),
        )
        handoff_payload: dict | None = None
        with self._lock:
            self._active_runs.add(run_id)
            self._service_state = SERVICE_STATE_PROCESSING
        try:
            self._create_run_manifest(run_id, descriptor)
            monitor.start()
            self._append_state_history(run_id, RUN_STATE_DROP_VERIFIED, "drop folder verified")
            self._append_state_history(run_id, RUN_STATE_PREPARED, "run workspace prepared")

            self._call_worker(hash_input_pdf_main, run_id, "hash_input_pdf")
            self._call_worker(split_pdf_to_single_page_pdfs_main, run_id, "split_pdf_to_single_page_pdfs")
            self._call_worker(register_split_pages_main, run_id, "register_split_pages")
            self._append_state_history(run_id, RUN_STATE_SPLIT, "split state complete")

            self._call_worker(
                lambda rid: run_page_geometry_normalization(self.base_dir, rid, self.thresholds),
                run_id,
                "run_page_geometry_normalization",
            )
            self._append_state_history(
                run_id,
                RUN_STATE_GEOMETRY_NORMALIZED,
                "page orientation and skew normalization complete",
            )

            self._call_worker(
                lambda rid: run_page_feature_pipeline(self.base_dir, rid, self.thresholds),
                run_id,
                "run_page_feature_pipeline",
            )
            self._append_state_history(run_id, RUN_STATE_FEATURES_COMPUTED, "baseline features and text extraction complete")

            run_page_typing(self.base_dir, run_id, self.thresholds)
            self._append_state_history(run_id, RUN_STATE_TYPED, "page typing complete")

            run_page_type_extraction(self.base_dir, run_id)
            self._append_state_history(run_id, RUN_STATE_EXTRACTED, "typed extraction complete")

            handoff_payload = package_run(self.base_dir, run_id)
            self._append_state_history(run_id, RUN_STATE_PACKAGED, "package created")
            self._dispatch_review_engine(run_id)
            self._append_state_history(run_id, RUN_STATE_HANDOFF_READY, "handoff payload written")
            self._append_state_history(run_id, RUN_STATE_COMPLETED, "run completed")
            self.run_manifest_handler.update(run_id, {"handoff_payload": handoff_payload, "status": RUN_STATE_COMPLETED})
            with self._lock:
                self._jobs[run_id]["status"] = "completed"
                self._jobs[run_id]["handoff_payload"] = handoff_payload
        except Exception as exc:
            reason = f"{exc}\n{traceback.format_exc()}"
            self.logger.error("RUN_FAILED", "FAILURE", run_id=run_id, message=str(exc))
            manifest_path = self.base_dir / "manifests" / "runs" / f"{run_id}.json"
            if manifest_path.exists():
                try:
                    self._append_state_history(run_id, RUN_STATE_FAILED, str(exc))
                except Exception as hist_exc:
                    self.logger.error("STATE_HISTORY_APPEND_FAILED", "FAILURE", run_id=run_id, message=str(hist_exc))
                self._quarantine_run(run_id, reason)
            with self._lock:
                self._jobs[run_id]["status"] = "failed"
                self._jobs[run_id]["error"] = str(exc)
        finally:
            manifest_path = self.base_dir / "manifests" / "runs" / f"{run_id}.json"
            if manifest_path.exists():
                try:
                    page_count = self.run_manifest_handler.load(run_id).get("page_count", 0)
                except Exception:
                    page_count = 0
                runtime_metrics = monitor.stop(page_count=page_count)
                self.run_manifest_handler.update(run_id, {"runtime_metrics": runtime_metrics})
                self._sync_packaged_runtime_metrics(run_id)
                with self._lock:
                    if run_id in self._jobs:
                        self._jobs[run_id]["runtime_metrics"] = runtime_metrics
            # Janitor authority lives in the QA module.
            with self._lock:
                self._active_runs.discard(run_id)
                self._service_state = SERVICE_STATE_READY if not self._active_runs else SERVICE_STATE_PROCESSING


def create_http_handler(service: DropHandshakeService):
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
