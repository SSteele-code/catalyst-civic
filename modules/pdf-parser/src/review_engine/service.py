from __future__ import annotations

import datetime
import json
import shutil
import sys
import threading
import time
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
from src.state_machine.config import load_thresholds
from src.review_engine.processor import process_review_page


REVIEW_SERVICE_STATE_READY = "ready"
REVIEW_SERVICE_STATE_PROCESSING = "processing"
REVIEW_RUN_STATE_HANDSHAKE_RECEIVED = "handshake_received"
REVIEW_RUN_STATE_STAGED = "staged"
REVIEW_RUN_STATE_PROCESSED = "processed"
REVIEW_RUN_STATE_PACKAGED = "packaged"
REVIEW_RUN_STATE_COPIED_BACK = "copied_back"
REVIEW_RUN_STATE_COMPLETED = "completed"
REVIEW_RUN_STATE_FAILED = "failed"


@dataclass
class ReviewJobDescriptor:
    job_folder: Path
    job_path: Path
    parent_run_id: str
    parent_output_root: Path
    parent_machine_readable_folder: Path
    source_pdf_hash: str | None
    pages: list[dict]


def generate_review_run_id() -> str:
    now = datetime.datetime.now()
    return f"REV_{now.strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:4].upper()}"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def table_region_count(page_manifest: dict) -> int:
    return sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id)


def absolute_if_present(base_dir: Path, relative_path: str | None) -> str | None:
    value = str(relative_path or "").strip()
    if not value:
        return None
    return str((base_dir / value).resolve())


def review_state_priority(review_state: str) -> int:
    mapping = {
        "quarantined": 0,
        "review_required": 1,
        "provisional": 2,
        "auto_pass": 3,
    }
    return mapping.get(str(review_state or ""), 9)


def should_dispatch_page(page_manifest: dict, settings: dict) -> bool:
    review_state = str(page_manifest.get("review_state") or "")
    review_states = set(settings.get("candidate_review_states", ["review_required", "quarantined"]))
    if review_state not in review_states:
        return False

    sort_lane = str(page_manifest.get("sort_lane") or "")
    layout_type = str(page_manifest.get("layout_type") or page_manifest.get("page_layout") or "")
    route_type = str(page_manifest.get("route_type") or "")
    rotation = abs(int(page_manifest.get("cardinal_rotation_applied") or 0))
    sort_lanes = set(settings.get("candidate_sort_lanes", ["table_specialist", "weak_fallback"]))
    layout_types = set(settings.get("candidate_layout_types", ["table", "mixed", "form"]))
    table_minimum = int(settings.get("table_region_minimum", 4))
    candidate_rotations = {int(value) for value in settings.get("cardinal_rotation_candidates", [90, 270])}

    return (
        sort_lane in sort_lanes
        or layout_type in layout_types
        or route_type == "ocr_mixed_layout_page"
        or table_region_count(page_manifest) >= table_minimum
        or rotation in candidate_rotations
    )


