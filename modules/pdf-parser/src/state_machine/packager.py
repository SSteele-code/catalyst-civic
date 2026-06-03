from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler


PAGE_SCHEMA_VERSION = "catalyst_page_export.v5"
RUN_SCHEMA_VERSION = "catalyst_run_export.v5"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(base_dir: Path, page_manifest: dict) -> str:
    for relative_path in [page_manifest.get("ocr_text_path"), page_manifest.get("native_text_path")]:
        if not relative_path:
            continue
        full_path = base_dir / relative_path
        if full_path.exists():
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read()
    return ""


def display_source_name(run_manifest: dict) -> str:
    return str(
        run_manifest.get("source_pdf_display_name")
        or run_manifest.get("source_pdf_original_name")
        or run_manifest.get("source_pdf_alias_name")
        or run_manifest.get("source_pdf_intake_name")
        or "source.pdf"
    )


def build_page_export(base_dir: Path, run_manifest: dict, page_manifest: dict) -> dict:
    text = load_text(base_dir, page_manifest)
    extraction_payload = {}
    extraction_path = page_manifest.get("typed_extraction_path")
    if extraction_path:
        extraction_payload = load_json(base_dir / extraction_path)
    word_witness_payload = {}
    word_witness_path = page_manifest.get("word_witness_path")
    if word_witness_path:
        word_witness_payload = load_json(base_dir / word_witness_path)
    native_word_witness_payload = {}
    native_word_witness_path = page_manifest.get("native_word_witness_path")
    if native_word_witness_path:
        native_word_witness_payload = load_json(base_dir / native_word_witness_path)

    table_region_count = sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id)
    text_region_count = sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" not in region_id)

    return {
        "schema_version": PAGE_SCHEMA_VERSION,
        "run": {
            "run_id": run_manifest["run_id"],
            "job_id": run_manifest.get("job_id"),
            "document_machine_code": run_manifest.get("document_machine_code"),
            "source_pdf_name": display_source_name(run_manifest),
            "source_pdf_display_name": run_manifest.get("source_pdf_display_name"),
            "source_pdf_intake_name": run_manifest.get("source_pdf_intake_name"),
            "source_pdf_original_name": run_manifest.get("source_pdf_original_name"),
            "source_pdf_alias_name": run_manifest.get("source_pdf_alias_name"),
            "source_pdf_hash": run_manifest.get("source_pdf_hash"),
            "created_at": run_manifest.get("created_at"),
        },
        "page": {
            "page_id": page_manifest["page_id"],
            "run_page_id": page_manifest.get("run_page_id", page_manifest["page_id"]),
            "page_machine_code": page_manifest.get("page_machine_code"),
            "document_machine_code": page_manifest.get("document_machine_code", run_manifest.get("document_machine_code")),
            "source_page_number": page_manifest.get("source_page_number"),
            "page_type": page_manifest.get("page_type"),
            "function_type": page_manifest.get("function_type"),
            "function_type_source": page_manifest.get("function_type_source"),
            "layout_type": page_manifest.get("layout_type", page_manifest.get("page_layout")),
            "layout_type_source": page_manifest.get("layout_type_source", page_manifest.get("page_layout_source")),
            "support_role": page_manifest.get("support_role"),
            "support_role_source": page_manifest.get("support_role_source"),
            "support_role_confidence": page_manifest.get("support_role_confidence", 0.0),
            "support_role_reasons": page_manifest.get("support_role_reasons", []),
            "page_family": page_manifest.get("page_family"),
            "page_layout": page_manifest.get("page_layout"),
            "page_support_subtype": page_manifest.get("page_support_subtype"),
            "page_support_subtype_source": page_manifest.get("page_support_subtype_source"),
            "page_support_subtype_confidence": page_manifest.get("page_support_subtype_confidence", 0.0),
            "page_support_subtype_reasons": page_manifest.get("page_support_subtype_reasons", []),
            "sort_lane": page_manifest.get("sort_lane"),
            "sort_lane_source": page_manifest.get("sort_lane_source"),
            "sort_lane_reasons": page_manifest.get("sort_lane_reasons", []),
            "review_state": page_manifest.get("review_state"),
            "review_state_source": page_manifest.get("review_state_source"),
            "review_state_reasons": page_manifest.get("review_state_reasons", []),
            "page_type_confidence": page_manifest.get("page_type_confidence", 0.0),
            "suspicion_score": page_manifest.get("suspicion_score", 0.0),
            "suspicion_reasons": page_manifest.get("suspicion_reasons", []),
            "decision_reason": page_manifest.get("decision_reason"),
            "current_state": page_manifest.get("current_state"),
            "routing_tags": ["route_agenda"] if page_manifest.get("page_type") == "agenda" or page_manifest.get("function_type") == "agenda" else [],
        },
        "route": {
            "type": page_manifest.get("route_type"),
            "confidence": page_manifest.get("route_confidence", 0.0),
            "native_text_detected": page_manifest.get("native_text_detected", False),
            "native_text_quality_score": page_manifest.get("native_text_quality_score", 0.0),
            "handwriting_detected": page_manifest.get("handwriting_detected", False),
            "cardinal_rotation_applied": page_manifest.get("cardinal_rotation_applied", 0),
            "cardinal_orientation_source": page_manifest.get("cardinal_orientation_source"),
            "cardinal_orientation_triggered": page_manifest.get("cardinal_orientation_triggered", False),
            "ocr_variant_used": page_manifest.get("ocr_variant_used"),
            "ocr_alnum_count": page_manifest.get("ocr_alnum_count"),
            "geometry_normalization_state": page_manifest.get("geometry_normalization_state"),
            "geometry_normalization_source": page_manifest.get("geometry_normalization_source"),
            "geometry_normalization_pass_count": page_manifest.get("geometry_normalization_pass_count", 0),
            "residual_skew_angle": page_manifest.get("residual_skew_angle", 0.0),
            "residual_skew_probe_angle": page_manifest.get("residual_skew_probe_angle", 0.0),
            "sort_lane": page_manifest.get("sort_lane"),
            "sort_lane_source": page_manifest.get("sort_lane_source"),
            "sort_lane_reasons": page_manifest.get("sort_lane_reasons", []),
        },
        "ocr_witness": {
            "state": page_manifest.get("ocr_witness_state"),
            "reasons": page_manifest.get("ocr_witness_reasons", []),
            "quality_score": page_manifest.get("ocr_quality_score", 0.0),
            "word_count": page_manifest.get("ocr_word_count", 0),
            "lexical_word_count": page_manifest.get("ocr_lexical_word_count", 0),
            "numeric_token_count": page_manifest.get("ocr_numeric_token_count", 0),
            "noise_ratio": page_manifest.get("ocr_noise_ratio", 0.0),
            "selection_score": page_manifest.get("ocr_selection_score", 0.0),
            "selection_margin": page_manifest.get("ocr_selection_margin", 0.0),
            "retry_used": page_manifest.get("ocr_retry_used", False),
            "cardinal_orientation_candidates": page_manifest.get("cardinal_orientation_candidates", []),
            "cardinal_orientation_ocr_candidates": page_manifest.get("cardinal_orientation_ocr_candidates", []),
            "candidate_summaries": page_manifest.get("ocr_candidate_summaries", []),
        },
        "word_witness": {
            "path": word_witness_path,
            "engine": page_manifest.get("word_witness_engine"),
            "coordinate_space": page_manifest.get("word_witness_coordinate_space"),
            "word_count": page_manifest.get("word_witness_word_count", 0),
            "line_count": page_manifest.get("word_witness_line_count", 0),
            "source_variant": page_manifest.get("word_witness_source_variant"),
            "line_strategy": page_manifest.get("word_witness_line_strategy"),
            "data": word_witness_payload,
        },
        "native_word_witness": {
            "path": native_word_witness_path,
            "word_count": page_manifest.get("native_word_witness_word_count", 0),
            "line_count": page_manifest.get("native_word_witness_line_count", 0),
            "line_strategy": native_word_witness_payload.get("line_strategy"),
            "data": native_word_witness_payload,
        },
        "text": {
            "content": text,
            "char_count": len(text),
            "alnum_count": sum(1 for ch in text if ch.isalnum()),
        },
        "visual_features": {
            "table_region_count": table_region_count,
            "text_region_count": text_region_count,
            "region_count": len(page_manifest.get("region_ids", [])),
            "detected_skew_angle": page_manifest.get("detected_skew_angle", 0.0),
            "page_type_signals": page_manifest.get("page_type_signals", {}),
            "page_type_candidates": page_manifest.get("page_type_candidates", []),
        },
        "extraction": extraction_payload,
    }


