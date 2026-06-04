from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .validation import ensure_within


RUN_ID_PATTERN = re.compile(r"^(RUN_\d{4}_\d{2}_\d{2}_[A-Z0-9]{4})(?:_|$)")


def extract_run_id(name: str) -> str | None:
    match = RUN_ID_PATTERN.match(name)
    if not match:
        return None
    return match.group(1)


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


def choose_kept_outbox_folders(parser_root: Path, keep_count: int, explicit_keep: Path | None = None) -> list[Path]:
    outbox_dir = ensure_within(parser_root, parser_root / "outbox")
    outbox_dir.mkdir(parents=True, exist_ok=True)

    if explicit_keep is not None:
        keep_path = ensure_within(parser_root, explicit_keep)
        if keep_path.parent.resolve() != outbox_dir.resolve():
            raise RuntimeError(f"Explicit keep folder is not a parser outbox folder: {keep_path}")
        if not keep_path.exists() or not keep_path.is_dir():
            raise RuntimeError(f"Explicit keep folder not found: {keep_path}")
        return [keep_path]

    candidates = sorted((p for p in outbox_dir.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return []
    keep_count = max(1, int(keep_count))
    return candidates[:keep_count]


@dataclass
class ParserJanitorSummary:
    parser_root: str
    kept_outbox: list[str]
    removed_outbox: int
    removed_inbox: int
    removed_work_runs: int
    removed_logs_runs: int
    removed_manifests_pages: int
    removed_manifests_regions: int
    removed_run_manifests: int
    removed_review_runtime: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": datetime.now().isoformat(),
            "parser_root": self.parser_root,
            "kept_outbox": self.kept_outbox,
            "removed_outbox": self.removed_outbox,
            "removed_inbox": self.removed_inbox,
            "removed_work_runs": self.removed_work_runs,
            "removed_logs_runs": self.removed_logs_runs,
            "removed_manifests_pages": self.removed_manifests_pages,
            "removed_manifests_regions": self.removed_manifests_regions,
            "removed_run_manifests": self.removed_run_manifests,
            "removed_review_runtime": self.removed_review_runtime,
        }


def run_parser_janitor(
    parser_root: Path,
    janitor_settings: dict[str, Any],
    explicit_keep_outbox: Path | None = None,
) -> ParserJanitorSummary:
    parser_root = parser_root.resolve()
    keep_count = int(janitor_settings.get("keep_outbox_folders", 1))
    kept_outbox_paths = choose_kept_outbox_folders(parser_root, keep_count, explicit_keep_outbox)
    kept_outbox_set = {path.resolve() for path in kept_outbox_paths}
    kept_outbox_names = [path.name for path in kept_outbox_paths]
    kept_run_ids = {run_id for name in kept_outbox_names if (run_id := extract_run_id(name))}

    removed_outbox = 0
    outbox_dir = ensure_within(parser_root, parser_root / "outbox")
    for child in list(outbox_dir.iterdir()):
        if child.resolve() in kept_outbox_set:
            continue
        removed_outbox += remove_path(child)

    removed_inbox = 0
    if bool(janitor_settings.get("purge_inbox", True)):
        removed_inbox = clear_dir_contents(ensure_within(parser_root, parser_root / "inbox"))

    removed_work_runs = 0
    if bool(janitor_settings.get("purge_work_runs", True)):
        removed_work_runs = clear_dir_contents(ensure_within(parser_root, parser_root / "work" / "runs"))

    removed_logs_runs = 0
    if bool(janitor_settings.get("purge_logs_runs", True)):
        removed_logs_runs = clear_dir_contents(ensure_within(parser_root, parser_root / "logs" / "runs"))

    removed_manifests_pages = 0
    if bool(janitor_settings.get("purge_manifests_pages", True)):
        removed_manifests_pages = clear_dir_contents(ensure_within(parser_root, parser_root / "manifests" / "pages"))

    removed_manifests_regions = 0
    if bool(janitor_settings.get("purge_manifests_regions", True)):
        removed_manifests_regions = clear_dir_contents(ensure_within(parser_root, parser_root / "manifests" / "regions"))

    removed_run_manifests = 0
    if bool(janitor_settings.get("purge_run_manifests", True)):
        keep_run_manifests_for_kept = bool(janitor_settings.get("keep_run_manifests_for_kept_outbox", False))
        manifests_run_dir = ensure_within(parser_root, parser_root / "manifests" / "runs")
        manifests_run_dir.mkdir(parents=True, exist_ok=True)
        for child in list(manifests_run_dir.iterdir()):
            if child.is_dir():
                removed_run_manifests += remove_path(child)
                continue
            run_id = extract_run_id(child.name)
            if keep_run_manifests_for_kept and run_id in kept_run_ids:
                continue
            removed_run_manifests += remove_path(child)

    removed_review_runtime = 0
    if bool(janitor_settings.get("purge_review_runtime", True)):
        for relative in (
            Path("review_inbox"),
            Path("review_outbox"),
            Path("review_work"),
            Path("review_manifests"),
            Path("review_logs"),
        ):
            removed_review_runtime += clear_dir_contents(ensure_within(parser_root, parser_root / relative))

    return ParserJanitorSummary(
        parser_root=str(parser_root),
        kept_outbox=kept_outbox_names,
        removed_outbox=removed_outbox,
        removed_inbox=removed_inbox,
        removed_work_runs=removed_work_runs,
        removed_logs_runs=removed_logs_runs,
        removed_manifests_pages=removed_manifests_pages,
        removed_manifests_regions=removed_manifests_regions,
        removed_run_manifests=removed_run_manifests,
        removed_review_runtime=removed_review_runtime,
    )


def run_parser_janitor_cleanup(
    parser_root: Path,
    janitor_settings: dict[str, Any],
    explicit_keep_outbox: Path | None = None,
    report_path: Path | None = None,
) -> ParserJanitorSummary:
    summary = run_parser_janitor(
        parser_root=parser_root,
        janitor_settings=janitor_settings,
        explicit_keep_outbox=explicit_keep_outbox,
    )
    resolved_report = report_path.resolve() if report_path else (parser_root / "reports" / "QA_LAST_JANITOR.json").resolve()
    resolved_report.parent.mkdir(parents=True, exist_ok=True)
    resolved_report.write_text(json.dumps(summary.as_dict(), indent=2) + "\n", encoding="utf-8")
    return summary
