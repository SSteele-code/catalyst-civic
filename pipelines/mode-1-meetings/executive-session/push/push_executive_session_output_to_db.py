#!/usr/bin/env python
"""
Executive Session PUSH (DB Loader)

Loads normalized executive-session parse/preparse records from
`_Sources/M1-Meetings/Executive_Session/_output` into:
  - m1_executive_session.documents
  - m1_executive_session.sections
  - m1_executive_session.figures
  - cco.registry
  - cco.identities
  - cco.observations

Strict invariant:
  - DB load only (core executive-session + glossary authority tables)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _maybe_reexec_py312() -> None:
    if os.name != "nt":
        return
    launcher = shutil.which("py")
    if not launcher:
        return
    script = str(Path(__file__).resolve())
    argv = [launcher, "-3.12", script, *sys.argv[1:]]
    os.execv(launcher, argv)


try:
    import psycopg2
except ModuleNotFoundError as exc:
    try:
        _maybe_reexec_py312()
    except Exception:
        pass
    raise SystemExit(
        "Missing dependency: psycopg2. Install with `py -m pip install psycopg2-binary` "
        "or run this script with Python 3.12 that has psycopg2."
    ) from exc


EXECUTIVE_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Executive_Session"
OUTPUT_ROOT = EXECUTIVE_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = EXECUTIVE_ROOT / "executive_session_push_state.json"
MANIFEST_FILE = EXECUTIVE_ROOT / "M1_EXECUTIVE_SESSION_PUSH_MANIFEST.jsonl"
PARSE_STATE_FILE = EXECUTIVE_ROOT / "executive_session_preparse_state.json"

SOURCE_SCHEMA_VERSION = "m1.executive_session.preparse.v1"
SOURCE_SCHEMA_VERSION_COMPAT = "m1.executive_session.parse.v1"
SOURCE_SCHEMA_VERSIONS = {SOURCE_SCHEMA_VERSION, SOURCE_SCHEMA_VERSION_COMPAT}
SOURCE_GLOSSARY_SCHEMA_VERSION = "m1.executive_session.glossary.v1"
PUSH_SCHEMA_VERSION = "m1.executive_session.push.v1"
TARGET_SCHEMA = "m1_executive_session"
JURISDICTION_DEFAULT = "Richlands"

RECORD_DIR_RE = re.compile(r"^M1\.AG\.ES\.\d{6}\.\d{8}\.\d{8}$", re.IGNORECASE)

CURRENCY_RE = re.compile(
    r"(?<![\w])(?:[-+]?\$[\s]*[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)",
    re.IGNORECASE,
)
PERCENT_RE = re.compile(
    r"(?<![\w$])(?:[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(
    r"(?<![\w$%])(?:[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w%])",
    re.IGNORECASE,
)

# Database configuration
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict[str, Any]) -> None:
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def iter_parse_jsons() -> list[Path]:
    if not OUTPUT_ROOT.exists():
        return []
    out: list[Path] = []
    for d in sorted(OUTPUT_ROOT.iterdir()):
        if not d.is_dir() or not RECORD_DIR_RE.match(d.name):
            continue
        parse_json = d / f"{d.name}.parse.json"
        preparse_json = d / f"{d.name}.preparse.json"
        if parse_json.exists():
            out.append(parse_json)
        elif preparse_json.exists():
            out.append(preparse_json)
    return out


def latest_parse_run_id_from_state() -> str | None:
    if not PARSE_STATE_FILE.exists():
        return None
    try:
        payload = json.loads(PARSE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    records = payload.get("records")
    if not isinstance(records, dict):
        return None
    best: str | None = None
    for value in records.values():
        if not isinstance(value, dict):
            continue
        run_id = str(value.get("last_run_id") or "").strip()
        if not run_id.upper().startswith("RUN_"):
            continue
        if best is None or run_id > best:
            best = run_id
    return best


def to_iso_date(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except Exception:
        return None


def to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s != "" else None


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_name(name: str) -> str:
    return normalize_ws(str(name or "")).strip(" ,;:.|-")


def generate_registry_id(category: str, name: str) -> str:
    normalized = normalize_ws(name)
    safe = re.sub(r"[^A-Za-z0-9]", "_", normalized.upper()).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return f"{category.upper()}_{safe}"


def map_glossary_category(source_category: str) -> str:
    src = str(source_category or "").upper().strip()
    if src == "PERSON":
        return "PEOPLE"
    if src in {
        "PEOPLE",
        "ORGANIZATION",
        "BOARD",
        "AGENCY",
        "LOCATION",
        "LAW",
        "LEGAL_REFERENCE",
        "TOPIC",
        "PROCEDURE",
        "PIPELINE_SIGNAL",
        "POSITION",
    }:
        return src
    return src or "UNKNOWN"


def ensure_target_tables(cur: Any) -> None:
    cur.execute(
        "SELECT to_regclass('m1_executive_session.documents'), "
        "to_regclass('m1_executive_session.sections'), "
        "to_regclass('m1_executive_session.figures'), "
        "to_regclass('cco.registry'), "
        "to_regclass('cco.identities'), "
        "to_regclass('cco.observations')"
    )
    docs_reg, sections_reg, figures_reg, registry_reg, identities_reg, observations_reg = cur.fetchone()
    if not docs_reg or not sections_reg or not figures_reg:
        raise RuntimeError(
            "Target tables missing. Apply "
            "_Infra/DATABASE/init/019_executive_session_schema.sql and "
            "_Infra/DATABASE/init/020_executive_session_sections_figures.sql before PUSH."
        )
    if not registry_reg or not identities_reg or not observations_reg:
        raise RuntimeError(
            "Target CCO tables missing. Apply "
            "_Infra/DATABASE/init/012_industrial_glossary.sql before PUSH."
        )


def derive_document_row(
    payload: dict[str, Any],
    record_code: str,
    source_parse_json: Path,
    source_parse_sha256: str,
) -> dict[str, Any]:
    pusher = payload.get("pusher_ready") if isinstance(payload.get("pusher_ready"), dict) else {}
    context = payload.get("meeting_context") if isinstance(payload.get("meeting_context"), dict) else {}
    summary = payload.get("executive_session_summary")
    summary = summary if isinstance(summary, dict) else {}
    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    source_lane = str(payload.get("source_lane") or "").strip()

    record_id = str(
        pusher.get("report_packet_id")
        or payload.get("executive_session_code")
        or record_code
    ).strip()
    source_id = str(
        pusher.get("source_id")
        or payload.get("linked_source_pdf_code")
        or payload.get("artifact_machine_code")
        or record_id
    ).strip()

    metadata = {
        "source_schema_version": str(payload.get("schema_version") or ""),
        "source_record_type": str(payload.get("record_type") or ""),
        "source_parse_run_id": str(payload.get("parse_run_id") or payload.get("preparse_run_id") or ""),
        "lineage": lineage,
        "executive_session_summary": summary,
        "push_source_parse_json": str(source_parse_json),
        "push_source_parse_sha256": source_parse_sha256,
        "push_loaded_at": datetime.now().isoformat(timespec="seconds"),
    }

    source_integrity_pass = None
    if summary.get("source_integrity_pass") is not None:
        source_integrity_pass = bool(summary.get("source_integrity_pass"))

    return {
        "record_id": record_id,
        "source_id": source_id,
        "source_lane": source_lane,
        "jurisdiction": str(payload.get("jurisdiction") or JURISDICTION_DEFAULT),
        "meeting_date": to_iso_date(context.get("anchor_meeting_date")),
        "content_mode": clean_text(pusher.get("content_mode")),
        "is_complete_document": bool(pusher.get("is_complete_document")),
        "linked_source_pdf_code": clean_text(payload.get("linked_source_pdf_code")),
        "section_count": to_int(summary.get("section_count")) or 0,
        "total_reason_lines": to_int(summary.get("total_reason_lines")) or 0,
        "source_integrity_score": to_float(summary.get("source_integrity_score")),
        "source_integrity_pass": source_integrity_pass,
        "metadata": metadata,
    }


def derive_section_rows(payload: dict[str, Any], record_id: str) -> list[dict[str, Any]]:
    sections = payload.get("executive_session_sections")
    if not isinstance(sections, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, sec in enumerate(sections, start=1):
        if not isinstance(sec, dict):
            continue
        section_id = str(sec.get("section_id") or f"ES{idx:03d}").strip()
        content = str(sec.get("text") or "")
        content_sha256 = str(sec.get("text_sha256") or sha256_text(content))
        reason_categories = sec.get("reason_categories")
        if not isinstance(reason_categories, list):
            reason_categories = []
        code_references = sec.get("code_references")
        if not isinstance(code_references, list):
            code_references = []
        reason_lines = sec.get("reason_lines")
        if not isinstance(reason_lines, list):
            reason_lines = []

        metadata = {
            "source_section_payload": {
                "session_key": sec.get("session_key"),
                "heading_text": sec.get("heading_text"),
                "heading_line_number": sec.get("heading_line_number"),
            }
        }

        out.append(
            {
                "section_row_id": f"{record_id}.{section_id}",
                "record_id": record_id,
                "section_id": section_id,
                "ordinal": idx,
                "candidate_status": clean_text(sec.get("candidate_status")),
                "session_key": clean_text(sec.get("session_key")),
                "heading_line_number": to_int(sec.get("heading_line_number")),
                "heading_text": clean_text(sec.get("heading_text")),
                "start_line": to_int(sec.get("start_line")),
                "end_line": to_int(sec.get("end_line")),
                "reason_line_count": to_int(sec.get("reason_line_count")) or 0,
                "reason_categories": reason_categories,
                "code_references": code_references,
                "reason_lines": reason_lines,
                "heading_line_match": bool(sec.get("heading_line_match")) if sec.get("heading_line_match") is not None else None,
                "reason_line_matches": to_int(sec.get("reason_line_matches")),
                "reason_line_total": to_int(sec.get("reason_line_total")),
                "content": content,
                "content_sha256": content_sha256,
                "metadata": metadata,
            }
        )
    return out


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    s, e = span
    for os, oe in occupied:
        if s < oe and os < e:
            return True
    return False


def _to_decimal(raw: str) -> Decimal | None:
    cleaned = raw.replace("$", "").replace("%", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def extract_figures_from_text(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    def push_match(match: re.Match[str], figure_type: str, unit: str) -> None:
        span = match.span()
        if _overlaps(span, occupied):
            return
        raw = match.group(0).strip()
        dec = _to_decimal(raw)
        start = max(0, span[0] - 48)
        end = min(len(text), span[1] + 48)
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        out.append(
            {
                "figure_type": figure_type,
                "raw_value": raw,
                "numeric_value": dec,
                "unit": unit,
                "start_char": span[0],
                "end_char": span[1],
                "context_snippet": snippet,
            }
        )
        occupied.append(span)

    for m in CURRENCY_RE.finditer(text):
        push_match(m, "CURRENCY", "$")
    for m in PERCENT_RE.finditer(text):
        push_match(m, "PERCENT", "%")
    for m in NUMBER_RE.finditer(text):
        push_match(m, "NUMBER", "number")

    out.sort(key=lambda r: (int(r["start_char"]), int(r["end_char"])))
    return out


def derive_figure_rows(section_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sec in section_rows:
        text = str(sec.get("content") or "")
        figures = extract_figures_from_text(text)
        for idx, fg in enumerate(figures, start=1):
            figure_row_id = f"{sec['section_row_id']}.FG{idx:03d}"
            rows.append(
                {
                    "figure_row_id": figure_row_id,
                    "record_id": sec["record_id"],
                    "section_row_id": sec["section_row_id"],
                    "section_id": sec["section_id"],
                    "ordinal": idx,
                    "figure_type": fg["figure_type"],
                    "raw_value": fg["raw_value"],
                    "numeric_value": fg["numeric_value"],
                    "unit": fg["unit"],
                    "start_char": fg["start_char"],
                    "end_char": fg["end_char"],
                    "context_snippet": fg["context_snippet"],
                    "metadata": {},
                }
            )
    return rows


def upsert_document(cur: Any, row: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO m1_executive_session.documents (
            record_id, source_id, source_lane, jurisdiction, meeting_date,
            content_mode, is_complete_document, linked_source_pdf_code, section_count,
            total_reason_lines, source_integrity_score, source_integrity_pass, metadata
        )
        VALUES (
            %(record_id)s, %(source_id)s, %(source_lane)s, %(jurisdiction)s, %(meeting_date)s,
            %(content_mode)s, %(is_complete_document)s, %(linked_source_pdf_code)s, %(section_count)s,
            %(total_reason_lines)s, %(source_integrity_score)s, %(source_integrity_pass)s, %(metadata)s::jsonb
        )
        ON CONFLICT (record_id) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            source_lane = EXCLUDED.source_lane,
            jurisdiction = EXCLUDED.jurisdiction,
            meeting_date = EXCLUDED.meeting_date,
            content_mode = EXCLUDED.content_mode,
            is_complete_document = EXCLUDED.is_complete_document,
            linked_source_pdf_code = EXCLUDED.linked_source_pdf_code,
            section_count = EXCLUDED.section_count,
            total_reason_lines = EXCLUDED.total_reason_lines,
            source_integrity_score = EXCLUDED.source_integrity_score,
            source_integrity_pass = EXCLUDED.source_integrity_pass,
            metadata = EXCLUDED.metadata,
            updated_at = CURRENT_TIMESTAMP;
        """,
        {
            **row,
            "metadata": json.dumps(row["metadata"], ensure_ascii=True),
        },
    )


