from __future__ import annotations

import json
import re
import shutil
import time
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


RUN_ID_PATTERN = re.compile(r"^(RUN_\d{4}_\d{2}_\d{2}_[A-Z0-9]{4})(?:_|$)")
DEFAULT_REPORT_RELATIVE_PATH = Path("reports") / "JANITOR_LAST_RUN.json"


def _debug_log(base_dir: Path, run_id: str, hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    payload = {
        "sessionId": "728be7",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
        "id": f"log_{int(time.time() * 1000)}_{random.randint(1000, 9999)}",
    }
    with (base_dir / "debug-728be7.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def ensure_within(base_dir: Path, candidate: Path) -> Path:
    resolved_base = base_dir.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"Unsafe path outside machine root: {resolved_candidate}") from exc
    return resolved_candidate


def extract_run_id(name: str) -> str | None:
    match = RUN_ID_PATTERN.match(name)
    if not match:
        return None
    return match.group(1)


def validate_successful_output_folder(path: Path, require_success_marker: bool = True) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if not path.exists() or not path.is_dir():
        issues.append("output_root_missing")
        return False, issues

    if require_success_marker and not (path / "SUCCESS.txt").exists():
        issues.append("success_marker_missing")

    handoff_path = path / "handoff.json"
    machine_readable_path = path / "machine_readable"
    run_json_path = machine_readable_path / "run.json"
    mr_handoff_path = machine_readable_path / "handoff.json"
    if not handoff_path.exists():
        issues.append("handoff_missing")
    if not machine_readable_path.exists():
        issues.append("machine_readable_missing")
    if not run_json_path.exists():
        issues.append("run_json_missing")
    if not mr_handoff_path.exists():
        issues.append("machine_readable_handoff_missing")

    if run_json_path.exists():
        try:
            run_payload = json.loads(run_json_path.read_text(encoding="utf-8"))
            run_id = str(run_payload.get("run_id") or "")
            expected_run_id = extract_run_id(path.name)
            if expected_run_id and run_id and expected_run_id != run_id:
                issues.append("run_id_mismatch")
        except Exception:
            issues.append("run_json_invalid")

    return len(issues) == 0, issues


def remove_path(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_dir():
        shutil.rmtree(path)
        return 1
    path.unlink()
    return 1


def clear_dir_contents(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    removed = 0
    for child in list(path.iterdir()):
        removed += remove_path(child)
    return removed


def choose_kept_outbox_folders(
    base_dir: Path,
    keep_count: int,
    explicit_keep: Path | None,
    require_success: bool,
) -> tuple[list[Path], list[dict[str, Any]]]:
    outbox_dir = ensure_within(base_dir, base_dir / "outbox")
    outbox_dir.mkdir(parents=True, exist_ok=True)
    candidates = sorted((p for p in outbox_dir.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    validation: list[dict[str, Any]] = []

    if explicit_keep is not None:
        keep_path = ensure_within(base_dir, explicit_keep)
        ok, issues = validate_successful_output_folder(keep_path, require_success_marker=require_success)
        validation.append({"path": str(keep_path), "valid": ok, "issues": issues})
        if not ok:
            raise RuntimeError(f"Explicit keep folder failed validation: {keep_path} ({issues})")
        return [keep_path], validation

    successful: list[Path] = []
    for candidate in candidates:
        ok, issues = validate_successful_output_folder(candidate, require_success_marker=require_success)
        validation.append({"path": str(candidate), "valid": ok, "issues": issues})
        if ok:
            successful.append(candidate)
    if not successful:
        raise RuntimeError("No valid successful outbox folders found.")
    keep_count = max(1, int(keep_count))
    return successful[:keep_count], validation


@dataclass
class JanitorSummary:
    kept_outbox: list[str]
    removed_outbox: int
    removed_work_runs: int
    removed_logs_runs: int
    removed_manifest_pages: int
    removed_manifest_regions: int
    removed_review_runtime: int
    removed_run_manifests: int
    removed_run_manifest_aux: int
    removed_inbox: int
    run_manifest_kept: list[str]
    validation: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": datetime.now().isoformat(),
            "kept_outbox": self.kept_outbox,
            "removed_outbox": self.removed_outbox,
            "removed_work_runs": self.removed_work_runs,
            "removed_logs_runs": self.removed_logs_runs,
            "removed_manifest_pages": self.removed_manifest_pages,
            "removed_manifest_regions": self.removed_manifest_regions,
            "removed_review_runtime": self.removed_review_runtime,
            "removed_run_manifests": self.removed_run_manifests,
            "removed_run_manifest_aux": self.removed_run_manifest_aux,
            "removed_inbox": self.removed_inbox,
            "run_manifest_kept": self.run_manifest_kept,
            "validation": self.validation,
        }


def run_janitor(base_dir: Path, janitor_settings: dict[str, Any], explicit_keep: Path | None = None) -> JanitorSummary:
    keep_count = int(janitor_settings.get("keep_outbox_folders", 1))
    require_success = bool(janitor_settings.get("require_success_marker", True))

    kept_outbox_paths, validation = choose_kept_outbox_folders(
        base_dir=base_dir,
        keep_count=keep_count,
        explicit_keep=explicit_keep,
        require_success=require_success,
    )
    # region agent log
    _debug_log(
        base_dir,
        "janitor",
        "H6",
        "src/state_machine/janitor.py:177",
        "Janitor keep selection decided",
        {
            "keep_count": keep_count,
            "require_success": require_success,
            "kept_outbox": [path.name for path in kept_outbox_paths],
            "validation_items": len(validation),
        },
    )
    # endregion
    kept_outbox_set = {path.resolve() for path in kept_outbox_paths}
    kept_outbox_names = [path.name for path in kept_outbox_paths]
    kept_run_ids = {run_id for path in kept_outbox_paths if (run_id := extract_run_id(path.name))}

    removed_outbox = 0
    outbox_dir = ensure_within(base_dir, base_dir / "outbox")
    for child in list(outbox_dir.iterdir()):
        if child.resolve() in kept_outbox_set:
            continue
        removed_outbox += remove_path(child)

    removed_work_runs = 0
    if bool(janitor_settings.get("purge_work_runs", True)):
        removed_work_runs = clear_dir_contents(ensure_within(base_dir, base_dir / "work" / "runs"))

    removed_logs_runs = 0
    if bool(janitor_settings.get("purge_logs_runs", True)):
        removed_logs_runs = clear_dir_contents(ensure_within(base_dir, base_dir / "logs" / "runs"))

    removed_manifest_pages = 0
    if bool(janitor_settings.get("purge_manifest_pages", True)):
        removed_manifest_pages = clear_dir_contents(ensure_within(base_dir, base_dir / "manifests" / "pages"))

    removed_manifest_regions = 0
    if bool(janitor_settings.get("purge_manifest_regions", True)):
        removed_manifest_regions = clear_dir_contents(ensure_within(base_dir, base_dir / "manifests" / "regions"))

    removed_review_runtime = 0
    if bool(janitor_settings.get("purge_review_runtime", True)):
        for relative in (
            Path("review_inbox"),
            Path("review_outbox"),
            Path("review_work"),
            Path("review_manifests"),
            Path("review_logs"),
        ):
            removed_review_runtime += clear_dir_contents(ensure_within(base_dir, base_dir / relative))

    removed_run_manifests = 0
    removed_run_manifest_aux = 0
    run_manifest_kept: list[str] = []
    run_manifest_dir = ensure_within(base_dir, base_dir / "manifests" / "runs")
    run_manifest_dir.mkdir(parents=True, exist_ok=True)
    keep_manifests_for_kept_runs = bool(janitor_settings.get("keep_manifests_for_kept_outbox_runs", False))

    if bool(janitor_settings.get("purge_run_manifests", True)):
        for child in list(run_manifest_dir.iterdir()):
            if child.is_dir():
                removed_run_manifest_aux += remove_path(child)
                continue
            run_id = extract_run_id(child.name)
            keep_this = keep_manifests_for_kept_runs and run_id in kept_run_ids
            if keep_this:
                run_manifest_kept.append(child.name)
                continue
            if child.suffix.lower() == ".json":
                removed_run_manifests += remove_path(child)
            elif child.suffix.lower() in {".bak", ".lock"}:
                if bool(janitor_settings.get("purge_run_manifest_locks_and_backups", True)):
                    removed_run_manifest_aux += remove_path(child)
                else:
                    run_manifest_kept.append(child.name)
            else:
                removed_run_manifest_aux += remove_path(child)

    removed_inbox = 0
    if bool(janitor_settings.get("purge_inbox", False)):
        removed_inbox = clear_dir_contents(ensure_within(base_dir, base_dir / "inbox"))

    return JanitorSummary(
        kept_outbox=kept_outbox_names,
        removed_outbox=removed_outbox,
        removed_work_runs=removed_work_runs,
        removed_logs_runs=removed_logs_runs,
        removed_manifest_pages=removed_manifest_pages,
        removed_manifest_regions=removed_manifest_regions,
        removed_review_runtime=removed_review_runtime,
        removed_run_manifests=removed_run_manifests,
        removed_run_manifest_aux=removed_run_manifest_aux,
        removed_inbox=removed_inbox,
        run_manifest_kept=sorted(run_manifest_kept),
        validation=validation,
    )


def run_janitor_cleanup(
    base_dir: Path,
    janitor_settings: dict[str, Any],
    explicit_keep_outbox: Path | None = None,
    report_path: Path | None = None,
) -> JanitorSummary:
    summary = run_janitor(
        base_dir=base_dir.resolve(),
        janitor_settings=janitor_settings,
        explicit_keep=explicit_keep_outbox.resolve() if explicit_keep_outbox else None,
    )

    resolved_report = (report_path or (base_dir / DEFAULT_REPORT_RELATIVE_PATH)).resolve()
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    resolved_report.write_text(json.dumps(summary.as_dict(), indent=2) + "\n", encoding="utf-8")
    return summary