def build_review_job_folder(base_dir: Path, parent_run_id: str, thresholds: dict) -> Path | None:
    settings = thresholds.get("review_engine", {})
    if not settings.get("enabled", True) or not settings.get("auto_dispatch_enabled", True):
        return None

    run_handler = ManifestHandler(base_dir / "manifests" / "runs")
    page_handler = ManifestHandler(base_dir / "manifests" / "pages")
    run_manifest = run_handler.load(parent_run_id)
    packaged_output = run_manifest.get("packaged_output", {})
    if not packaged_output:
        run_handler.update(parent_run_id, {"secondary_review": {"status": "skipped", "reason": "missing_packaged_output"}})
        return None

    candidates: list[dict] = []
    for page_id in run_manifest.get("page_ids", []):
        page_manifest = page_handler.load(page_id)
        if not should_dispatch_page(page_manifest, settings):
            continue
        candidates.append(
            {
                "page_id": page_id,
                "source_page_number": page_manifest.get("source_page_number"),
                "review_state": page_manifest.get("review_state"),
                "sort_lane": page_manifest.get("sort_lane"),
                "layout_type": page_manifest.get("layout_type", page_manifest.get("page_layout")),
                "route_type": page_manifest.get("route_type"),
                "cardinal_rotation_applied": page_manifest.get("cardinal_rotation_applied", 0),
                "table_region_count": table_region_count(page_manifest),
                "raw_pdf_path": absolute_if_present(base_dir, page_manifest.get("raw_pdf_path")),
                "normalized_image_path": absolute_if_present(base_dir, page_manifest.get("normalized_image_path")),
                "rendered_image_path": absolute_if_present(base_dir, page_manifest.get("rendered_image_path")),
                "word_witness_path": absolute_if_present(base_dir, page_manifest.get("word_witness_path")),
                "page_manifest_path": str((base_dir / "manifests" / "pages" / f"{page_id}.json").resolve()),
                "page_export_path": str((Path(packaged_output.get("machine_readable", "")) / "pages" / f"{page_id}.json").resolve()),
            }
        )

    if not candidates:
        run_handler.update(parent_run_id, {"secondary_review": {"status": "skipped", "reason": "no_candidate_pages", "page_count": 0}})
        return None

    candidates.sort(
        key=lambda item: (
            review_state_priority(str(item.get("review_state") or "")),
            int(item.get("source_page_number") or 0),
        )
    )
    max_pages = int(settings.get("max_pages_per_run", 12))
    selected_candidates = candidates[:max_pages]

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    job_folder = base_dir / "review_inbox" / f"JOB_REVIEW_{parent_run_id}_{timestamp}"
    job_folder.mkdir(parents=True, exist_ok=True)
    review_job = {
        "parent_run_id": parent_run_id,
        "parent_output_root": packaged_output.get("root"),
        "parent_machine_readable_folder": packaged_output.get("machine_readable"),
        "source_pdf_hash": run_manifest.get("source_pdf_hash"),
        "created_at": datetime.datetime.now().isoformat(),
        "pages": selected_candidates,
    }
    write_json(job_folder / "review_job.json", review_job)
    run_handler.update(
        parent_run_id,
        {
            "secondary_review": {
                "status": "queued",
                "job_folder": str(job_folder),
                "candidate_page_count": len(selected_candidates),
                "candidate_pages": [
                    {
                        "page_id": item["page_id"],
                        "source_page_number": item["source_page_number"],
                        "review_state": item["review_state"],
                        "sort_lane": item["sort_lane"],
                    }
                    for item in selected_candidates
                ],
            }
        },
    )
    return job_folder


def load_review_job_descriptor(job_folder_raw: str) -> ReviewJobDescriptor:
    job_folder = Path(job_folder_raw).resolve()
    if not job_folder.exists() or not job_folder.is_dir():
        raise ValueError(f"Review job folder not found: {job_folder}")
    job_path = job_folder / "review_job.json"
    if not job_path.exists():
        raise ValueError(f"Review job descriptor missing: {job_path}")
    payload = load_json(job_path)
    parent_run_id = str(payload.get("parent_run_id") or "").strip()
    if not parent_run_id:
        raise ValueError("Review job missing parent_run_id.")
    parent_output_root = Path(str(payload.get("parent_output_root") or "")).resolve()
    parent_machine_readable_folder = Path(str(payload.get("parent_machine_readable_folder") or "")).resolve()
    pages = list(payload.get("pages") or [])
    if not pages:
        raise ValueError("Review job does not contain any pages.")
    return ReviewJobDescriptor(
        job_folder=job_folder,
        job_path=job_path,
        parent_run_id=parent_run_id,
        parent_output_root=parent_output_root,
        parent_machine_readable_folder=parent_machine_readable_folder,
        source_pdf_hash=payload.get("source_pdf_hash"),
        pages=pages,
    )