def replace_sections(cur: Any, record_id: str, section_rows: list[dict[str, Any]]) -> None:
    cur.execute("DELETE FROM m1_executive_session.sections WHERE record_id = %s", (record_id,))
    if not section_rows:
        return
    for row in section_rows:
        cur.execute(
            """
            INSERT INTO m1_executive_session.sections (
                section_row_id, record_id, section_id, ordinal, candidate_status, session_key,
                heading_line_number, heading_text, start_line, end_line, reason_line_count,
                reason_categories, code_references, reason_lines, heading_line_match,
                reason_line_matches, reason_line_total, content, content_sha256, metadata
            )
            VALUES (
                %(section_row_id)s, %(record_id)s, %(section_id)s, %(ordinal)s, %(candidate_status)s, %(session_key)s,
                %(heading_line_number)s, %(heading_text)s, %(start_line)s, %(end_line)s, %(reason_line_count)s,
                %(reason_categories)s::jsonb, %(code_references)s::jsonb, %(reason_lines)s::jsonb, %(heading_line_match)s,
                %(reason_line_matches)s, %(reason_line_total)s, %(content)s, %(content_sha256)s, %(metadata)s::jsonb
            )
            ON CONFLICT (section_row_id) DO UPDATE SET
                record_id = EXCLUDED.record_id,
                section_id = EXCLUDED.section_id,
                ordinal = EXCLUDED.ordinal,
                candidate_status = EXCLUDED.candidate_status,
                session_key = EXCLUDED.session_key,
                heading_line_number = EXCLUDED.heading_line_number,
                heading_text = EXCLUDED.heading_text,
                start_line = EXCLUDED.start_line,
                end_line = EXCLUDED.end_line,
                reason_line_count = EXCLUDED.reason_line_count,
                reason_categories = EXCLUDED.reason_categories,
                code_references = EXCLUDED.code_references,
                reason_lines = EXCLUDED.reason_lines,
                heading_line_match = EXCLUDED.heading_line_match,
                reason_line_matches = EXCLUDED.reason_line_matches,
                reason_line_total = EXCLUDED.reason_line_total,
                content = EXCLUDED.content,
                content_sha256 = EXCLUDED.content_sha256,
                metadata = EXCLUDED.metadata;
            """,
            {
                **row,
                "reason_categories": json.dumps(row["reason_categories"], ensure_ascii=True),
                "code_references": json.dumps(row["code_references"], ensure_ascii=True),
                "reason_lines": json.dumps(row["reason_lines"], ensure_ascii=True),
                "metadata": json.dumps(row["metadata"], ensure_ascii=True),
            },
        )