def flatten_word_rows(page_export: dict, witness_key: str, witness_role: str) -> list[dict]:
    witness = page_export.get(witness_key) or {}
    witness_data = witness.get("data") or {}
    words = witness_data.get("words") or []
    page_info = page_export.get("page") or {}
    route_info = page_export.get("route") or {}
    ocr_witness = page_export.get("ocr_witness") or {}
    rows: list[dict] = []

    for word in words:
        rows.append(
            {
                "schema_version": "catalyst_word_row.v1",
                "witness_role": witness_role,
                "run_id": page_export["run"]["run_id"],
                "document_machine_code": page_export["run"].get("document_machine_code"),
                "source_pdf_hash": page_export["run"].get("source_pdf_hash"),
                "page_id": page_info.get("page_id"),
                "page_machine_code": page_info.get("page_machine_code"),
                "source_page_number": page_info.get("source_page_number"),
                "page_type": page_info.get("page_type"),
                "function_type": page_info.get("function_type"),
                "layout_type": page_info.get("layout_type"),
                "sort_lane": page_info.get("sort_lane"),
                "review_state": page_info.get("review_state"),
                "route_type": route_info.get("type"),
                "ocr_witness_state": ocr_witness.get("state"),
                "engine": witness.get("engine") or witness_data.get("engine"),
                "coordinate_space": witness.get("coordinate_space") or witness_data.get("coordinate_space"),
                "source_variant": witness.get("source_variant") or witness_data.get("source_variant"),
                "line_strategy": witness.get("line_strategy") or witness_data.get("line_strategy"),
                "page_size": witness_data.get("page_size"),
                "word_id": word.get("word_id"),
                "line_id": word.get("line_id"),
                "block_id": word.get("block_id"),
                "reading_order": word.get("reading_order"),
                "text": word.get("text"),
                "bbox": word.get("bbox"),
                "confidence": word.get("confidence"),
            }
        )
    return rows


