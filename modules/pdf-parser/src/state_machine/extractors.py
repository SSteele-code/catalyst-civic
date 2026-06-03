from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.state_machine.page_types import resolve_extractor_name


DATE_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
CURRENCY_PATTERN = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")


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


def load_page_evidence(base_dir: Path, page_manifest: dict, text: str) -> dict:
    word_witness = load_json(base_dir / page_manifest["word_witness_path"]) if page_manifest.get("word_witness_path") else {}
    native_word_witness = (
        load_json(base_dir / page_manifest["native_word_witness_path"]) if page_manifest.get("native_word_witness_path") else {}
    )
    return {
        "text": text,
        "word_witness": word_witness,
        "native_word_witness": native_word_witness,
        "route_type": page_manifest.get("route_type"),
        "ocr_witness_state": page_manifest.get("ocr_witness_state"),
        "ocr_witness_reasons": page_manifest.get("ocr_witness_reasons", []),
        "sort_lane": page_manifest.get("sort_lane"),
        "review_state": page_manifest.get("review_state"),
        "review_state_reasons": page_manifest.get("review_state_reasons", []),
    }


def nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def heading_candidates(lines: list[str], limit: int = 6) -> list[str]:
    headings = []
    for line in lines[: limit * 2]:
        if len(headings) >= limit:
            break
        if len(line) <= 140:
            headings.append(line)
    return headings


def extract_dates(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in DATE_PATTERN.finditer(text or "")))


def extract_currency_values(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in CURRENCY_PATTERN.finditer(text or "")))


def canonical_witness(evidence: dict | None) -> dict:
    if not evidence:
        return {}
    word_witness = evidence.get("word_witness") or {}
    if word_witness.get("words") or word_witness.get("lines"):
        return word_witness
    return evidence.get("native_word_witness") or {}


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return float(ordered[mid - 1] + ordered[mid]) / 2.0


def witness_lines(evidence: dict | None) -> list[dict]:
    return list((canonical_witness(evidence).get("lines") or []))


def witness_words(evidence: dict | None) -> list[dict]:
    return list((canonical_witness(evidence).get("words") or []))


def witness_page_width(evidence: dict | None) -> float:
    witness = canonical_witness(evidence)
    page_size = witness.get("page_size") or []
    if len(page_size) >= 1:
        return float(page_size[0])
    return 0.0


def line_words(line: dict, word_index: dict[str, dict]) -> list[dict]:
    words = [word_index[word_id] for word_id in line.get("word_ids", []) if word_id in word_index]
    return sorted(words, key=lambda word: float(word.get("bbox", [0.0])[0]))


def split_words_into_cells(words: list[dict], page_width: float) -> list[dict]:
    if not words:
        return []
    widths = [float(word.get("bbox", [0.0, 0.0, 0.0, 0.0])[2]) for word in words]
    gap_threshold = max(24.0, median(widths) * 2.5, page_width * 0.035)

    cells: list[list[dict]] = [[words[0]]]
    for word in words[1:]:
        previous = cells[-1][-1]
        previous_bbox = previous.get("bbox", [0.0, 0.0, 0.0, 0.0])
        current_bbox = word.get("bbox", [0.0, 0.0, 0.0, 0.0])
        previous_right = float(previous_bbox[0]) + float(previous_bbox[2])
        gap = float(current_bbox[0]) - previous_right
        if gap >= gap_threshold:
            cells.append([word])
        else:
            cells[-1].append(word)

    structured_cells = []
    for index, cell_words in enumerate(cells, start=1):
        x0 = min(float(word["bbox"][0]) for word in cell_words)
        y0 = min(float(word["bbox"][1]) for word in cell_words)
        x1 = max(float(word["bbox"][0]) + float(word["bbox"][2]) for word in cell_words)
        y1 = max(float(word["bbox"][1]) + float(word["bbox"][3]) for word in cell_words)
        cell_text = " ".join(str(word.get("text") or "").strip() for word in cell_words if str(word.get("text") or "").strip()).strip()
        structured_cells.append(
            {
                "cell_index": index,
                "text": cell_text,
                "bbox": [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)],
                "word_ids": [word.get("word_id") for word in cell_words],
                "numeric_token_count": sum(1 for token in re.findall(r"\S+", cell_text) if any(ch.isdigit() for ch in token)),
            }
        )
    return structured_cells


