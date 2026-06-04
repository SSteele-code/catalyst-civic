import os
#!/usr/bin/env python
"""
Ordinance/Resolution PRE_PARSE (Agenda-Staging Lane)

Transforms staged ordinance/resolution metadata-only pull artifacts into normalized
parse records suitable for:
  1) table push projection
  2) CCO glossary projection

Writes to:
  _Sources/M1-Meetings/Ordinance_Resolution/_output/<ordinance_resolution_code>/

Strict invariant:
  - PRE_PARSE only (schema normalization + lineage packaging + table/CCO projection)
  - No DB writes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


OR_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Ordinance_Resolution"
STAGING_ROOT = OR_ROOT / "_staging"
OUTPUT_ROOT = OR_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = OR_ROOT / "ordinance_resolution_preparse_state.json"
MANIFEST_FILE = OR_ROOT / "M1_ORDINANCE_RESOLUTION_PREPARSE_MANIFEST.jsonl"

SOURCE_SCHEMA_VERSION = "m1.ordinance_resolution.pull.v1"
SCHEMA_VERSION = "m1.ordinance_resolution.parse.v1"
GLOSSARY_SCHEMA_VERSION = "m1.ordinance_resolution.glossary.v1"
TABLE_PROJECTION_SCHEMA_VERSION = "m1.ordinance_resolution.table_projection.v1"
CCO_PROJECTION_SCHEMA_VERSION = "m1.ordinance_resolution.cco_projection.v1"
SOURCE_LANE = "agenda_output_ordinance_resolution_metadata_only"
JURISDICTION = "Richlands"

AG_SOURCE_CODE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
RUN_DIR_RE = re.compile(r"^RUN_\d{8}T\d{6}$", re.IGNORECASE)


@dataclass
class StageDocCandidate:
    stage_json_path: Path
    stage_json_sha256: str
    source_stage_run_id: str
    source_stage_captured_at: str
    source_packet_code: str
    source_pdf_code: str
    anchor_meeting_date: str | None
    source_txt_path: Path
    source_factsheet_path: Path
    source_packet_txt_path: Path
    source_packet_txt_sha256: str
    source_lane: str
    jurisdiction: str
    document_index: int
    document_type: str
    document_number: str | None
    document_number_raw: str | None
    title: str | None
    header_line: str | None
    start_line: int | None
    end_line: int | None
    page_hint: str | None
    match_pattern: str | None
    confidence: float


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_name(text: str) -> str:
    return normalize_ws((text or "").strip(" ,;:.|"))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    OR_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    OR_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def load_manifest_codes() -> set[str]:
    codes: set[str] = set()
    if not MANIFEST_FILE.exists():
        return codes
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue
        code = str(row.get("ordinance_resolution_code") or "").strip()
        if code:
            codes.add(code)
    return codes


def to_int(value: object, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def safe_token(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    token = re.sub(r"[^A-Za-z0-9]+", "_", value.upper()).strip("_")
    if not token:
        return fallback
    if len(token) > 24:
        token = token[:24]
    return token


def normalize_document_number(document_number: str | None, document_type: str) -> tuple[str | None, str | None]:
    if document_number is None:
        return None, None
    raw = clean_name(document_number)
    if not raw:
        return None, None
    norm = raw.upper().replace(" ", "")

    # OCR harmonization
    lead = norm[0]
    tail = norm[1:] if len(norm) > 1 else ""
    if document_type == "ORDINANCE" and lead in {"0", "Q", "9"}:
        norm = "O" + tail
    if document_type == "RESOLUTION" and lead == "0":
        norm = "O" + tail

    return norm, raw


def iter_stage_json_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.ordinance_resolution.json"))


def latest_stage_run_id(root: Path) -> str | None:
    if not root.exists():
        return None
    run_dirs = [p for p in root.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name)]
    if not run_dirs:
        return None
    run_dirs.sort(key=lambda p: p.name, reverse=True)
    return run_dirs[0].name


def iter_stage_json_files_scoped(
    root: Path, source_run_id: str | None, all_staging: bool
) -> tuple[list[Path], str]:
    if all_staging:
        return list(iter_stage_json_files(root)), "ALL_STAGING"
    run_id = source_run_id or latest_stage_run_id(root)
    if not run_id:
        return [], "NONE"
    run_dir = root / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        return [], run_id
    return sorted(run_dir.rglob("*.ordinance_resolution.json")), run_id


def doc_dedupe_key(document_type: str, document_number: str | None, title: str | None, header: str | None) -> str:
    t = document_type.upper().strip() or "DOCUMENT"
    n = normalize_ws(document_number or "").upper()
    if n:
        return f"{t}|N|{n}"
    ttl = normalize_ws(title or "").upper()
    if ttl:
        return f"{t}|T|{ttl}"
    hdr = normalize_ws(header or "").upper()
    return f"{t}|H|{hdr}"


def doc_quality_score(doc: dict) -> tuple[float, int, int, int]:
    confidence = to_float(doc.get("confidence"), default=0.0)
    title_len = len(clean_name(str(doc.get("title") or "")))
    has_number = 1 if clean_name(str(doc.get("document_number") or "")) else 0
    start_line = to_int(doc.get("start_line"), default=10**9) or 10**9
    return (confidence, has_number, title_len, -start_line)


def dedupe_documents(documents: list[dict]) -> list[dict]:
    best_by_key: dict[str, dict] = {}
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        document_type = clean_name(str(doc.get("document_type") or "")).upper() or "DOCUMENT"
        document_number = clean_name(str(doc.get("document_number") or "")) or None
        title = clean_name(str(doc.get("title") or "")) or None
        header = clean_name(str(doc.get("header_line") or "")) or None
        key = doc_dedupe_key(document_type, document_number, title, header)
        prev = best_by_key.get(key)
        if prev is None or doc_quality_score(doc) > doc_quality_score(prev):
            best_by_key[key] = doc
    out = list(best_by_key.values())
    out.sort(key=lambda d: (to_int(d.get("start_line"), default=10**9) or 10**9, str(d.get("document_type") or "")))
    return out


def parse_stage_documents(stage_json_path: Path) -> tuple[list[StageDocCandidate], str | None]:
    try:
        stage_text = stage_json_path.read_text(encoding="utf-8")
        payload = json.loads(stage_text)
    except Exception as exc:
        return [], f"invalid_stage_json: {exc}"

    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version != SOURCE_SCHEMA_VERSION:
        return [], f"unsupported_schema_version: {schema_version}"

    source_pdf_code = clean_name(str(payload.get("source_pdf_code") or ""))
    source_packet_code = clean_name(str(payload.get("packet_code") or ""))
    if not source_pdf_code:
        return [], "missing_source_pdf_code"
    if not source_packet_code:
        return [], "missing_packet_code"

    source_txt_path = Path(str(payload.get("source_txt") or "").strip())
    source_factsheet_path = Path(str(payload.get("source_factsheet") or "").strip())
    source_packet_txt_path = Path(str(payload.get("source_packet_txt") or "").strip())
    if not source_txt_path.exists():
        return [], "missing_source_txt"
    if not source_factsheet_path.exists():
        return [], "missing_source_factsheet"
    if not source_packet_txt_path.exists():
        return [], "missing_source_packet_txt"

    documents = payload.get("documents")
    if not isinstance(documents, list):
        return [], "missing_documents"
    documents = dedupe_documents(documents)
    if not documents:
        return [], "no_documents_after_dedupe"

    source_stage_run_id = clean_name(str(payload.get("run_id") or ""))
    source_stage_captured_at = clean_name(str(payload.get("captured_at") or ""))
    source_lane = clean_name(str(payload.get("source_lane") or SOURCE_LANE)) or SOURCE_LANE
    jurisdiction = clean_name(str(payload.get("jurisdiction") or JURISDICTION)) or JURISDICTION
    anchor_meeting_date = clean_name(str(payload.get("anchor_meeting_date") or "")) or None
    source_packet_txt_sha256 = clean_name(str(payload.get("source_packet_txt_sha256") or "")) or ""
    stage_json_sha256 = sha256_text(stage_text)

    out: list[StageDocCandidate] = []
    for idx, doc in enumerate(documents, start=1):
        document_type = clean_name(str(doc.get("document_type") or "")).upper() or "DOCUMENT"
        document_number_norm, document_number_raw = normalize_document_number(
            clean_name(str(doc.get("document_number") or "")) or None,
            document_type=document_type,
        )
        out.append(
            StageDocCandidate(
                stage_json_path=stage_json_path,
                stage_json_sha256=stage_json_sha256,
                source_stage_run_id=source_stage_run_id,
                source_stage_captured_at=source_stage_captured_at,
                source_packet_code=source_packet_code,
                source_pdf_code=source_pdf_code,
                anchor_meeting_date=anchor_meeting_date,
                source_txt_path=source_txt_path,
                source_factsheet_path=source_factsheet_path,
                source_packet_txt_path=source_packet_txt_path,
                source_packet_txt_sha256=source_packet_txt_sha256,
                source_lane=source_lane,
                jurisdiction=jurisdiction,
                document_index=idx,
                document_type=document_type,
                document_number=document_number_norm,
                document_number_raw=document_number_raw
                or (clean_name(str(doc.get("document_number_raw") or "")) or None),
                title=clean_name(str(doc.get("title") or "")) or None,
                header_line=clean_name(str(doc.get("header_line") or "")) or None,
                start_line=to_int(doc.get("start_line")),
                end_line=to_int(doc.get("end_line")),
                page_hint=clean_name(str(doc.get("page_hint") or "")) or None,
                match_pattern=clean_name(str(doc.get("match_pattern") or "")) or None,
                confidence=to_float(doc.get("confidence"), default=0.0),
            )
        )
    return out, None


def build_ordinance_resolution_code(candidate: StageDocCandidate) -> str:
    match = AG_SOURCE_CODE_RE.match(candidate.source_pdf_code)
    docnum = "000000"
    created_ymd = "00000000"
    pulled_ymd = "00000000"
    if match:
        docnum, created_ymd, pulled_ymd = match.group(1), match.group(2), match.group(3)
    type_tag = "DOC"
    if candidate.document_type == "ORDINANCE":
        type_tag = "ORD"
    elif candidate.document_type == "RESOLUTION":
        type_tag = "RES"
    token = safe_token(candidate.document_number, fallback=f"D{candidate.document_index:03d}")
    return f"M1.AG.OR.{docnum}.{created_ymd}.{pulled_ymd}.{type_tag}.{token}"


def choose_best_candidate(candidates: Sequence[StageDocCandidate]) -> StageDocCandidate:
    def rank_key(c: StageDocCandidate) -> tuple[str, float, int, str]:
        start_line = c.start_line if c.start_line is not None else 10**9
        return (c.source_stage_captured_at, c.confidence, -start_line, str(c.stage_json_path))

    return sorted(candidates, key=rank_key, reverse=True)[0]


def build_glossary_section(candidate: StageDocCandidate, ordinance_resolution_code: str) -> dict:
    entries: list[dict] = []
    dedupe: set[tuple[str, str, str]] = set()
    by_category: dict[str, int] = {}

    title_and_header = normalize_ws(f"{candidate.title or ''} {candidate.header_line or ''}")

    def add_entry(
        category: str,
        canonical_name: str,
        fact_key: str,
        confidence: float,
        matched_from: str,
        evidence: str,
    ) -> None:
        name = clean_name(canonical_name)
        if not name:
            return
        key = (category.upper(), name.upper(), fact_key.upper())
        if key in dedupe:
            return
        dedupe.add(key)
        by_category[category.upper()] = by_category.get(category.upper(), 0) + 1
        entries.append(
            {
                "entry_id": f"GL{len(entries) + 1:03d}",
                "category": category.upper(),
                "canonical_name": name,
                "fact_key": fact_key.upper(),
                "confidence": round(float(confidence), 3),
                "matched_from": matched_from,
                "evidence": normalize_ws(evidence)[:280],
                "source_span": {
                    "source_packet_code": candidate.source_packet_code,
                    "start_line": candidate.start_line,
                    "end_line": candidate.end_line,
                },
            }
        )

    if candidate.document_number:
        fact_key = "ORDINANCE_NUMBER" if candidate.document_type == "ORDINANCE" else "RESOLUTION_NUMBER"
        add_entry(
            category="LEGAL_REFERENCE",
            canonical_name=candidate.document_number,
            fact_key=fact_key,
            confidence=max(candidate.confidence, 0.94),
            matched_from="document_number",
            evidence=candidate.header_line or candidate.title or candidate.document_number,
        )

    if candidate.title:
        add_entry(
            category="LEGAL_REFERENCE",
            canonical_name=candidate.title,
            fact_key="DOCUMENT_TITLE",
            confidence=max(candidate.confidence - 0.03, 0.86),
            matched_from="document_title",
            evidence=title_and_header,
        )

    if re.search(r"\bTOWN\s+COUNCIL\b", title_and_header, re.IGNORECASE):
        add_entry(
            category="BOARD",
            canonical_name="Richlands Town Council",
            fact_key="MENTIONED_IN_RECORD",
            confidence=0.92,
            matched_from="title_or_header",
            evidence=title_and_header,
        )

    if re.search(r"\bTOWN\s+OF\s+RICHLANDS\b", title_and_header, re.IGNORECASE):
        add_entry(
            category="ORGANIZATION",
            canonical_name="Town of Richlands",
            fact_key="MENTIONED_IN_RECORD",
            confidence=0.9,
            matched_from="title_or_header",
            evidence=title_and_header,
        )

    entries.sort(key=lambda e: (e["category"], e["canonical_name"], e["fact_key"]))
    for idx, entry in enumerate(entries, start=1):
        entry["entry_id"] = f"GL{idx:03d}"

    return {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "record_type": "ordinance_resolution_glossary_section",
        "source_ordinance_resolution_code": ordinance_resolution_code,
        "source_pdf_code": candidate.source_pdf_code,
        "glossary_scope_text_hint": "ordinance_resolution_metadata.document_title|header_line",
        "summary": {
            "entries_total": len(entries),
            "entries_by_category": by_category,
        },
        "entities": entries,
    }


def build_payload(candidate: StageDocCandidate, run_id: str, ordinance_resolution_code: str) -> dict:
    glossary = build_glossary_section(candidate, ordinance_resolution_code)
    meeting_date = candidate.anchor_meeting_date

    table_projection = {
        "schema_version": TABLE_PROJECTION_SCHEMA_VERSION,
        "record_type": "ordinance_resolution_table_projection",
        "ordinance_resolution_code": ordinance_resolution_code,
        "source_pdf_code": candidate.source_pdf_code,
        "packet_code": candidate.source_packet_code,
        "anchor_meeting_date": meeting_date,
        "document_type": candidate.document_type,
        "document_number": candidate.document_number,
        "document_title": candidate.title,
        "header_line": candidate.header_line,
        "start_line": candidate.start_line,
        "end_line": candidate.end_line,
        "match_pattern": candidate.match_pattern,
        "confidence": candidate.confidence,
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "ordinance_resolution_parse_record",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "parse_run_id": run_id,
        "source_lane": candidate.source_lane or SOURCE_LANE,
        "jurisdiction": candidate.jurisdiction or JURISDICTION,
        "ordinance_resolution_code": ordinance_resolution_code,
        "artifact_machine_code": ordinance_resolution_code,
        "linked_source_pdf_code": candidate.source_pdf_code,
        "meeting_context": {
            "anchor_meeting_date": meeting_date,
            "anchor_meeting_type": "COUNCIL_MEETING",
        },
        "lineage": {
            "source_stage_run_id": candidate.source_stage_run_id,
            "source_stage_captured_at": candidate.source_stage_captured_at,
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "source_packet_code": candidate.source_packet_code,
            "source_packet_txt_path": str(candidate.source_packet_txt_path),
            "source_packet_txt_sha256": candidate.source_packet_txt_sha256,
            "source_txt_path": str(candidate.source_txt_path),
            "source_factsheet_path": str(candidate.source_factsheet_path),
        },
        "ordinance_resolution_metadata": {
            "document_type": candidate.document_type,
            "document_number": candidate.document_number,
            "document_number_raw": candidate.document_number_raw,
            "document_title": candidate.title,
            "header_line": candidate.header_line,
            "start_line": candidate.start_line,
            "end_line": candidate.end_line,
            "page_hint": candidate.page_hint,
            "match_pattern": candidate.match_pattern,
            "confidence": candidate.confidence,
        },
        "table_projection": table_projection,
        "glossary": glossary,
        "cco_projection": {
            "schema_version": CCO_PROJECTION_SCHEMA_VERSION,
            "record_type": "ordinance_resolution_cco_projection",
            "glossary_schema_version": GLOSSARY_SCHEMA_VERSION,
            "entities_total": int(glossary.get("summary", {}).get("entries_total", 0)),
            "glossary_scope_text_hint": "ordinance_resolution_metadata.document_title|header_line",
        },
        "pusher_ready": {
            "record_id": ordinance_resolution_code,
            "source_id": candidate.source_pdf_code,
            "content_mode": "metadata_only",
            "is_complete_document": False,
            "document_type": candidate.document_type,
            "document_number": candidate.document_number,
            "document_title": candidate.title,
            "anchor_meeting_date": meeting_date,
            "table_projection_schema_version": TABLE_PROJECTION_SCHEMA_VERSION,
            "glossary_scope_text_hint": "ordinance_resolution_metadata.document_title|header_line",
        },
    }
    return payload


def render_summary_text(payload: dict) -> str:
    meta = payload.get("ordinance_resolution_metadata", {})
    glossary_summary = payload.get("glossary", {}).get("summary", {})
    lines = [
        f"ORDINANCE_RESOLUTION_CODE: {payload.get('ordinance_resolution_code')}",
        f"SOURCE_PDF_CODE: {payload.get('linked_source_pdf_code')}",
        f"PACKET_CODE: {payload.get('lineage', {}).get('source_packet_code')}",
        f"MEETING_DATE: {payload.get('meeting_context', {}).get('anchor_meeting_date') or ''}",
        f"DOCUMENT_TYPE: {meta.get('document_type') or ''}",
        f"DOCUMENT_NUMBER: {meta.get('document_number') or ''}",
        f"DOCUMENT_TITLE: {meta.get('document_title') or ''}",
        f"HEADER_LINE: {meta.get('header_line') or ''}",
        f"MATCH_PATTERN: {meta.get('match_pattern') or ''}",
        f"CONFIDENCE: {meta.get('confidence')}",
        f"GLOSSARY_ENTRIES: {glossary_summary.get('entries_total', 0)}",
    ]
    return "\n".join(lines).strip() + "\n"


def run_parse(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    source_run_id: str | None = None,
    all_staging: bool = False,
) -> None:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")

    state = load_state()
    state_records = state.setdefault("records", {})
    manifest_codes = load_manifest_codes()

    discovered = 0
    mapped = 0
    prepared = 0
    skipped_unchanged = 0
    failed = 0

    failure_rows: list[dict] = []
    prepared_rows: list[dict] = []

    groups: dict[str, list[StageDocCandidate]] = {}
    scoped_stage_files, effective_source_run_id = iter_stage_json_files_scoped(
        STAGING_ROOT,
        source_run_id=source_run_id,
        all_staging=all_staging,
    )

    for stage_json in scoped_stage_files:
        discovered += 1
        candidates, error = parse_stage_documents(stage_json)
        if error:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "source_stage_json_path": str(stage_json),
                    "error": error,
                }
            )
            continue

        for candidate in candidates:
            code = build_ordinance_resolution_code(candidate)
            groups.setdefault(code, []).append(candidate)
            mapped += 1

    chosen: list[tuple[str, StageDocCandidate]] = []
    for code, candidates in groups.items():
        chosen.append((code, choose_best_candidate(candidates)))
    chosen.sort(key=lambda item: item[0])
    if limit is not None:
        chosen = chosen[:limit]

    run_dir = RUNS_ROOT / run_id
    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)

    for code, candidate in chosen:
        prev = state_records.get(code, {})
        output_json = OUTPUT_ROOT / code / f"{code}.parse.json"
        output_txt = OUTPUT_ROOT / code / f"{code}.parse.txt"
        compat_preparse_json = OUTPUT_ROOT / code / f"{code}.preparse.json"
        compat_preparse_txt = OUTPUT_ROOT / code / f"{code}.preparse.txt"

        if (
            not force
            and prev.get("source_stage_json_sha256") == candidate.stage_json_sha256
            and output_json.exists()
            and output_txt.exists()
        ):
            skipped_unchanged += 1
            continue

        payload = build_payload(candidate, run_id, code)
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        payload_sha256 = sha256_text(payload_text)

        if not dry_run:
            out_dir = OUTPUT_ROOT / code
            out_dir.mkdir(parents=True, exist_ok=True)
            output_json.write_text(payload_text, encoding="utf-8")
            output_txt.write_text(render_summary_text(payload), encoding="utf-8")
            compat_preparse_json.write_text(payload_text, encoding="utf-8")
            compat_preparse_txt.write_text(render_summary_text(payload), encoding="utf-8")

        prepared += 1
        row = {
            "run_id": run_id,
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "ordinance_resolution_code": code,
            "source_pdf_code": candidate.source_pdf_code,
            "packet_code": candidate.source_packet_code,
            "document_type": candidate.document_type,
            "document_number": candidate.document_number,
            "document_title": candidate.title,
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "payload_sha256": payload_sha256,
            "output_json": str(output_json),
            "output_txt": str(output_txt),
            "output_json_compat_preparse": str(compat_preparse_json),
            "output_txt_compat_preparse": str(compat_preparse_txt),
            "glossary_entities_total": int(payload.get("glossary", {}).get("summary", {}).get("entries_total", 0)),
        }
        prepared_rows.append(row)

        state_records[code] = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_run_id": run_id,
            "last_status": "prepared",
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "payload_sha256": payload_sha256,
            "output_json": str(output_json),
            "output_txt": str(output_txt),
            "source_pdf_code": candidate.source_pdf_code,
            "document_type": candidate.document_type,
            "document_number": candidate.document_number,
            "glossary_entities_total": int(payload.get("glossary", {}).get("summary", {}).get("entries_total", 0)),
        }
        if not dry_run:
            save_state(state)
            if code not in manifest_codes:
                append_manifest_rows([row])
                manifest_codes.add(code)

    if not dry_run:
        run_manifest = run_dir / "ordinance_resolution_parse_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in prepared_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            run_failures = run_dir / "ordinance_resolution_parse_failures.jsonl"
            with run_failures.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "source_schema_version": SOURCE_SCHEMA_VERSION,
            "source_lane": SOURCE_LANE,
            "staging_root": str(STAGING_ROOT),
            "source_stage_scope": effective_source_run_id,
            "output_root": str(OUTPUT_ROOT),
            "discovered_stage_json": discovered,
            "mapped_document_candidates": mapped,
            "prepared_records": prepared,
            "skipped_unchanged": skipped_unchanged,
            "failed": failed,
            "limit": limit,
            "force": force,
            "dry_run": dry_run,
        }
        (run_dir / "run_summary.json").write_text(
            json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("=" * 72)
    print("ORDINANCE/RESOLUTION PRE_PARSE SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Source lane: {SOURCE_LANE}")
    print(f"  Staging root: {STAGING_ROOT}")
    print(f"  Source stage scope: {effective_source_run_id}")
    print(f"  Output root: {OUTPUT_ROOT}")
    print(f"  Stage files discovered: {discovered}")
    print(f"  Document candidates mapped: {mapped}")
    print(f"  Records prepared: {prepared}")
    print(f"  Records skipped (unchanged): {skipped_unchanged}")
    print(f"  Records failed: {failed}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Run artifacts: {run_dir}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
    print("=" * 72)


def run_preparse(limit: int | None = None, force: bool = False, dry_run: bool = False) -> None:
    run_parse(limit=limit, force=force, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-parse ordinance/resolution staging artifacts into table+CCO schema records."
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N parsed records.")
    parser.add_argument("--force", action="store_true", help="Rebuild outputs even if source hash unchanged.")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only; do not write output files.")
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Only read stage records from this RUN_* id (default: latest stage run).",
    )
    parser.add_argument(
        "--all-staging",
        action="store_true",
        help="Read all staging runs instead of a single source run scope.",
    )
    args = parser.parse_args()

    run_parse(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        source_run_id=args.source_run_id,
        all_staging=args.all_staging,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