def flatten_line_rows(page_export: dict, witness_key: str, witness_role: str) -> list[dict]:
    witness = page_export.get(witness_key) or {}
    witness_data = witness.get("data") or {}
    lines = witness_data.get("lines") or []
    page_info = page_export.get("page") or {}
    route_info = page_export.get("route") or {}
    ocr_witness = page_export.get("ocr_witness") or {}
    rows: list[dict] = []

    for line in lines:
        rows.append(
            {
                "schema_version": "catalyst_line_row.v1",
                "witness_role": witness_role,
                "run_id": page_export["run"]["run_id"],
                "document_machine_code": page_export["run"].get("document_machine_code"),
                "source_pdf_hash": page_export["run"].get("source_pdf_hash"),
                "page_id": page_info.get("page_id"),
                "page_machine_code": page_info.get("page_machine_code"),
                "source_page_number": page_info.get("source_page_number"),
                "page_type": page_info.get("page_type"),
                "function_type": page_info.get("function_type"),
                "layout_type": page_info.get("layout_type"),
                "sort_lane": page_info.get("sort_lane"),
                "review_state": page_info.get("review_state"),
                "route_type": route_info.get("type"),
                "ocr_witness_state": ocr_witness.get("state"),
                "engine": witness.get("engine") or witness_data.get("engine"),
                "coordinate_space": witness.get("coordinate_space") or witness_data.get("coordinate_space"),
                "source_variant": witness.get("source_variant") or witness_data.get("source_variant"),
                "line_strategy": witness.get("line_strategy") or witness_data.get("line_strategy"),
                "page_size": witness_data.get("page_size"),
                "line_id": line.get("line_id"),
                "block_id": line.get("block_id"),
                "reading_order": line.get("reading_order"),
                "text": line.get("text"),
                "bbox": line.get("bbox"),
                "word_ids": line.get("word_ids", []),
                "word_count": len(line.get("word_ids", [])),
            }
        )
    return rows