def replace_figures(cur: Any, record_id: str, figure_rows: list[dict[str, Any]]) -> None:
    cur.execute("DELETE FROM m1_executive_session.figures WHERE record_id = %s", (record_id,))
    if not figure_rows:
        return
    for row in figure_rows:
        cur.execute(
            """
            INSERT INTO m1_executive_session.figures (
                figure_row_id, record_id, section_row_id, section_id, ordinal,
                figure_type, raw_value, numeric_value, unit, start_char, end_char,
                context_snippet, metadata
            )
            VALUES (
                %(figure_row_id)s, %(record_id)s, %(section_row_id)s, %(section_id)s, %(ordinal)s,
                %(figure_type)s, %(raw_value)s, %(numeric_value)s, %(unit)s, %(start_char)s, %(end_char)s,
                %(context_snippet)s, %(metadata)s::jsonb
            )
            ON CONFLICT (figure_row_id) DO UPDATE SET
                record_id = EXCLUDED.record_id,
                section_row_id = EXCLUDED.section_row_id,
                section_id = EXCLUDED.section_id,
                ordinal = EXCLUDED.ordinal,
                figure_type = EXCLUDED.figure_type,
                raw_value = EXCLUDED.raw_value,
                numeric_value = EXCLUDED.numeric_value,
                unit = EXCLUDED.unit,
                start_char = EXCLUDED.start_char,
                end_char = EXCLUDED.end_char,
                context_snippet = EXCLUDED.context_snippet,
                metadata = EXCLUDED.metadata;
            """,
            {
                **row,
                "metadata": json.dumps(row["metadata"], ensure_ascii=True),
            },
        )