def build_structured_rows(evidence: dict | None, limit: int = 60) -> list[dict]:
    lines = witness_lines(evidence)
    words = witness_words(evidence)
    if not lines or not words:
        return []
    word_index = {word.get("word_id"): word for word in words}
    page_width = witness_page_width(evidence)
    rows = []
    for line in lines[:limit]:
        row_words = line_words(line, word_index)
        cells = split_words_into_cells(row_words, page_width)
        row_text = str(line.get("text") or "").strip()
        rows.append(
            {
                "line_id": line.get("line_id"),
                "text": row_text,
                "bbox": line.get("bbox"),
                "reading_order": line.get("reading_order"),
                "cell_count": len(cells),
                "cells": cells,
                "numeric_cell_count": sum(1 for cell in cells if int(cell.get("numeric_token_count", 0)) > 0),
            }
        )
    return rows


def summarize_column_hints(rows: list[dict], max_columns: int = 8) -> list[dict]:
    columns: dict[int, list[float]] = {}
    for row in rows:
        for index, cell in enumerate(row.get("cells", [])):
            columns.setdefault(index, []).append(float(cell.get("bbox", [0.0])[0]))

    hints = []
    for index, positions in sorted(columns.items()):
        if not positions:
            continue
        hints.append(
            {
                "column_index": index + 1,
                "x_anchor": round(median(positions), 2),
                "observations": len(positions),
            }
        )
        if len(hints) >= max_columns:
            break
    return hints


def extract_key_value_pairs_from_rows(rows: list[dict], limit: int = 40) -> list[dict]:
    pairs: list[dict] = []
    for row in rows:
        row_text = str(row.get("text") or "").strip()
        if not row_text:
            continue
        if ":" in row_text and 0 < row_text.find(":") <= 40:
            key, value = row_text.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key:
                pairs.append({"key": key, "value": value, "source": "colon_line", "line_id": row.get("line_id")})
        elif row.get("cell_count", 0) >= 2:
            first_cell = row["cells"][0]
            trailing_cells = row["cells"][1:]
            key = str(first_cell.get("text") or "").strip(" :")
            value = " | ".join(str(cell.get("text") or "").strip() for cell in trailing_cells if str(cell.get("text") or "").strip()).strip()
            if key and value and len(key) <= 48:
                pairs.append({"key": key, "value": value, "source": "aligned_cells", "line_id": row.get("line_id")})
        if len(pairs) >= limit:
            break
    return pairs