def package_run(base_dir: Path, run_id: str) -> dict:
    log_dir = base_dir / "logs" / "runs" / run_id
    manifest_dir = base_dir / "manifests"
    run_handler = ManifestHandler(manifest_dir / "runs")
    page_handler = ManifestHandler(manifest_dir / "pages")
    logger = PipelineLogger(log_dir, "state_machine_packager")

    run_manifest = run_handler.load(run_id)
    page_ids = run_manifest.get("page_ids", [])
    work_output_dir = base_dir / "work" / "runs" / run_id / "machine_readable"
    work_output_dir.mkdir(parents=True, exist_ok=True)

    page_type_counts: dict[str, int] = {}
    page_file_paths = []
    page_exports = []
    word_rows: list[dict] = []
    line_rows: list[dict] = []
    native_word_rows: list[dict] = []
    native_line_rows: list[dict] = []

    logger.info("PACKAGING_START", "SUCCESS", run_id=run_id, message=f"Packaging {len(page_ids)} pages.")

    for page_id in page_ids:
        page_manifest = page_handler.load(page_id)
        page_export = build_page_export(base_dir, run_manifest, page_manifest)
        page_exports.append(page_export)
        word_rows.extend(flatten_word_rows(page_export, "word_witness", "canonical"))
        line_rows.extend(flatten_line_rows(page_export, "word_witness", "canonical"))
        native_word_rows.extend(flatten_word_rows(page_export, "native_word_witness", "native"))
        native_line_rows.extend(flatten_line_rows(page_export, "native_word_witness", "native"))
        page_type = page_export["page"]["page_type"] or "unknown"
        page_type_counts[page_type] = page_type_counts.get(page_type, 0) + 1

        relative_path = Path("pages") / f"{page_id}.json"
        absolute_path = work_output_dir / relative_path
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        with open(absolute_path, "w", encoding="utf-8") as f:
            json.dump(page_export, f, indent=2)
            f.write("\n")
        page_file_paths.append(relative_path.as_posix())

    pages_jsonl_path = work_output_dir / "pages.jsonl"
    with open(pages_jsonl_path, "w", encoding="utf-8") as f:
        for page_export in page_exports:
            f.write(json.dumps(page_export) + "\n")

    words_jsonl_path = work_output_dir / "words.jsonl"
    with open(words_jsonl_path, "w", encoding="utf-8") as f:
        for row in word_rows:
            f.write(json.dumps(row) + "\n")

    lines_jsonl_path = work_output_dir / "lines.jsonl"
    with open(lines_jsonl_path, "w", encoding="utf-8") as f:
        for row in line_rows:
            f.write(json.dumps(row) + "\n")

    native_words_jsonl_relative = None
    if native_word_rows:
        native_words_jsonl_relative = "native_words.jsonl"
        with open(work_output_dir / native_words_jsonl_relative, "w", encoding="utf-8") as f:
            for row in native_word_rows:
                f.write(json.dumps(row) + "\n")

    native_lines_jsonl_relative = None
    if native_line_rows:
        native_lines_jsonl_relative = "native_lines.jsonl"
        with open(work_output_dir / native_lines_jsonl_relative, "w", encoding="utf-8") as f:
            for row in native_line_rows:
                f.write(json.dumps(row) + "\n")

    run_export = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_manifest["run_id"],
        "job_id": run_manifest.get("job_id"),
        "document_machine_code": run_manifest.get("document_machine_code"),
        "source_pdf_name": display_source_name(run_manifest),
        "source_pdf_display_name": run_manifest.get("source_pdf_display_name"),
        "source_pdf_intake_name": run_manifest.get("source_pdf_intake_name"),
        "source_pdf_original_name": run_manifest.get("source_pdf_original_name"),
        "source_pdf_alias_name": run_manifest.get("source_pdf_alias_name"),
        "source_pdf_hash": run_manifest.get("source_pdf_hash"),
        "created_at": run_manifest.get("created_at"),
        "packaged_at": datetime.datetime.now().isoformat(),
        "page_count": len(page_ids),
        "word_count": len(word_rows),
        "line_count": len(line_rows),
        "native_word_count": len(native_word_rows),
        "native_line_count": len(native_line_rows),
        "page_type_counts": page_type_counts,
        "function_type_counts": run_manifest.get("function_type_counts", {}),
        "layout_type_counts": run_manifest.get("layout_type_counts", {}),
        "support_role_counts": run_manifest.get("support_role_counts", {}),
        "page_family_counts": run_manifest.get("page_family_counts", {}),
        "page_layout_counts": run_manifest.get("page_layout_counts", {}),
        "page_support_subtype_counts": run_manifest.get("page_support_subtype_counts", {}),
        "sort_lane_counts": run_manifest.get("sort_lane_counts", {}),
        "review_state_counts": run_manifest.get("review_state_counts", {}),
        "suspicious_page_count": run_manifest.get("suspicious_page_count", 0),
        "suspicious_pages": run_manifest.get("suspicious_pages", []),
        "page_files": page_file_paths,
        "pages_jsonl": "pages.jsonl",
        "words_jsonl": "words.jsonl",
        "lines_jsonl": "lines.jsonl",
        "native_words_jsonl": native_words_jsonl_relative,
        "native_lines_jsonl": native_lines_jsonl_relative,
        "worker_timings": run_manifest.get("worker_timings", []),
        "geometry_normalization_stats": run_manifest.get("geometry_normalization_stats"),
        "feature_pipeline_stats": run_manifest.get("feature_pipeline_stats"),
        "runtime_metrics": run_manifest.get("runtime_metrics"),
    }
    with open(work_output_dir / "run.json", "w", encoding="utf-8") as f:
        json.dump(run_export, f, indent=2)
        f.write("\n")

    output_root = base_dir / "outbox" / f"{run_id}_{Path(run_manifest.get('source_pdf_intake_name') or display_source_name(run_manifest)).stem}"
    machine_readable_output = output_root / "machine_readable"
    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(work_output_dir, machine_readable_output)

    handoff_payload = {
        "run_id": run_id,
        "job_id": run_manifest.get("job_id"),
        "document_machine_code": run_manifest.get("document_machine_code"),
        "status": "handoff_ready",
        "output_folder": str(output_root),
        "machine_readable_folder": str(machine_readable_output),
        "source_pdf_name": display_source_name(run_manifest),
        "source_pdf_display_name": run_manifest.get("source_pdf_display_name"),
        "source_pdf_intake_name": run_manifest.get("source_pdf_intake_name"),
        "source_pdf_original_name": run_manifest.get("source_pdf_original_name"),
        "source_pdf_alias_name": run_manifest.get("source_pdf_alias_name"),
        "source_pdf_hash": run_manifest.get("source_pdf_hash"),
        "page_count": len(page_ids),
        "word_count": len(word_rows),
        "line_count": len(line_rows),
        "native_word_count": len(native_word_rows),
        "native_line_count": len(native_line_rows),
        "pages_jsonl": str(machine_readable_output / "pages.jsonl"),
        "words_jsonl": str(machine_readable_output / "words.jsonl"),
        "lines_jsonl": str(machine_readable_output / "lines.jsonl"),
        "native_words_jsonl": str(machine_readable_output / native_words_jsonl_relative) if native_words_jsonl_relative else None,
        "native_lines_jsonl": str(machine_readable_output / native_lines_jsonl_relative) if native_lines_jsonl_relative else None,
        "page_type_counts": page_type_counts,
        "function_type_counts": run_manifest.get("function_type_counts", {}),
        "layout_type_counts": run_manifest.get("layout_type_counts", {}),
        "support_role_counts": run_manifest.get("support_role_counts", {}),
        "page_family_counts": run_manifest.get("page_family_counts", {}),
        "page_layout_counts": run_manifest.get("page_layout_counts", {}),
        "page_support_subtype_counts": run_manifest.get("page_support_subtype_counts", {}),
        "sort_lane_counts": run_manifest.get("sort_lane_counts", {}),
        "review_state_counts": run_manifest.get("review_state_counts", {}),
        "suspicious_page_count": run_manifest.get("suspicious_page_count", 0),
        "suspicious_pages": run_manifest.get("suspicious_pages", []),
        "worker_timings": run_manifest.get("worker_timings", []),
        "geometry_normalization_stats": run_manifest.get("geometry_normalization_stats"),
        "feature_pipeline_stats": run_manifest.get("feature_pipeline_stats"),
        "runtime_metrics": run_manifest.get("runtime_metrics"),
    }
    with open(output_root / "handoff.json", "w", encoding="utf-8") as f:
        json.dump(handoff_payload, f, indent=2)
        f.write("\n")
    with open(machine_readable_output / "handoff.json", "w", encoding="utf-8") as f:
        json.dump(handoff_payload, f, indent=2)
        f.write("\n")
    with open(output_root / "SUCCESS.txt", "w", encoding="utf-8") as f:
        f.write(f"Run {run_id} completed successfully.\n")

    run_handler.update(
        run_id,
        {
            "packaged_output": {
                "root": str(output_root),
                "machine_readable": str(machine_readable_output),
                "handoff": str(output_root / "handoff.json"),
                "machine_readable_handoff": str(machine_readable_output / "handoff.json"),
            },
            "page_type_counts": page_type_counts,
            "status": "handoff_ready",
        },
    )

    logger.info("PACKAGING_COMPLETE", "SUCCESS", run_id=run_id, message=str(output_root))
    return handoff_payload
