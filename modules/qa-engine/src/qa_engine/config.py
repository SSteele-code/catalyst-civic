from __future__ import annotations

import json
from pathlib import Path


DEFAULT_THRESHOLDS = {
    "qa": {
        "host": "127.0.0.1",
        "port": 8093,
        "max_concurrent_runs": 1,
        "performance_sample_interval_seconds": 0.5,
        "source_text_min_chars_for_compare": 80,
        "source_ocr_fallback_enabled": True,
        "source_ocr_dpi": 240,
        "source_ocr_dpi_primary": 240,
        "source_ocr_dpi_fallback": 240,
        "source_ocr_adaptive_dpi_enabled": False,
        "source_ocr_psm_candidates": [3, 4, 6, 11],
        "source_ocr_rotation_candidates": [0],
        "source_ocr_workers": 5,
        "source_ocr_worker_backend": "thread",
        "source_ocr_max_inflight_factor": 2,
        "source_ocr_candidate_cascade_enabled": False,
        "source_ocr_cascade_quality_threshold": 0.6,
        "source_ocr_cascade_alignment_threshold": 0.2,
        "source_ocr_cascade_min_chars": 120,
        "source_ocr_timeout_seconds": 25.0,
        "source_reference_min_quality": 0.35,
        "source_ocr_parser_alignment_weight": 0.25,
        "source_ocr_min_alignment_for_reliable": 0.08,
        "page_pass_score": 0.8,
        "page_warn_score": 0.65,
        "run_pass_ratio": 0.95,
        "run_warn_ratio": 0.9,
        "run_accept_ratio": 0.95,
        "run_max_fail_ratio": 0.05,
        "run_min_comparable_pages": 60,
        "duplicate_min_chars": 80,
        "janitor_on_pass": True,
    },
    "janitor": {
        "keep_outbox_folders": 1,
        "purge_inbox": True,
        "purge_work_runs": True,
        "purge_logs_runs": True,
        "purge_manifests_pages": True,
        "purge_manifests_regions": True,
        "purge_run_manifests": True,
        "purge_review_runtime": True,
    },
}


def _deep_merge(base: dict, updates: dict) -> dict:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_thresholds(base_dir: Path) -> dict:
    config_path = base_dir / "config" / "thresholds.json"
    if not config_path.exists():
        return DEFAULT_THRESHOLDS
    with open(config_path, "r", encoding="utf-8") as f:
        file_thresholds = json.load(f)
    return _deep_merge(DEFAULT_THRESHOLDS, file_thresholds)


def get_service_settings(thresholds: dict) -> dict:
    return thresholds.get("qa", {})
