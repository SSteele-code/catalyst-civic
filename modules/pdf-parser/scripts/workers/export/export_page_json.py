import sys
import os
import json
import datetime

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.common.constants import ESCALATION_HUMAN_REVIEW


PAGE_SCHEMA_VERSION = "catalyst_page_export.v2"
RUN_SCHEMA_VERSION = "catalyst_run_export.v2"


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_text_file(path):
    if not path or not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_json_file(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def alnum_count(text):
    return sum(1 for ch in (text or "") if ch.isalnum())


def load_region_manifests(region_manifest_dir, region_ids):
    regions = []
    for region_id in sorted(region_ids or []):
        region_path = os.path.join(region_manifest_dir, f"{region_id}.json")
        if not os.path.exists(region_path):
            continue
        with open(region_path, "r", encoding="utf-8") as f:
            regions.append(json.load(f))
    return regions


def summarize_regions(regions):
    counts = {
        "total": len(regions),
        "text_block": 0,
        "table_region": 0
    }

    for region in regions:
        region_type = region.get("region_type")
        if region_type == "text_block":
            counts["text_block"] += 1
        elif region_type == "table_region":
            counts["table_region"] += 1

    return counts


def bbox_contains_center(container_bbox, item_bbox):
    cx = item_bbox[0] + (item_bbox[2] / 2.0)
    cy = item_bbox[1] + (item_bbox[3] / 2.0)
    x0, y0, width, height = container_bbox
    return x0 <= cx <= (x0 + width) and y0 <= cy <= (y0 + height)


def is_ledger_line(line_text):
    if not line_text:
        return False
    tokens = line_text.split()
    numeric_tokens = sum(1 for token in tokens if any(ch.isdigit() for ch in token))
    alpha_tokens = sum(1 for token in tokens if any(ch.isalpha() for ch in token))
    return (
        numeric_tokens >= 2 and alpha_tokens >= 1
    ) or "$" in line_text or "%" in line_text


def cluster_positions(positions, tolerance):
    sorted_positions = sorted(int(position) for position in positions)
    clusters = []
    for position in sorted_positions:
        if not clusters or abs(position - clusters[-1]["center"]) > tolerance:
            clusters.append({"center": float(position), "values": [position]})
            continue
        clusters[-1]["values"].append(position)
        clusters[-1]["center"] = sum(clusters[-1]["values"]) / len(clusters[-1]["values"])
    return [int(round(cluster["center"])) for cluster in clusters]


def build_table_object(selected_words, selected_lines, bbox, source, extraction_thresholds):
    if not selected_words or not selected_lines:
        return None

    table_row_min_words = extraction_thresholds.get("table_row_min_words", 3)
    table_min_rows = extraction_thresholds.get("table_min_rows", 4)
    table_column_tolerance = extraction_thresholds.get("table_column_tolerance", 80)

    line_lookup = {line["line_id"]: line for line in selected_lines}
    grouped_rows = []
    for line in selected_lines:
        row_words = sorted(
            [word for word in selected_words if word.get("line_id") == line["line_id"]],
            key=lambda item: item["bbox"][0]
        )
        if not row_words:
            continue
        if len(row_words) < table_row_min_words and not is_ledger_line(line.get("text", "")):
            continue
        grouped_rows.append({
            "line_id": line["line_id"],
            "bbox": line["bbox"],
            "words": row_words,
            "text": line.get("text", "")
        })

    if len(grouped_rows) < table_min_rows:
        return None

    column_centers = cluster_positions([word["bbox"][0] for word in selected_words], table_column_tolerance)
    if len(column_centers) < 3:
        return None

    rows = []
    populated_cells = 0
    total_cells = 0
    for row_index, row in enumerate(grouped_rows):
        cells = [""] * len(column_centers)
        for word in row["words"]:
            nearest_column = min(
                range(len(column_centers)),
                key=lambda index: abs(word["bbox"][0] - column_centers[index])
            )
            if cells[nearest_column]:
                cells[nearest_column] = f"{cells[nearest_column]} {word['text']}".strip()
            else:
                cells[nearest_column] = word["text"]

        rows.append({
            "row_index": row_index,
            "line_id": row["line_id"],
            "text": row["text"],
            "bbox": row["bbox"],
            "cells": cells
        })
        populated_cells += sum(1 for cell in cells if cell.strip())
        total_cells += len(cells)

    used_columns = []
    for index in range(len(column_centers)):
        if any(row["cells"][index].strip() for row in rows):
            used_columns.append(index)

    if len(used_columns) < 3:
        return None

    column_centers = [column_centers[index] for index in used_columns]
    for row in rows:
        row["cells"] = [row["cells"][index] for index in used_columns]

    header = []
    data_rows = rows
    if rows:
        first_row = rows[0]
        first_row_alpha = sum(1 for cell in first_row["cells"] if any(ch.isalpha() for ch in cell))
        first_row_numeric = sum(1 for cell in first_row["cells"] if any(ch.isdigit() for ch in cell))
        if first_row_alpha >= max(2, first_row_numeric):
            header = first_row["cells"]
            data_rows = rows[1:]

    if len(data_rows) < table_min_rows - 1:
        return None

    populated_ratio = (populated_cells / total_cells) if total_cells else 0.0
    confidence = min(0.95, 0.40 + (populated_ratio * 0.30) + (len(data_rows) * 0.02) + (len(column_centers) * 0.03))

    return {
        "source": source,
        "bbox": bbox,
        "confidence": round(confidence, 2),
        "column_count": len(column_centers),
        "row_count": len(data_rows),
        "column_positions": column_centers,
        "header": header,
        "rows": data_rows
    }


def build_table_objects(words, lines, regions, quality, extraction_thresholds):
    words = words or []
    lines = lines or []
    if not words or not lines:
        return []

    line_by_id = {line["line_id"]: line for line in lines}
    table_objects = []

    for region in [region for region in regions if region.get("region_type") == "table_region"]:
        region_words = [word for word in words if bbox_contains_center(region["bbox"], word["bbox"])]
        if not region_words:
            continue
        region_line_ids = {word["line_id"] for word in region_words}
        region_lines = [line_by_id[line_id] for line_id in region_line_ids if line_id in line_by_id]
        table_object = build_table_object(region_words, region_lines, region["bbox"], "table_region", extraction_thresholds)
        if table_object:
            table_objects.append(table_object)

    family_hint = quality.get("page_family_hint")
    if not table_objects and family_hint == "ledger":
        ledger_lines = [line for line in lines if is_ledger_line(line.get("text", ""))]
        if ledger_lines:
            ledger_line_ids = {line["line_id"] for line in ledger_lines}
            ledger_words = [word for word in words if word.get("line_id") in ledger_line_ids]
            x0 = min(word["bbox"][0] for word in ledger_words)
            y0 = min(word["bbox"][1] for word in ledger_words)
            x1 = max(word["bbox"][0] + word["bbox"][2] for word in ledger_words)
            y1 = max(word["bbox"][1] + word["bbox"][3] for word in ledger_words)
            table_object = build_table_object(
                ledger_words,
                ledger_lines,
                [x0, y0, x1 - x0, y1 - y0],
                "page_wide_ledger",
                extraction_thresholds
            )
            if table_object:
                table_objects.append(table_object)

    return table_objects


def build_quality_section(page_manifest, ocr_artifact):
    text_metrics = ocr_artifact.get("text_metrics", {})
    return {
        "page_family_hint": ocr_artifact.get("family_hint", page_manifest.get("page_family_hint", "unknown")),
        "ocr_quality_score": page_manifest.get("ocr_quality_score", 0.0),
        "prose_score": safe_float(text_metrics.get("prose_score"), page_manifest.get("ocr_prose_score", 0.0)),
        "ledger_score": safe_float(text_metrics.get("ledger_score"), page_manifest.get("ocr_ledger_score", 0.0)),
        "mean_word_confidence": safe_float(ocr_artifact.get("mean_word_confidence"), page_manifest.get("ocr_mean_confidence", 0.0)),
        "candidate_count": page_manifest.get("ocr_candidate_count", len(ocr_artifact.get("candidate_summaries", []))),
        "winning_candidate": ocr_artifact.get("best_candidate", {}),
        "candidates": ocr_artifact.get("candidate_summaries", []),
        "blank_detection": ocr_artifact.get("blank_detection", page_manifest.get("ocr_blank_detection", {}))
    }


def derive_review_state(page_manifest, text, region_summary, quality, table_objects, extraction_thresholds):
    reasons = []
    text_alnum = alnum_count(text)
    blank_detection = quality.get("blank_detection", {})
    verified_blank = blank_detection.get("verified_blank", False)
    visually_nonblank = blank_detection.get("visually_nonblank", False)
    mean_word_confidence = safe_float(quality.get("mean_word_confidence"), 0.0)
    family_hint = quality.get("page_family_hint", "unknown")
    ledger_score = safe_float(quality.get("ledger_score"), 0.0)
    prose_score = safe_float(quality.get("prose_score"), 0.0)
    structural = page_manifest.get("structural_signals", {})
    has_structure = region_summary["total"] >= 8 or structural.get("has_table_structure", False) or bool(table_objects)

    if page_manifest.get("review_required", False):
        reasons.append("pre_flagged_review")
    if page_manifest.get("escalation_policy") == ESCALATION_HUMAN_REVIEW:
        reasons.append("human_review_escalation")
    if page_manifest.get("handwriting_detected", False):
        reasons.append("handwriting_suspected")

    if not verified_blank and visually_nonblank and text_alnum <= 2:
        reasons.append("failed_nonblank_ocr")
    if page_manifest.get("page_type") == "unknown" and has_structure and text_alnum <= 24 and not verified_blank:
        reasons.append("unknown_low_text_structured_page")

    if family_hint == "ledger" and not verified_blank:
        if mean_word_confidence < extraction_thresholds.get("word_confidence_review_threshold", 45.0):
            reasons.append("low_confidence_ledger_ocr")
        if ledger_score < extraction_thresholds.get("ledger_quality_review_threshold", 220.0):
            reasons.append("ledger_quality_low")
        if region_summary["table_region"] > 0 and not table_objects:
            reasons.append("table_structure_unresolved")

    if family_hint in ["prose", "dense_text"] and not verified_blank:
        if text_alnum >= 80 and mean_word_confidence < extraction_thresholds.get("word_confidence_review_threshold", 45.0):
            reasons.append("low_confidence_ocr")
        if text_alnum >= 80 and prose_score < extraction_thresholds.get("prose_quality_review_threshold", 120.0):
            reasons.append("prose_quality_low")

    deduped_reasons = []
    seen = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        deduped_reasons.append(reason)

    review_required = len(deduped_reasons) > 0
    escalation_policy = ESCALATION_HUMAN_REVIEW if review_required else "none"
    status = "verified_blank" if verified_blank else ("review_required" if review_required else "ready")
    return review_required, deduped_reasons, escalation_policy, status


def build_page_export(run_manifest, page_manifest, text, regions, ocr_artifact, extraction_thresholds):
    route_type = page_manifest.get("route_type", "unknown")
    extraction_engine = page_manifest.get("extraction_engine_used", "unknown")
    region_summary = summarize_regions(regions)
    words = ocr_artifact.get("words", [])
    lines = ocr_artifact.get("lines", [])
    quality = build_quality_section(page_manifest, ocr_artifact)
    table_objects = build_table_objects(words, lines, regions, quality, extraction_thresholds)
    review_required, review_reasons, escalation_policy, review_status = derive_review_state(
        page_manifest,
        text,
        region_summary,
        quality,
        table_objects,
        extraction_thresholds
    )

    return {
        "schema_version": PAGE_SCHEMA_VERSION,
        "run": {
            "run_id": run_manifest["run_id"],
            "source_pdf_name": run_manifest.get("source_pdf_original_name"),
            "source_pdf_hash": run_manifest.get("source_pdf_hash"),
            "created_at": run_manifest.get("created_at")
        },
        "page": {
            "page_id": page_manifest["page_id"],
            "source_page_number": page_manifest.get("source_page_number"),
            "page_type": page_manifest.get("page_type", "unknown"),
            "page_type_confidence": page_manifest.get("page_type_confidence", 0.0),
            "decision_reason": page_manifest.get("decision_reason"),
            "current_state": page_manifest.get("current_state"),
            "review_required": review_required,
            "review_reasons": review_reasons,
            "escalation_policy": escalation_policy
        },
        "route": {
            "type": route_type,
            "confidence": page_manifest.get("route_confidence", 0.0),
            "engine": extraction_engine,
            "native_text_detected": page_manifest.get("native_text_detected", False),
            "native_text_quality_score": page_manifest.get("native_text_quality_score", 0.0),
            "handwriting_detected": page_manifest.get("handwriting_detected", False),
            "family_hint": page_manifest.get("page_family_hint", quality.get("page_family_hint")),
            "requires_full_page_extraction": page_manifest.get("requires_full_page_extraction", route_type.startswith("ocr_")),
            "ocr_variant_used": page_manifest.get("ocr_variant_used"),
            "ocr_alnum_count": page_manifest.get("ocr_alnum_count"),
            "ocr_mean_confidence": page_manifest.get("ocr_mean_confidence"),
            "ocr_candidate_count": page_manifest.get("ocr_candidate_count")
        },
        "text": {
            "content": text,
            "char_count": len(text),
            "alnum_count": alnum_count(text),
            "line_count": len(lines)
        },
        "words": words,
        "lines": lines,
        "table_objects": table_objects,
        "quality": quality,
        "review": {
            "required": review_required,
            "reasons": review_reasons,
            "status": review_status,
            "escalation_policy": escalation_policy
        },
        "layout": {
            "region_counts": region_summary,
            "regions": regions
        },
        "signals": {
            "classification_scores": page_manifest.get("classification_scores", {}),
            "semantic_features": page_manifest.get("semantic_features", {}),
            "structural_signals": page_manifest.get("structural_signals", {})
        }
    }


def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    region_manifest_dir = os.path.join(manifest_dir, "regions")

    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r", encoding="utf-8") as f:
        thresholds = json.load(f)
    extraction_thresholds = thresholds.get("extraction", {})

    output_dir = os.path.join(base_dir, "work", "runs", run_id, "machine_readable")
    pages_dir = os.path.join(output_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    logger = PipelineLogger(log_dir, "export_page_json")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)

    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])

        logger.info("PAGE_EXPORT_START", "SUCCESS", run_id=run_id, message=f"Exporting {len(page_ids)} pages to JSON.")

        page_file_paths = []
        page_exports = []
        review_required_count = 0
        verified_blank_count = 0
        table_object_page_count = 0
        page_family_counts = {}

        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            text_path = page_manifest.get("ocr_text_path")
            full_text_path = os.path.join(base_dir, text_path) if text_path else None
            text = read_text_file(full_text_path)

            artifact_path = page_manifest.get("ocr_artifact_path")
            full_artifact_path = os.path.join(base_dir, artifact_path) if artifact_path else None
            ocr_artifact = read_json_file(full_artifact_path)
            if not ocr_artifact:
                ocr_artifact = {
                    "family_hint": page_manifest.get("page_family_hint", "unknown"),
                    "best_candidate": {},
                    "candidate_summaries": [],
                    "text_metrics": {
                        "prose_score": page_manifest.get("ocr_prose_score", 0.0),
                        "ledger_score": page_manifest.get("ocr_ledger_score", 0.0)
                    },
                    "mean_word_confidence": page_manifest.get("ocr_mean_confidence", 0.0),
                    "blank_detection": page_manifest.get("ocr_blank_detection", {}),
                    "words": [],
                    "lines": []
                }

            regions = load_region_manifests(region_manifest_dir, page_manifest.get("region_ids", []))
            page_export = build_page_export(run_manifest, page_manifest, text, regions, ocr_artifact, extraction_thresholds)
            page_exports.append(page_export)

            review_required = page_export["review"]["required"]
            review_reasons = page_export["review"]["reasons"]
            escalation_policy = page_export["review"]["escalation_policy"]
            if (
                review_required != page_manifest.get("review_required", False)
                or review_reasons != page_manifest.get("review_reasons", [])
                or escalation_policy != page_manifest.get("escalation_policy", "none")
            ):
                page_handler.update(page_id, {
                    "review_required": review_required,
                    "review_reasons": review_reasons,
                    "escalation_policy": escalation_policy
                })

            if review_required:
                review_required_count += 1
            if page_export["review"]["status"] == "verified_blank":
                verified_blank_count += 1
            if page_export["table_objects"]:
                table_object_page_count += 1
            family_hint = page_export["quality"]["page_family_hint"]
            page_family_counts[family_hint] = page_family_counts.get(family_hint, 0) + 1

            relative_page_path = os.path.join("pages", f"{page_id}.json")
            output_path = os.path.join(output_dir, relative_page_path)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(page_export, f, indent=2)
                f.write("\n")

            page_file_paths.append(relative_page_path.replace("\\", "/"))
            logger.info("PAGE_EXPORTED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Wrote {relative_page_path}")

        pages_jsonl_path = os.path.join(output_dir, "pages.jsonl")
        with open(pages_jsonl_path, "w", encoding="utf-8") as f:
            for page_export in page_exports:
                f.write(json.dumps(page_export) + "\n")

        run_export = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_manifest["run_id"],
            "source_pdf_name": run_manifest.get("source_pdf_original_name"),
            "source_pdf_hash": run_manifest.get("source_pdf_hash"),
            "created_at": run_manifest.get("created_at"),
            "exported_at": datetime.datetime.now().isoformat(),
            "page_count": len(page_ids),
            "review_required_count": review_required_count,
            "verified_blank_count": verified_blank_count,
            "table_object_page_count": table_object_page_count,
            "page_family_counts": page_family_counts,
            "page_files": page_file_paths,
            "pages_jsonl": "pages.jsonl"
        }

        with open(os.path.join(output_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(run_export, f, indent=2)
            f.write("\n")

        run_handler.update(run_id, {
            "machine_readable_output": {
                "root": os.path.join("work", "runs", run_id, "machine_readable"),
                "run_manifest": os.path.join("work", "runs", run_id, "machine_readable", "run.json"),
                "pages_jsonl": os.path.join("work", "runs", run_id, "machine_readable", "pages.jsonl"),
                "page_count": len(page_ids),
                "review_required_count": review_required_count,
                "verified_blank_count": verified_blank_count,
                "table_object_page_count": table_object_page_count
            },
            "status": "export_complete"
        })

        logger.info("PAGE_EXPORT_COMPLETE", "SUCCESS", run_id=run_id)

    except Exception as e:
        logger.error("PAGE_EXPORT_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python export_page_json.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