def compact_glossary_observation_fact(
    *,
    entity: dict[str, Any],
    payload: dict[str, Any],
    record_id: str,
) -> dict[str, Any]:
    summary = payload.get("executive_session_summary")
    summary = summary if isinstance(summary, dict) else {}
    return {
        "schema_version": PUSH_SCHEMA_VERSION,
        "source_schema_version": str(payload.get("schema_version") or ""),
        "source_record_type": str(payload.get("record_type") or ""),
        "source_glossary_schema_version": str(
            (payload.get("glossary") or {}).get("schema_version") if isinstance(payload.get("glossary"), dict) else ""
        ),
        "source_lane": str(payload.get("source_lane") or ""),
        "source_executive_session_code": record_id,
        "entity_id": str(entity.get("entry_id") or ""),
        "source_category": str(entity.get("category") or ""),
        "source_fact_key": str(entity.get("fact_key") or ""),
        "confidence": float(entity.get("confidence") or 0.0),
        "match_type": str(entity.get("matched_from") or ""),
        "source_span": entity.get("source_span") if isinstance(entity.get("source_span"), dict) else {},
        "source_integrity_score": summary.get("source_integrity_score"),
        "source_integrity_pass": summary.get("source_integrity_pass"),
    }


def effective_date_from_payload(payload: dict[str, Any]) -> str | None:
    context = payload.get("meeting_context")
    context = context if isinstance(context, dict) else {}
    date_value = str(context.get("anchor_meeting_date") or "").strip()
    if not date_value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        return date_value
    return None