class ReviewHandshakeService:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir or BASE_DIR)
        self.thresholds = load_thresholds(self.base_dir)
        self.settings = self.thresholds.get("review_engine", {})
        self.run_handler = ManifestHandler(self.base_dir / "review_manifests" / "runs")
        self.page_handler = ManifestHandler(self.base_dir / "review_manifests" / "pages")
        self.parent_run_handler = ManifestHandler(self.base_dir / "manifests" / "runs")
        self.parent_page_handler = ManifestHandler(self.base_dir / "manifests" / "pages")
        self.logger = PipelineLogger(self.base_dir / "review_logs" / "service", "review_engine_service")
        self._executor = ThreadPoolExecutor(max_workers=int(self.settings.get("max_concurrent_runs", 1)))
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._active_runs: set[str] = set()
        self._service_state = REVIEW_SERVICE_STATE_READY

    @property
    def host(self) -> str:
        return str(self.settings.get("host", "127.0.0.1"))

    @property
    def port(self) -> int:
        return int(self.settings.get("port", 8092))

    def health_payload(self) -> dict:
        with self._lock:
            return {
                "service_state": self._service_state,
                "active_runs": sorted(self._active_runs),
                "known_runs": sorted(self._jobs),
            }

    def run_payload(self, run_id: str) -> dict:
        payload = dict(self._jobs.get(run_id, {}))
        manifest_path = self.base_dir / "review_manifests" / "runs" / f"{run_id}.json"
        if manifest_path.exists():
            payload["manifest"] = load_json(manifest_path)
        return payload

    def submit_handshake(self, payload: dict) -> dict:
        descriptor = load_review_job_descriptor(str(payload.get("job_folder") or ""))
        run_id = generate_review_run_id()
        with self._lock:
            self._jobs[run_id] = {
                "run_id": run_id,
                "status": "accepted",
                "parent_run_id": descriptor.parent_run_id,
                "job_folder": str(descriptor.job_folder),
                "submitted_at": datetime.datetime.now().isoformat(),
            }
        self._executor.submit(self._process_handshake, run_id, descriptor)
        self.logger.info("REVIEW_HANDSHAKE_ACCEPTED", "SUCCESS", run_id=run_id, message=descriptor.parent_run_id)
        return {"run_id": run_id, "status": "accepted"}

    def process_handshake_job(self, job_folder_raw: str) -> dict:
        descriptor = load_review_job_descriptor(job_folder_raw)
        run_id = generate_review_run_id()
        with self._lock:
            self._jobs[run_id] = {
                "run_id": run_id,
                "status": "accepted",
                "parent_run_id": descriptor.parent_run_id,
                "job_folder": str(descriptor.job_folder),
                "submitted_at": datetime.datetime.now().isoformat(),
            }
        self._process_handshake(run_id, descriptor)
        return self.run_payload(run_id)

    def _append_state_history(self, run_id: str, state: str, message: str = "") -> None:
        manifest = self.run_handler.load(run_id)
        history = manifest.get("state_history", [])
        history.append(
            {
                "state": state,
                "timestamp": datetime.datetime.now().isoformat(),
                "message": message,
            }
        )
        self.run_handler.update(run_id, {"current_state": state, "state_history": history, "status": state})

    def _create_run_manifest(self, run_id: str, descriptor: ReviewJobDescriptor) -> None:
        manifest = {
            "review_run_id": run_id,
            "parent_run_id": descriptor.parent_run_id,
            "parent_output_root": str(descriptor.parent_output_root),
            "parent_machine_readable_folder": str(descriptor.parent_machine_readable_folder),
            "source_pdf_hash": descriptor.source_pdf_hash,
            "created_at": datetime.datetime.now().isoformat(),
            "page_count": len(descriptor.pages),
            "page_ids": [],
            "status": REVIEW_RUN_STATE_HANDSHAKE_RECEIVED,
            "current_state": REVIEW_RUN_STATE_HANDSHAKE_RECEIVED,
            "state_history": [
                {
                    "state": REVIEW_RUN_STATE_HANDSHAKE_RECEIVED,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "message": "review handshake accepted",
                }
            ],
        }
        self.run_handler.save(run_id, manifest)

    def _copy_optional(self, source_path: str | None, destination_path: Path) -> str | None:
        if not source_path:
            return None
        source = Path(source_path)
        if not source.exists() or not source.is_file():
            return None
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination_path)
        return str(destination_path)

    def _stage_inputs(self, run_id: str, descriptor: ReviewJobDescriptor) -> list[str]:
        staged_page_ids: list[str] = []
        input_root = self.base_dir / "review_work" / "runs" / run_id / "input" / "pages"
        for page in descriptor.pages:
            parent_page_id = str(page["page_id"])
            review_page_id = f"{run_id}_{parent_page_id}"
            page_dir = input_root / review_page_id
            page_dir.mkdir(parents=True, exist_ok=True)
            normalized_image_path = self._copy_optional(page.get("normalized_image_path"), page_dir / "normalized.png")
            raw_pdf_path = self._copy_optional(page.get("raw_pdf_path"), page_dir / "page.pdf")
            page_export_path = self._copy_optional(page.get("page_export_path"), page_dir / "page_export.json")
            staged_manifest = {
                "review_run_id": run_id,
                "review_page_id": review_page_id,
                "parent_run_id": descriptor.parent_run_id,
                "parent_page_id": parent_page_id,
                "source_page_number": page.get("source_page_number"),
                "source_review_state": page.get("review_state"),
                "source_sort_lane": page.get("sort_lane"),
                "source_layout_type": page.get("layout_type"),
                "source_route_type": page.get("route_type"),
                "source_cardinal_rotation_applied": page.get("cardinal_rotation_applied", 0),
                "source_table_region_count": page.get("table_region_count", 0),
                "staged_normalized_image_path": normalized_image_path,
                "staged_raw_pdf_path": raw_pdf_path,
                "staged_page_export_path": page_export_path,
                "parent_page_export_path": page.get("page_export_path"),
                "parent_page_manifest_path": page.get("page_manifest_path"),
                "current_state": REVIEW_RUN_STATE_STAGED,
            }
            self.page_handler.save(review_page_id, staged_manifest)
            staged_page_ids.append(review_page_id)
        self.run_handler.update(run_id, {"page_ids": staged_page_ids})
        return staged_page_ids

    def _package_review_run(self, run_id: str, descriptor: ReviewJobDescriptor, page_ids: list[str]) -> dict:
        run_root = self.base_dir / "review_work" / "runs" / run_id
        input_root = run_root / "input"
        results_root = run_root / "results"
        machine_root = run_root / "machine_readable"
        pages_dir = machine_root / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        page_payloads: list[dict] = []
        page_summaries: list[dict] = []

        for review_page_id in page_ids:
            page_manifest = self.page_handler.load(review_page_id)
            result_path = Path(str(page_manifest.get("review_result_path") or ""))
            result_payload = load_json(result_path) if result_path.exists() else {}
            page_payload = {
                "schema_version": "catalyst_review_page_export.v1",
                "review_run_id": run_id,
                "parent_run_id": descriptor.parent_run_id,
                "review_page_id": review_page_id,
                "parent_page_id": page_manifest.get("parent_page_id"),
                "source_page_number": page_manifest.get("source_page_number"),
                "source_review_state": page_manifest.get("source_review_state"),
                "source_sort_lane": page_manifest.get("source_sort_lane"),
                "status": page_manifest.get("review_status"),
                "artifact_created_at": page_manifest.get("review_completed_at"),
                "source_bundle_path": f"input/pages/{review_page_id}",
                "source_files": {
                    "normalized_image": f"input/pages/{review_page_id}/normalized.png",
                    "raw_pdf": f"input/pages/{review_page_id}/page.pdf",
                    "page_export": f"input/pages/{review_page_id}/page_export.json",
                },
                "result": result_payload,
            }
            write_json(pages_dir / f"{review_page_id}.json", page_payload)
            page_payloads.append(page_payload)
            page_summaries.append(
                {
                    "review_page_id": review_page_id,
                    "parent_page_id": page_manifest.get("parent_page_id"),
                    "source_page_number": page_manifest.get("source_page_number"),
                    "status": page_manifest.get("review_status"),
                    "artifact_path": f"pages/{review_page_id}.json",
                    "source_bundle_path": f"input/pages/{review_page_id}",
                    "completed_table_count": result_payload.get("completed_table_count", 0),
                }
            )

        with open(machine_root / "pages.jsonl", "w", encoding="utf-8") as f:
            for payload in page_payloads:
                f.write(json.dumps(payload) + "\n")

        status_counts: dict[str, int] = {}
        for summary in page_summaries:
            status = str(summary.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        run_payload = {
            "schema_version": "catalyst_review_run_export.v1",
            "review_run_id": run_id,
            "parent_run_id": descriptor.parent_run_id,
            "source_pdf_hash": descriptor.source_pdf_hash,
            "created_at": self.run_handler.load(run_id).get("created_at"),
            "packaged_at": datetime.datetime.now().isoformat(),
            "page_count": len(page_ids),
            "source_bundle_root": "input/pages",
            "status_counts": status_counts,
            "page_results": page_summaries,
        }
        write_json(machine_root / "review_run.json", run_payload)
        write_json(machine_root / "run.json", run_payload)
        handoff_payload = {
            "schema_version": "catalyst_review_handoff.v1",
            "review_run_id": run_id,
            "parent_run_id": descriptor.parent_run_id,
            "status": REVIEW_RUN_STATE_COMPLETED,
            "page_count": len(page_ids),
            "status_counts": status_counts,
            "source_bundle_root": "input/pages",
            "page_results": page_summaries,
        }
        write_json(machine_root / "handoff.json", handoff_payload)

        outbox_root = self.base_dir / "review_outbox" / f"{run_id}_{descriptor.parent_run_id}"
        if outbox_root.exists():
            shutil.rmtree(outbox_root)
        outbox_root.mkdir(parents=True, exist_ok=True)
        if input_root.exists():
            shutil.copytree(input_root, outbox_root / "input")
        if results_root.exists():
            shutil.copytree(results_root, outbox_root / "results")
        shutil.copytree(machine_root, outbox_root / "machine_readable")
        write_json(outbox_root / "handoff.json", handoff_payload)
        (outbox_root / "SUCCESS.txt").write_text(f"Review run {run_id} completed successfully.\n", encoding="utf-8")

        package_payload = {
            "review_run_id": run_id,
            "parent_run_id": descriptor.parent_run_id,
            "output_root": str(outbox_root),
            "machine_readable_folder": str(outbox_root / "machine_readable"),
            "source_bundle_root": str(outbox_root / "input" / "pages"),
            "page_count": len(page_ids),
            "status_counts": status_counts,
            "page_results": page_summaries,
        }
        self.run_handler.update(run_id, {"packaged_output": package_payload})
        return package_payload

    def _rewrite_parent_pages_jsonl(self, parent_machine_readable_folder: Path) -> None:
        pages_dir = parent_machine_readable_folder / "pages"
        pages_jsonl_path = parent_machine_readable_folder / "pages.jsonl"
        with open(pages_jsonl_path, "w", encoding="utf-8") as f:
            for page_path in sorted(pages_dir.glob("*.json")):
                f.write(page_path.read_text(encoding="utf-8").rstrip() + "\n")

    def _copy_back_to_parent(self, run_id: str, descriptor: ReviewJobDescriptor, package_payload: dict) -> dict:
        copyback_subdir = str(self.settings.get("copyback_subdir", "review_engine"))
        review_outbox_root = Path(package_payload["output_root"])
        parent_root_target = descriptor.parent_output_root / copyback_subdir / run_id
        parent_machine_target = descriptor.parent_machine_readable_folder / copyback_subdir / run_id

        if parent_root_target.exists():
            shutil.rmtree(parent_root_target)
        if parent_machine_target.exists():
            shutil.rmtree(parent_machine_target)
        shutil.copytree(review_outbox_root, parent_root_target)
        shutil.copytree(review_outbox_root / "machine_readable", parent_machine_target)

        page_results: list[dict] = []
        for page_summary in package_payload.get("page_results", []):
            parent_page_id = str(page_summary["parent_page_id"])
            review_page_id = str(page_summary["review_page_id"])
            relative_artifact_path = Path(copyback_subdir) / run_id / "pages" / f"{review_page_id}.json"
            relative_source_bundle_path = Path(copyback_subdir) / run_id / "input" / "pages" / review_page_id
            review_artifact_path = descriptor.parent_machine_readable_folder / relative_artifact_path
            review_payload = load_json(review_artifact_path)

            parent_page_export_path = descriptor.parent_machine_readable_folder / "pages" / f"{parent_page_id}.json"
            if parent_page_export_path.exists():
                parent_page_export = load_json(parent_page_export_path)
                parent_page_export["secondary_review"] = {
                    "engine": "deterministic_cell_crop_review.v1",
                    "review_run_id": run_id,
                    "status": page_summary.get("status"),
                    "artifact_path": relative_artifact_path.as_posix(),
                    "source_bundle_path": relative_source_bundle_path.as_posix(),
                    "completed_table_count": review_payload.get("result", {}).get("completed_table_count", 0),
                    "flattened_text_preview": str(review_payload.get("result", {}).get("flattened_text") or "")[:4000],
                }
                write_json(parent_page_export_path, parent_page_export)

            page_results.append(
                {
                    "parent_page_id": parent_page_id,
                    "source_page_number": page_summary.get("source_page_number"),
                    "status": page_summary.get("status"),
                    "artifact_path": relative_artifact_path.as_posix(),
                    "source_bundle_path": relative_source_bundle_path.as_posix(),
                    "completed_table_count": review_payload.get("result", {}).get("completed_table_count", 0),
                }
            )
            self.parent_page_handler.update(
                parent_page_id,
                {
                    "secondary_review_status": page_summary.get("status"),
                    "secondary_review_run_id": run_id,
                    "secondary_review_artifact_path": relative_artifact_path.as_posix(),
                    "secondary_review_source_bundle_path": relative_source_bundle_path.as_posix(),
                },
            )

        self._rewrite_parent_pages_jsonl(descriptor.parent_machine_readable_folder)

        summary = {
            "status": "completed",
            "review_run_id": run_id,
            "page_count": package_payload.get("page_count", 0),
            "copyback_root": str(parent_root_target),
            "copyback_machine_readable_root": str(parent_machine_target),
            "source_bundle_root": str(parent_root_target / "input" / "pages"),
            "status_counts": package_payload.get("status_counts", {}),
            "page_results": page_results,
        }

        for handoff_path in [
            descriptor.parent_output_root / "handoff.json",
            descriptor.parent_machine_readable_folder / "handoff.json",
        ]:
            if handoff_path.exists():
                handoff_payload = load_json(handoff_path)
                handoff_payload["secondary_review"] = summary
                write_json(handoff_path, handoff_payload)

        run_json_path = descriptor.parent_machine_readable_folder / "run.json"
        if run_json_path.exists():
            run_payload = load_json(run_json_path)
            run_payload["secondary_review"] = summary
            write_json(run_json_path, run_payload)

        self.parent_run_handler.update(descriptor.parent_run_id, {"secondary_review": summary})
        return summary

    def _process_handshake(self, run_id: str, descriptor: ReviewJobDescriptor) -> None:
        with self._lock:
            self._active_runs.add(run_id)
            self._service_state = REVIEW_SERVICE_STATE_PROCESSING
        started = time.perf_counter()
        try:
            self._create_run_manifest(run_id, descriptor)
            staged_page_ids = self._stage_inputs(run_id, descriptor)
            self._append_state_history(run_id, REVIEW_RUN_STATE_STAGED, f"staged {len(staged_page_ids)} pages")

            results_root = self.base_dir / "review_work" / "runs" / run_id / "results" / "pages"
            results_root.mkdir(parents=True, exist_ok=True)
            for review_page_id in staged_page_ids:
                page_manifest = self.page_handler.load(review_page_id)
                result = process_review_page(page_manifest, self.settings)
                result.update(
                    {
                        "review_run_id": run_id,
                        "parent_run_id": descriptor.parent_run_id,
                        "review_page_id": review_page_id,
                        "parent_page_id": page_manifest.get("parent_page_id"),
                        "source_page_number": page_manifest.get("source_page_number"),
                    }
                )
                result_path = results_root / f"{review_page_id}.json"
                write_json(result_path, result)
                self.page_handler.update(
                    review_page_id,
                    {
                        "review_result_path": str(result_path),
                        "review_status": result.get("status"),
                        "review_completed_at": datetime.datetime.now().isoformat(),
                        "current_state": REVIEW_RUN_STATE_PROCESSED,
                    },
                )

            self._append_state_history(run_id, REVIEW_RUN_STATE_PROCESSED, "deterministic review processing complete")
            package_payload = self._package_review_run(run_id, descriptor, staged_page_ids)
            self._append_state_history(run_id, REVIEW_RUN_STATE_PACKAGED, "review package created")
            copyback_summary = self._copy_back_to_parent(run_id, descriptor, package_payload)
            self._append_state_history(run_id, REVIEW_RUN_STATE_COPIED_BACK, "review results copied back to parent run")
            duration_seconds = round(time.perf_counter() - started, 2)
            self.run_handler.update(
                run_id,
                {
                    "copyback_summary": copyback_summary,
                    "duration_seconds": duration_seconds,
                    "status": REVIEW_RUN_STATE_COMPLETED,
                    "current_state": REVIEW_RUN_STATE_COMPLETED,
                },
            )
            self._append_state_history(run_id, REVIEW_RUN_STATE_COMPLETED, "review run completed")
            with self._lock:
                self._jobs[run_id]["status"] = "completed"
                self._jobs[run_id]["copyback_summary"] = copyback_summary
        except Exception as exc:
            self.run_handler.update(
                run_id,
                {
                    "status": REVIEW_RUN_STATE_FAILED,
                    "current_state": REVIEW_RUN_STATE_FAILED,
                    "failure_reason": str(exc),
                },
            )
            self._append_state_history(run_id, REVIEW_RUN_STATE_FAILED, str(exc))
            with self._lock:
                self._jobs[run_id]["status"] = "failed"
                self._jobs[run_id]["error"] = str(exc)
            self.logger.error("REVIEW_RUN_FAILED", "FAILURE", run_id=run_id, message=str(exc))
        finally:
            with self._lock:
                self._active_runs.discard(run_id)
                self._service_state = REVIEW_SERVICE_STATE_READY if not self._active_runs else REVIEW_SERVICE_STATE_PROCESSING


def create_http_handler(service: ReviewHandshakeService):
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


def dispatch_review_engine_handshake(base_dir: Path, parent_run_id: str, thresholds: dict) -> dict:
    job_folder = build_review_job_folder(base_dir, parent_run_id, thresholds)
    if job_folder is None:
        return {"status": "skipped"}
    service = ReviewHandshakeService(base_dir)
    return service.process_handshake_job(str(job_folder))