def extract_blank_separator(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    return {
        "type": "blank_separator",
        "is_separator": True,
        "reason": "low_signal_or_empty_page",
        "char_count": len(text or ""),
    }


def extract_agenda(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    items = []
    for line in lines:
        if re.match(r"^(?:[IVXLC]+\.|[A-Z]\.|[a-z]\.|[0-9]+\.)\s*", line):
            items.append(line)
    return {
        "type": "agenda",
        "title_lines": heading_candidates(lines, limit=4),
        "agenda_items": items,
        "dates": extract_dates(text),
    }


def extract_minutes(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    motions = [line for line in lines if re.search(r"\bmotion\b|\bsecond(?:ed)?\b|\bvote(?:d|s)?\b", line, re.IGNORECASE)]
    sections = [line for line in lines if line.endswith(":") or line.isupper()]
    return {
        "type": "minutes",
        "title_lines": heading_candidates(lines, limit=4),
        "section_headings": sections[:20],
        "motion_lines": motions[:30],
        "dates": extract_dates(text),
    }


def extract_reference_or_procedure(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    procedure_terms = [
        line
        for line in lines
        if re.search(
            r"robert'?s rules|parliamentary procedure|point of order|previous question|question of privilege|motion",
            line,
            re.IGNORECASE,
        )
    ]
    section_headings = [
        line
        for line in lines
        if re.match(r"^(?:article|section)\s+[ivx0-9]+", line, re.IGNORECASE) or line.isupper()
    ]
    return {
        "type": "reference_or_procedure",
        "title_lines": heading_candidates(lines, limit=5),
        "section_headings": section_headings[:25],
        "procedure_terms": procedure_terms[:30],
        "dates": extract_dates(text),
    }


def extract_legislative_prose(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    whereas_clauses = [line for line in lines if re.match(r"^whereas\b", line, re.IGNORECASE)]
    operative_lines = [line for line in lines if re.search(r"be it resolved|now[, ]+therefore", line, re.IGNORECASE)]
    return {
        "type": "legislative_prose",
        "title_lines": heading_candidates(lines, limit=5),
        "whereas_clauses": whereas_clauses[:20],
        "operative_lines": operative_lines[:10],
        "dates": extract_dates(text),
        "currency_values": extract_currency_values(text),
    }


def extract_contract_or_agreement(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    parties = []
    for line in lines[:40]:
        if re.search(r"\bbetween\b", line, re.IGNORECASE):
            parties.append(line)
    article_headings = [line for line in lines if re.match(r"^(?:article|section)\s+[ivx0-9]+", line, re.IGNORECASE)]
    return {
        "type": "contract_or_agreement",
        "title_lines": heading_candidates(lines, limit=5),
        "party_lines": parties[:10],
        "article_headings": article_headings[:25],
        "dates": extract_dates(text),
        "currency_values": extract_currency_values(text),
    }


def extract_financial_report(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    ledger_lines = [
        line
        for line in lines
        if len(CURRENCY_PATTERN.findall(line)) or re.search(r"\brevenues?\b|\bexpenditures?\b|\bbudget\b", line, re.IGNORECASE)
    ]
    return {
        "type": "financial_report",
        "title_lines": heading_candidates(lines, limit=5),
        "currency_values": extract_currency_values(text)[:100],
        "ledger_lines": ledger_lines[:40],
        "dates": extract_dates(text),
        "table_region_count": sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id),
    }


def extract_government_form(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    form_ids = [line for line in lines[:20] if re.search(r"\bform\s+\S+", line, re.IGNORECASE)]
    field_lines = [line for line in lines if re.search(r"\benter\b|\bbox\b|mm/dd/yyyy|phone no\.", line, re.IGNORECASE)]
    return {
        "type": "government_form",
        "title_lines": heading_candidates(lines, limit=5),
        "form_identifiers": form_ids[:5],
        "field_lines": field_lines[:30],
        "dates": extract_dates(text),
    }


def extract_invoice(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    amount_due = next((value for value in extract_currency_values(text)), None)
    identifiers = [line for line in lines[:30] if re.search(r"invoice|bill to|remit", line, re.IGNORECASE)]
    return {
        "type": "invoice",
        "title_lines": heading_candidates(lines, limit=4),
        "identifier_lines": identifiers[:12],
        "amount_due_candidate": amount_due,
        "dates": extract_dates(text),
    }


def extract_structured_table(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    rows = build_structured_rows(evidence, limit=80)
    numeric_rows = [row["text"] for row in rows if row.get("numeric_cell_count", 0) > 0]
    title_lines = heading_candidates([row["text"] for row in rows if row.get("text")] or nonempty_lines(text), limit=5)
    return {
        "type": "structured_table",
        "title_lines": title_lines,
        "table_region_count": sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id),
        "row_count": len(rows),
        "column_hints": summarize_column_hints(rows),
        "rows": rows[:40],
        "numeric_rows": numeric_rows[:40],
        "currency_values": extract_currency_values(text)[:100],
    }


def extract_structured_form(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    rows = build_structured_rows(evidence, limit=80)
    key_value_pairs = extract_key_value_pairs_from_rows(rows, limit=50)
    form_ids = [line for line in nonempty_lines(text)[:20] if re.search(r"\bform\s+\S+", line, re.IGNORECASE)]
    field_labels = [pair["key"] for pair in key_value_pairs[:40]]
    return {
        "type": "structured_form",
        "title_lines": heading_candidates([row["text"] for row in rows if row.get("text")] or nonempty_lines(text), limit=5),
        "form_identifiers": form_ids[:5],
        "field_labels": field_labels,
        "key_value_pairs": key_value_pairs,
        "row_preview": rows[:30],
        "dates": extract_dates(text),
    }


def extract_structured_mixed(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    rows = build_structured_rows(evidence, limit=80)
    key_value_pairs = extract_key_value_pairs_from_rows(rows, limit=30)
    prose_preview = [row["text"] for row in rows if row.get("cell_count", 0) <= 1][:20]
    return {
        "type": "structured_mixed",
        "title_lines": heading_candidates([row["text"] for row in rows if row.get("text")] or nonempty_lines(text), limit=5),
        "column_hints": summarize_column_hints(rows),
        "key_value_pairs": key_value_pairs,
        "rows": rows[:30],
        "prose_preview": prose_preview,
        "currency_values": extract_currency_values(text)[:60],
        "dates": extract_dates(text),
    }


def extract_weak_page_evidence(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    witness = canonical_witness(evidence)
    rows = build_structured_rows(evidence, limit=40)
    preview_lines = [row["text"] for row in rows if row.get("text")] or nonempty_lines(text)[:20]
    return {
        "type": "weak_page_evidence",
        "abstained": True,
        "review_state": (evidence or {}).get("review_state"),
        "ocr_witness_state": (evidence or {}).get("ocr_witness_state"),
        "ocr_witness_reasons": (evidence or {}).get("ocr_witness_reasons", []),
        "title_lines": heading_candidates(preview_lines, limit=4),
        "preview_lines": preview_lines[:20],
        "row_preview": rows[:20],
        "word_count": witness.get("word_count", 0),
        "line_count": witness.get("line_count", 0),
        "table_region_count": sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id),
        "note": "Weak pages preserve witness evidence and should not be treated as fully parsed structure.",
    }


def extract_table_or_mixed_layout(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    rows = build_structured_rows(evidence, limit=80)
    numeric_rows = [row["text"] for row in rows if row.get("numeric_cell_count", 0) > 0]
    return {
        "type": "table_or_mixed_layout",
        "title_lines": heading_candidates([row["text"] for row in rows if row.get("text")] or nonempty_lines(text), limit=4),
        "table_region_count": sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id),
        "column_hints": summarize_column_hints(rows),
        "numeric_rows": numeric_rows[:40],
        "rows": rows[:30],
        "currency_values": extract_currency_values(text)[:100],
    }


def extract_generic_prose(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    return {
        "type": "generic_prose",
        "title_lines": heading_candidates(lines, limit=5),
        "preview_lines": lines[:20],
        "dates": extract_dates(text),
        "currency_values": extract_currency_values(text)[:20],
    }


def extract_powerpoint(page_manifest: dict, text: str, evidence: dict | None = None) -> dict:
    lines = nonempty_lines(text)
    bullets = [line for line in lines if re.match(r"^[-*•o]\s+", line)]
    return {
        "type": "powerpoint",
        "title_lines": heading_candidates(lines, limit=3),
        "bullets": bullets[:30],
        "preview_lines": lines[:15],
    }


EXTRACTOR_HANDLERS = {
    "extract_blank_separator": extract_blank_separator,
    "extract_agenda": extract_agenda,
    "extract_minutes": extract_minutes,
    "extract_reference_or_procedure": extract_reference_or_procedure,
    "extract_legislative_prose": extract_legislative_prose,
    "extract_contract_or_agreement": extract_contract_or_agreement,
    "extract_financial_report": extract_financial_report,
    "extract_government_form": extract_government_form,
    "extract_invoice": extract_invoice,
    "extract_structured_table": extract_structured_table,
    "extract_structured_form": extract_structured_form,
    "extract_structured_mixed": extract_structured_mixed,
    "extract_weak_page_evidence": extract_weak_page_evidence,
    "extract_table_or_mixed_layout": extract_table_or_mixed_layout,
    "extract_generic_prose": extract_generic_prose,
    "extract_powerpoint": extract_powerpoint,
}


def run_page_type_extraction(base_dir: Path, run_id: str) -> dict:
    log_dir = base_dir / "logs" / "runs" / run_id
    manifest_dir = base_dir / "manifests"
    run_handler = ManifestHandler(manifest_dir / "runs")
    page_handler = ManifestHandler(manifest_dir / "pages")
    logger = PipelineLogger(log_dir, "state_machine_extractors")

    run_manifest = run_handler.load(run_id)
    page_ids = run_manifest.get("page_ids", [])
    output_dir = base_dir / "work" / "runs" / run_id / "typed_extraction"
    output_dir.mkdir(parents=True, exist_ok=True)

    extraction_counts: dict[str, int] = {}
    logger.info("PAGE_EXTRACTION_START", "SUCCESS", run_id=run_id, message=f"Extracting {len(page_ids)} pages.")

    for page_id in page_ids:
        page_manifest = page_handler.load(page_id)
        page_type = page_manifest.get("page_type", "generic_prose")
        function_type = page_manifest.get("function_type", "unknown")
        layout_type = page_manifest.get("layout_type", page_manifest.get("page_layout"))
        support_role = page_manifest.get("support_role", "standalone")
        extractor_name, extractor_selection_source = resolve_extractor_name(page_manifest)
        extractor = EXTRACTOR_HANDLERS[extractor_name]
        text = load_text(base_dir, page_manifest)
        evidence = load_page_evidence(base_dir, page_manifest, text)
        payload = extractor(page_manifest, text, evidence)
        artifact = {
            "schema_version": "catalyst_page_payload.v2",
            "generated_at": datetime.datetime.now().isoformat(),
            "page_id": page_id,
            "run_page_id": page_manifest.get("run_page_id", page_id),
            "page_machine_code": page_manifest.get("page_machine_code"),
            "document_machine_code": page_manifest.get("document_machine_code"),
            "source_pdf_name": page_manifest.get("source_pdf_name"),
            "source_pdf_intake_name": page_manifest.get("source_pdf_intake_name"),
            "source_pdf_original_name": page_manifest.get("source_pdf_original_name"),
            "source_pdf_alias_name": page_manifest.get("source_pdf_alias_name"),
            "page_type": page_type,
            "function_type": function_type,
            "layout_type": layout_type,
            "support_role": support_role,
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
            "support_role_source": page_manifest.get("support_role_source"),
            "support_role_confidence": page_manifest.get("support_role_confidence", 0.0),
            "support_role_reasons": page_manifest.get("support_role_reasons", []),
            "suspicion_score": page_manifest.get("suspicion_score", 0.0),
            "extractor": extractor_name,
            "extractor_selection_source": extractor_selection_source,
            "payload": payload,
        }

        artifact_path = output_dir / f"{page_id}.json"
        with open(artifact_path, "w", encoding="utf-8") as f:
            json.dump(artifact, f, indent=2)
            f.write("\n")

        relative_path = artifact_path.relative_to(base_dir).as_posix()
        extraction_counts[page_type] = extraction_counts.get(page_type, 0) + 1
        page_handler.update(
            page_id,
            {
                "typed_extraction_path": relative_path,
                "typed_extraction_status": "complete",
                "page_family_hint": page_manifest.get("page_family", page_type),
                "function_type_hint": function_type,
                "current_state": "extracted",
            },
        )
        logger.info("PAGE_EXTRACTED", "SUCCESS", run_id=run_id, page_id=page_id, message=page_type)

    run_handler.update(
        run_id,
        {
            "typed_extraction_counts": extraction_counts,
            "status": "extracted",
        },
    )
    logger.info("PAGE_EXTRACTION_COMPLETE", "SUCCESS", run_id=run_id)
    return extraction_counts