def replace_glossary_entities(
    cur: Any,
    *,
    record_id: str,
    payload: dict[str, Any],
) -> tuple[int, int]:
    glossary = payload.get("glossary")
    glossary = glossary if isinstance(glossary, dict) else {}
    entities = glossary.get("entities")
    entities = entities if isinstance(entities, list) else []

    # Deterministic replacement per source record.
    cur.execute("DELETE FROM cco.observations WHERE source_id = %s", (record_id,))
    cur.execute("DELETE FROM cco.identities WHERE source_id = %s", (record_id,))

    effective_date = effective_date_from_payload(payload)
    total = 0
    pushed = 0
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        total += 1

        category = map_glossary_category(str(entity.get("category") or ""))
        canonical_name = clean_name(str(entity.get("canonical_name") or ""))
        if not canonical_name:
            continue
        registry_id = generate_registry_id(category, canonical_name)

        cur.execute(
            """
            INSERT INTO cco.registry (registry_id, category, canonical_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (registry_id) DO UPDATE
            SET category = EXCLUDED.category,
                canonical_name = EXCLUDED.canonical_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (registry_id, category, canonical_name),
        )

        cur.execute(
            """
            INSERT INTO cco.identities (registry_id, alias_name, source_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (registry_id, alias_name) DO NOTHING
            """,
            (registry_id, canonical_name, record_id),
        )

        fact_key = clean_name(str(entity.get("fact_key") or "MENTIONED_IN_RECORD")).upper()
        evidence = normalize_ws(str(entity.get("evidence") or ""))[:500]
        if not evidence:
            evidence = canonical_name
        fact_value = compact_glossary_observation_fact(entity=entity, payload=payload, record_id=record_id)

        cur.execute(
            """
            INSERT INTO cco.observations (registry_id, fact_key, fact_value, source_id, evidence, effective_date)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s)
            """,
            (
                registry_id,
                fact_key,
                json.dumps(fact_value, ensure_ascii=True),
                record_id,
                evidence,
                effective_date,
            ),
        )
        pushed += 1

    return total, pushed


def run_push(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    source_run_id: str | None = None,
    all_output: bool = False,
) -> dict[str, Any]:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")

    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = iter_parse_jsons()
    discovered = len(candidates)

    pushed = 0
    skipped_unchanged = 0
    skipped_source_scope = 0
    failed = 0
    sections_pushed = 0
    figures_pushed = 0
    glossary_entities_pushed = 0
    run_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = RUNS_ROOT / run_id

    effective_source_run_id = source_run_id or latest_parse_run_id_from_state()

    conn = None
    cur = None
    if not dry_run:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            database=PG_DB,
            user=PG_USER,
            password=PG_PASS,
        )
        conn.autocommit = False
        cur = conn.cursor()
        ensure_target_tables(cur)

    try:
        for parse_json in candidates:
            if limit is not None and pushed >= limit:
                break

            record_code = parse_json.parent.name
            try:
                source_text = parse_json.read_text(encoding="utf-8")
                source_parse_sha256 = sha256_text(source_text)
                payload = json.loads(source_text)
            except Exception as exc:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "executive_session_code": record_code,
                        "source_parse_json": str(parse_json),
                        "error": f"invalid_parse_json: {exc}",
                    }
                )
                continue

            payload_schema_version = str(payload.get("schema_version") or "")
            if payload_schema_version not in SOURCE_SCHEMA_VERSIONS:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "executive_session_code": record_code,
                        "source_parse_json": str(parse_json),
                        "error": f"unsupported_schema_version: {payload_schema_version}",
                    }
                )
                continue

            payload_run_id = str(payload.get("parse_run_id") or payload.get("preparse_run_id") or "").strip()
            if not all_output and effective_source_run_id:
                if payload_run_id != effective_source_run_id:
                    skipped_source_scope += 1
                    continue

            document_row = derive_document_row(payload, record_code, parse_json, source_parse_sha256)
            record_id = document_row["record_id"]
            section_rows = derive_section_rows(payload, record_id)
            figure_rows = derive_figure_rows(section_rows)
            glossary_entities = (
                payload.get("glossary", {}).get("entities")
                if isinstance(payload.get("glossary"), dict)
                else []
            )
            glossary_entities_total = len(glossary_entities) if isinstance(glossary_entities, list) else 0

            prev = state_records.get(record_id, {})
            if (
                not force
                and prev.get("source_parse_sha256") == source_parse_sha256
                and str(prev.get("last_status") or "") == "pushed"
            ):
                skipped_unchanged += 1
                continue

            if dry_run:
                pushed += 1
                sections_pushed += len(section_rows)
                figures_pushed += len(figure_rows)
                glossary_entities_pushed += glossary_entities_total
                continue

            try:
                assert cur is not None
                assert conn is not None
                upsert_document(cur, document_row)
                replace_sections(cur, record_id, section_rows)
                replace_figures(cur, record_id, figure_rows)
                _, glossary_inserted_count = replace_glossary_entities(
                    cur,
                    record_id=record_id,
                    payload=payload,
                )
                conn.commit()

                pushed += 1
                sections_pushed += len(section_rows)
                figures_pushed += len(figure_rows)
                glossary_entities_pushed += glossary_inserted_count

                row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "record_id": record_id,
                    "source_id": document_row["source_id"],
                    "source_lane": document_row["source_lane"],
                    "meeting_date": document_row["meeting_date"],
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_parse_sha256,
                    "sections_count": len(section_rows),
                    "figures_count": len(figure_rows),
                    "glossary_entities_total": glossary_entities_total,
                    "glossary_entities_pushed": glossary_inserted_count,
                    "db_schema": f"{TARGET_SCHEMA}+cco",
                }
                run_rows.append(row)
                append_manifest_rows([row])

                state_records[record_id] = {
                    "last_run_id": run_id,
                    "last_status": "pushed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_parse_sha256,
                    "record_id": record_id,
                    "source_lane": document_row["source_lane"],
                    "sections_count": len(section_rows),
                    "figures_count": len(figure_rows),
                    "glossary_entities_total": glossary_entities_total,
                    "glossary_entities_pushed": glossary_inserted_count,
                }
                save_state(state)
            except Exception as exc:
                assert conn is not None
                conn.rollback()
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "record_id": record_id,
                        "executive_session_code": record_code,
                        "source_parse_json": str(parse_json),
                        "error": str(exc),
                    }
                )
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

    if not dry_run:
        run_manifest = run_dir / "executive_session_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "executive_session_push_failures.jsonl"
            with failure_out.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": PUSH_SCHEMA_VERSION,
            "source_schema_version": SOURCE_SCHEMA_VERSION,
            "target_schema": TARGET_SCHEMA,
            "source_run_scope": effective_source_run_id,
            "discovered_records": discovered,
            "pushed_records": pushed,
            "pushed_sections": sections_pushed,
            "pushed_figures": figures_pushed,
            "pushed_glossary_entities": glossary_entities_pushed,
            "skipped_unchanged": skipped_unchanged,
            "skipped_source_scope": skipped_source_scope,
            "failed": failed,
            "limit": limit,
            "force": force,
            "pg_host": PG_HOST,
            "pg_port": PG_PORT,
            "pg_db": PG_DB,
        }
        (run_dir / "run_summary.json").write_text(json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n")

    summary = {
        "run_id": run_id,
        "target_schema": TARGET_SCHEMA,
        "discovered_records": discovered,
        "pushed_records": pushed,
        "pushed_sections": sections_pushed,
        "pushed_figures": figures_pushed,
        "pushed_glossary_entities": glossary_entities_pushed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_source_scope": skipped_source_scope,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 68)
    print("EXECUTIVE SESSION PUSH SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Target schema: {summary['target_schema']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Pushed records: {summary['pushed_records']}")
    print(f"  Pushed sections: {summary['pushed_sections']}")
    print(f"  Pushed figures: {summary['pushed_figures']}")
    print(f"  Pushed glossary entities: {summary['pushed_glossary_entities']}")
    print(f"  Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"  Skipped (source scope): {summary['skipped_source_scope']}")
    print(f"  Failed: {summary['failed']}")
    if dry_run:
        print("  Dry run: yes (no DB writes)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 68)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push executive-session parse output records into m1_executive_session and cco tables."
    )
    parser.add_argument("--limit", type=int, default=None, help="Push first N executive-session records.")
    parser.add_argument("--force", action="store_true", help="Re-push even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write to DB.")
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Only push records from this parse/preparse run id (default: latest run id from parse state).",
    )
    parser.add_argument(
        "--all-output",
        action="store_true",
        help="Push across all output records (ignore run scoping).",
    )
    args = parser.parse_args()

    run_push(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        source_run_id=args.source_run_id,
        all_output=args.all_output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
