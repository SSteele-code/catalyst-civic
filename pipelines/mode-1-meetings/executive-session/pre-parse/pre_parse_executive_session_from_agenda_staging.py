#!/usr/bin/env python
"""
Executive Session PRE_PARSE (Agenda-Staging Lane)

Transforms staged agenda-mined executive-session excerpts into normalized schema
and writes pusher-ready artifacts into:
  _Sources/M1-Meetings/Executive_Session/_output/<executive_session_code>/

Strict invariant:
  - PRE_PARSE only (schema normalization + lineage packaging + glossary section)
  - No DB writes

Linkage contract:
  - source_pdf_code:         M1.AG.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
  - executive_session_code:  M1.AG.ES.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


EXECUTIVE_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session")
STAGING_ROOT = EXECUTIVE_ROOT / "_staging"
OUTPUT_ROOT = EXECUTIVE_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = EXECUTIVE_ROOT / "executive_session_preparse_state.json"
MANIFEST_FILE = EXECUTIVE_ROOT / "M1_EXECUTIVE_SESSION_PREPARSE_MANIFEST.jsonl"

SOURCE_SCHEMA_VERSION = "m1.executive_session.pull.v1"
SCHEMA_VERSION = "m1.executive_session.preparse.v1"
GLOSSARY_SCHEMA_VERSION = "m1.executive_session.glossary.v1"
SOURCE_LANE = "agenda_output_executive_session_sections"
JURISDICTION = "Richlands"

AG_CODE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
VA_CODE_SECTION_RE = re.compile(
    r"\b(?:VA\s*Code(?:\s*Section)?|Code\s*Section|Section)\s*"
    r"([0-9]{1,2}\s*\.\s*[0-9]{1,2}\s*[-\s]*[0-9]{3,4}(?:\s*\([A-Za-z0-9]+\))*)\b",
    re.IGNORECASE,
)
BARE_VA_CODE_RE = re.compile(
    r"\b([0-9]{1,2}\s*\.\s*[0-9]{1,2}\s*[-\s]*[0-9]{3,4}(?:\s*\([A-Za-z0-9]+\))*)\b",
    re.IGNORECASE,
)
HIRING_TRIGGER_RE = re.compile(r"\b(hiring|hire|appoint(?:ment)?|position)\b", re.IGNORECASE)
SESSION_KIND_RE = re.compile(r"\b(executive|closed)\s+session\b", re.IGNORECASE)

REASON_CATEGORY_MAP: dict[str, tuple[str, str, str, float]] = {
    "personnel": ("TOPIC", "Personnel", "EXEC_SESSION_TOPIC", 0.97),
    "contract_negotiation": ("TOPIC", "Contract Negotiation", "EXEC_SESSION_TOPIC", 0.95),
    "property_real_estate": ("TOPIC", "Real Property", "EXEC_SESSION_TOPIC", 0.93),
    "property_acquisition": ("TOPIC", "Property Acquisition", "EXEC_SESSION_TOPIC", 0.94),
    "legal_consultation": ("TOPIC", "Legal Consultation", "EXEC_SESSION_TOPIC", 0.94),
    "litigation": ("TOPIC", "Pending Litigation", "EXEC_SESSION_TOPIC", 0.94),
    "prospective_business": ("TOPIC", "Prospective Business", "EXEC_SESSION_TOPIC", 0.93),
    "prospective_industry": ("TOPIC", "Prospective Industry", "EXEC_SESSION_TOPIC", 0.93),
}

POSITION_MARKERS: list[str] = [
    "town manager",
    "interim town manager",
    "town attorney",
    "town clerk",
    "police chief",
    "public works foreman",
    "revenue part time position",
    "part time rec position",
]


@dataclass
class StageCandidate:
    stage_json_path: Path
    stage_json_sha256: str
    source_stage_run_id: str
    source_stage_captured_at: str
    source_stage_machine_code: str
    source_lane: str
    source_txt_path: Path
    source_txt_sha256: str
    source_bundle_txt: Path | None
    factsheet_path: Path
    source_pdf_code: str
    source_pdf_original_name: str
    source_pdf_internal_name: str
    source_pdf_hash: str
    page_count: int | None
    executive_session_code: str
    source_integrity: dict[str, Any]
    session_count: int
    status_counts: dict[str, int]
    sessions: list[dict[str, Any]]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}
    if not isinstance(payload, dict):
        return {"records": {}}
    payload.setdefault("records", {})
    return payload


def save_state(state: dict[str, Any]) -> None:
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)
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
        code = str(row.get("executive_session_code") or "").strip()
        if code:
            codes.add(code)
    return codes


def to_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_name(text: str) -> str:
    return normalize_ws((text or "").strip(" ,;:.|"))


def normalize_statute_reference(raw: str) -> str:
    text = normalize_ws(raw).upper()
    text = text.replace("\\", "")
    text = re.sub(r"\bVA\s*CODE(?:\s*SECTION)?\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCODE\s*SECTION\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSECTION\b", "", text, flags=re.IGNORECASE)
    text = text.strip(" :;-")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"([0-9]{1,2}\.[0-9]{1,2})[- ]?([0-9]{3,4})", r"\1-\2", text)
    return text


def title_case_words(text: str) -> str:
    return " ".join(part.capitalize() for part in clean_name(text).split())


def build_glossary_section(
    executive_session_code: str,
    source_pdf_code: str,
    sections: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    dedupe: set[tuple[str, str, str]] = set()
    by_category: dict[str, int] = {}

    def fact_key_priority(category: str, fact_key: str) -> int:
        cat = category.upper()
        key = fact_key.upper()
        if cat == "POSITION":
            if key == "POSITION_HIRING_DISCUSSION":
                return 3
            if key == "POSITION_DISCUSSED":
                return 2
        return 1

    def add_entry(
        *,
        category: str,
        canonical_name: str,
        fact_key: str,
        confidence: float,
        matched_from: str,
        evidence: str,
        section_id: str,
        start_line: int,
        end_line: int,
    ) -> None:
        name = clean_name(canonical_name)
        if not name:
            return
        category_upper = category.upper()
        fact_key_upper = fact_key.upper()

        # For POSITION entries, keep only one entry per canonical name and
        # preserve the strongest fact_key variant.
        if category_upper == "POSITION":
            for existing in entries:
                if (
                    str(existing.get("category") or "").upper() == "POSITION"
                    and str(existing.get("canonical_name") or "").upper() == name.upper()
                ):
                    existing_fact_key = str(existing.get("fact_key") or "").upper()
                    existing_priority = fact_key_priority(category_upper, existing_fact_key)
                    incoming_priority = fact_key_priority(category_upper, fact_key_upper)
                    existing_conf = float(existing.get("confidence") or 0.0)
                    incoming_conf = round(float(confidence), 3)
                    if incoming_priority > existing_priority or (
                        incoming_priority == existing_priority and incoming_conf > existing_conf
                    ):
                        old_key = (category_upper, name.upper(), existing_fact_key)
                        dedupe.discard(old_key)
                        existing["fact_key"] = fact_key_upper
                        existing["confidence"] = incoming_conf
                        existing["matched_from"] = matched_from
                        existing["evidence"] = normalize_ws(evidence)[:280]
                        existing["source_span"] = {
                            "section_id": section_id,
                            "start_line": start_line,
                            "end_line": end_line,
                        }
                        dedupe.add((category_upper, name.upper(), fact_key_upper))
                    return

        key = (category_upper, name.upper(), fact_key_upper)
        if key in dedupe:
            return
        dedupe.add(key)
        by_category[category_upper] = by_category.get(category_upper, 0) + 1
        entries.append(
            {
                "entry_id": f"GL{len(entries) + 1:03d}",
                "category": category_upper,
                "canonical_name": name,
                "fact_key": fact_key_upper,
                "confidence": round(float(confidence), 3),
                "matched_from": matched_from,
                "evidence": normalize_ws(evidence)[:280],
                "source_span": {
                    "section_id": section_id,
                    "start_line": start_line,
                    "end_line": end_line,
                },
            }
        )

    def add_statute_reference(code_value: str, section: dict[str, Any], matched_from: str, evidence: str) -> None:
        normalized_code = normalize_statute_reference(code_value)
        if len(normalized_code) < 5:
            return
        canonical_name = f"Virginia Code {normalized_code}"
        if "(" not in normalized_code:
            richer_prefix = canonical_name + "("
            for existing in entries:
                if (
                    str(existing.get("category") or "").upper() == "LEGAL_REFERENCE"
                    and str(existing.get("fact_key") or "").upper() == "STATUTE_REFERENCE"
                    and str(existing.get("canonical_name") or "").startswith(richer_prefix)
                ):
                    return
        else:
            base_code = normalized_code.split("(", 1)[0]
            base_name = f"Virginia Code {base_code}"
            for idx in range(len(entries) - 1, -1, -1):
                existing = entries[idx]
                if (
                    str(existing.get("category") or "").upper() == "LEGAL_REFERENCE"
                    and str(existing.get("fact_key") or "").upper() == "STATUTE_REFERENCE"
                    and str(existing.get("canonical_name") or "") == base_name
                ):
                    del entries[idx]
                    dedupe.discard(("LEGAL_REFERENCE", base_name.upper(), "STATUTE_REFERENCE"))
                    if by_category.get("LEGAL_REFERENCE", 0) > 0:
                        by_category["LEGAL_REFERENCE"] -= 1
                    break
        add_entry(
            category="LEGAL_REFERENCE",
            canonical_name=canonical_name,
            fact_key="STATUTE_REFERENCE",
            confidence=0.96,
            matched_from=matched_from,
            evidence=evidence,
            section_id=str(section.get("section_id") or ""),
            start_line=to_int(section.get("start_line"), default=0),
            end_line=to_int(section.get("end_line"), default=0),
        )

    for section in sections:
        if not isinstance(section, dict):
            continue

        section_text = str(section.get("text") or "")
        heading_text = str(section.get("heading_text") or "")
        status_text = str(section.get("candidate_status") or "")
        combined_text = "\n".join(x for x in [heading_text, section_text] if x).strip()

        if SESSION_KIND_RE.search(combined_text):
            add_entry(
                category="PROCEDURE",
                canonical_name="Executive Session",
                fact_key="SESSION_TYPE",
                confidence=0.95,
                matched_from="session_heading_pattern",
                evidence=combined_text,
                section_id=str(section.get("section_id") or ""),
                start_line=to_int(section.get("start_line"), default=0),
                end_line=to_int(section.get("end_line"), default=0),
            )

        reason_categories = section.get("reason_categories")
        if isinstance(reason_categories, list):
            for reason_cat in reason_categories:
                reason_key = clean_name(str(reason_cat)).lower()
                mapped = REASON_CATEGORY_MAP.get(reason_key)
                if not mapped:
                    continue
                category, canonical_name, fact_key, confidence = mapped
                add_entry(
                    category=category,
                    canonical_name=canonical_name,
                    fact_key=fact_key,
                    confidence=confidence,
                    matched_from="reason_category",
                    evidence=heading_text or section_text,
                    section_id=str(section.get("section_id") or ""),
                    start_line=to_int(section.get("start_line"), default=0),
                    end_line=to_int(section.get("end_line"), default=0),
                )

        code_references = section.get("code_references")
        if isinstance(code_references, list):
            for code_ref in code_references:
                code_value = clean_name(str(code_ref))
                if not code_value:
                    continue
                add_statute_reference(code_value, section, "reason_code_reference", heading_text or section_text)

        reason_lines = section.get("reason_lines")
        if isinstance(reason_lines, list):
            for reason_line in reason_lines:
                if not isinstance(reason_line, dict):
                    continue
                reason_line_text = str(reason_line.get("text") or "")
                reason_line_codes = reason_line.get("code_references")
                if isinstance(reason_line_codes, list):
                    for code_ref in reason_line_codes:
                        code_value = clean_name(str(code_ref))
                        if not code_value:
                            continue
                        add_statute_reference(code_value, section, "reason_line_code_reference", reason_line_text)
                for match in VA_CODE_SECTION_RE.finditer(reason_line_text):
                    add_statute_reference(match.group(1), section, "va_code_pattern", reason_line_text)
                for match in BARE_VA_CODE_RE.finditer(reason_line_text):
                    add_statute_reference(match.group(1), section, "bare_va_code_pattern", reason_line_text)

        for match in VA_CODE_SECTION_RE.finditer(combined_text):
            add_statute_reference(match.group(1), section, "va_code_pattern", combined_text)
        for match in BARE_VA_CODE_RE.finditer(combined_text):
            add_statute_reference(match.group(1), section, "bare_va_code_pattern", combined_text)

        lower_text = combined_text.lower()
        for marker in POSITION_MARKERS:
            marker_re = rf"\b{re.escape(marker)}\b"
            if not re.search(marker_re, lower_text, re.IGNORECASE):
                continue
            fact_key = "POSITION_HIRING_DISCUSSION" if HIRING_TRIGGER_RE.search(lower_text) else "POSITION_DISCUSSED"
            add_entry(
                category="POSITION",
                canonical_name=title_case_words(marker),
                fact_key=fact_key,
                confidence=0.92 if fact_key == "POSITION_HIRING_DISCUSSION" else 0.89,
                matched_from="position_marker",
                evidence=combined_text,
                section_id=str(section.get("section_id") or ""),
                start_line=to_int(section.get("start_line"), default=0),
                end_line=to_int(section.get("end_line"), default=0),
            )

        if status_text:
            add_entry(
                category="PIPELINE_SIGNAL",
                canonical_name=clean_name(status_text),
                fact_key="CANDIDATE_STATUS",
                confidence=0.8,
                matched_from="session_candidate_status",
                evidence=heading_text or section_text,
                section_id=str(section.get("section_id") or ""),
                start_line=to_int(section.get("start_line"), default=0),
                end_line=to_int(section.get("end_line"), default=0),
            )

    entries.sort(key=lambda e: (e["category"], e["canonical_name"], e["fact_key"]))
    for idx, entry in enumerate(entries, start=1):
        entry["entry_id"] = f"GL{idx:03d}"

    return {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "record_type": "executive_session_glossary_section",
        "source_executive_session_code": executive_session_code,
        "source_pdf_code": source_pdf_code,
        "glossary_scope_text_hint": "executive_session_sections[].text",
        "summary": {
            "entries_total": len(entries),
            "entries_by_category": by_category,
        },
        "entities": entries,
    }


def iter_stage_json_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.executive_session.json"))


def latest_stage_run_id(root: Path) -> str | None:
    if not root.exists():
        return None
    run_dirs = [p for p in root.iterdir() if p.is_dir() and p.name.upper().startswith("RUN_")]
    if not run_dirs:
        return None
    run_dirs.sort(key=lambda p: p.name, reverse=True)
    return run_dirs[0].name


def iter_stage_json_files_scoped(root: Path, source_run_id: str | None, all_staging: bool) -> tuple[list[Path], str]:
    if all_staging:
        files = list(iter_stage_json_files(root))
        return files, "ALL_STAGING"

    run_id = source_run_id or latest_stage_run_id(root)
    if not run_id:
        return [], "NONE"
    run_dir = root / run_id
    if not run_dir.exists() or not run_dir.is_dir():
        return [], run_id
    files = sorted(run_dir.rglob("*.executive_session.json"))
    return files, run_id


def build_executive_session_code(source_pdf_code: str) -> str | None:
    match = AG_CODE_RE.match(source_pdf_code.strip())
    if not match:
        return None
    docnum, created_ymd, pulled_ymd = match.group(1), match.group(2), match.group(3)
    return f"M1.AG.ES.{docnum}.{created_ymd}.{pulled_ymd}"


def ymd_to_iso(ymd: str) -> str | None:
    if not re.fullmatch(r"\d{8}", ymd):
        return None
    try:
        return datetime.strptime(ymd, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def read_factsheet(
    source_txt_path: Path,
    source_stage_machine_code: str,
    source_bundle_factsheet_raw: str,
) -> tuple[Path | None, dict[str, Any] | None]:
    if source_bundle_factsheet_raw:
        candidate = Path(source_bundle_factsheet_raw)
        if candidate.exists():
            try:
                return candidate, json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                return candidate, None

    output_dir = source_txt_path.parent
    candidate = output_dir / f"{source_stage_machine_code}.factsheet.json"
    if candidate.exists():
        try:
            return candidate, json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            return candidate, None

    for fs in sorted(output_dir.glob("*.factsheet.json")):
        try:
            return fs, json.loads(fs.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None, None


def parse_stage_candidate(stage_json_path: Path) -> tuple[StageCandidate | None, str | None]:
    try:
        stage_text = stage_json_path.read_text(encoding="utf-8")
        payload = json.loads(stage_text)
    except Exception as exc:
        return None, f"invalid_stage_json: {exc}"

    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version and schema_version != SOURCE_SCHEMA_VERSION:
        return None, f"unexpected_schema_version: {schema_version}"

    source_stage_machine_code = str(payload.get("machine_code") or "").strip()
    source_txt_raw = str(payload.get("source_txt") or "").strip()
    source_lane = str(payload.get("source_lane") or SOURCE_LANE).strip() or SOURCE_LANE

    if not source_stage_machine_code:
        return None, "missing_machine_code"
    if not source_txt_raw:
        return None, "missing_source_txt"

    source_txt_path = Path(source_txt_raw)
    if not source_txt_path.exists():
        return None, "missing_source_txt_file"

    source_bundle_factsheet_raw = str(payload.get("source_bundle_factsheet") or "").strip()
    factsheet_path, facts = read_factsheet(
        source_txt_path=source_txt_path,
        source_stage_machine_code=source_stage_machine_code,
        source_bundle_factsheet_raw=source_bundle_factsheet_raw,
    )
    if not facts:
        return None, "missing_or_invalid_factsheet"

    source_pdf_original_name = str(facts.get("source_pdf_original_name") or "").strip()
    if not source_pdf_original_name.lower().endswith(".pdf"):
        return None, "missing_source_pdf_original_name"
    source_pdf_code = source_pdf_original_name[:-4]

    executive_session_code = build_executive_session_code(source_pdf_code)
    if not executive_session_code:
        return None, f"unmappable_source_pdf_code: {source_pdf_code}"

    sessions_raw = payload.get("sessions")
    if not isinstance(sessions_raw, list):
        return None, "missing_sessions"
    sessions = [x for x in sessions_raw if isinstance(x, dict)]
    if not sessions:
        return None, "empty_sessions"

    source_bundle_txt_raw = str(payload.get("source_bundle_txt") or "").strip()
    source_bundle_txt = Path(source_bundle_txt_raw) if source_bundle_txt_raw else None

    status_counts_raw = payload.get("status_counts")
    status_counts: dict[str, int] = {}
    if isinstance(status_counts_raw, dict):
        for k, v in status_counts_raw.items():
            key = str(k).strip().lower()
            if not key:
                continue
            status_counts[key] = to_int(v, default=0)

    source_integrity = payload.get("integrity")
    if not isinstance(source_integrity, dict):
        source_integrity = {}

    candidate = StageCandidate(
        stage_json_path=stage_json_path,
        stage_json_sha256=sha256_text(stage_text),
        source_stage_run_id=str(payload.get("run_id") or "").strip(),
        source_stage_captured_at=str(payload.get("captured_at") or "").strip(),
        source_stage_machine_code=source_stage_machine_code,
        source_lane=source_lane,
        source_txt_path=source_txt_path,
        source_txt_sha256=sha256_file(source_txt_path),
        source_bundle_txt=source_bundle_txt,
        factsheet_path=factsheet_path if factsheet_path else Path(""),
        source_pdf_code=source_pdf_code,
        source_pdf_original_name=source_pdf_original_name,
        source_pdf_internal_name=str(facts.get("source_pdf_internal_name") or "").strip(),
        source_pdf_hash=str(facts.get("source_pdf_hash") or "").strip(),
        page_count=facts.get("page_count") if isinstance(facts.get("page_count"), int) else None,
        executive_session_code=executive_session_code,
        source_integrity=source_integrity,
        session_count=to_int(payload.get("session_count"), default=len(sessions)),
        status_counts=status_counts,
        sessions=sessions,
    )
    return candidate, None


def choose_best_candidate(candidates: Sequence[StageCandidate]) -> StageCandidate:
    def rank_key(c: StageCandidate) -> tuple[str, str]:
        return (c.source_stage_captured_at, str(c.stage_json_path))

    return sorted(candidates, key=rank_key, reverse=True)[0]


def render_summary_text(payload: dict[str, Any]) -> str:
    summary = payload.get("executive_session_summary")
    if not isinstance(summary, dict):
        summary = {}
    header = [
        f"EXECUTIVE_SESSION_CODE: {payload.get('executive_session_code')}",
        f"SOURCE_PDF_CODE: {payload.get('linked_source_pdf_code')}",
        f"SOURCE_LANE: {payload.get('source_lane')}",
        f"SECTION_COUNT: {summary.get('section_count')}",
        "",
    ]
    body: list[str] = []
    for sec in payload.get("executive_session_sections", []):
        body.append(
            f"[{sec['section_id']}] {sec['candidate_status']} "
            f"lines {sec['start_line']}-{sec['end_line']} "
            f"(heading_line={sec['heading_line_number']}, reason_lines={sec['reason_line_count']})"
        )
        body.append(f"HEADING: {sec['heading_text']}")
        body.append(sec["text"])
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def build_payload(candidate: StageCandidate, run_id: str) -> dict[str, Any]:
    source_match = AG_CODE_RE.match(candidate.source_pdf_code)
    assert source_match is not None
    created_ymd = source_match.group(2)
    anchor_meeting_date = ymd_to_iso(created_ymd)

    sections: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    reason_category_counts: Counter[str] = Counter()
    code_reference_counts: Counter[str] = Counter()
    total_reason_lines = 0

    for idx, raw in enumerate(candidate.sessions, start=1):
        candidate_status = str(raw.get("candidate_status") or "").strip().lower() or "unknown"
        status_counts[candidate_status] += 1

        heading_line_number = to_int(raw.get("heading_line_number"), default=0)
        heading_text = str(raw.get("heading_text") or "").strip()
        start_line = to_int(raw.get("start_line"), default=0)
        end_line = to_int(raw.get("end_line"), default=0)
        text = str(raw.get("text") or "").strip()
        reason_line_count = to_int(raw.get("reason_line_count"), default=0)
        total_reason_lines += reason_line_count

        reason_categories_raw = raw.get("reason_categories")
        if not isinstance(reason_categories_raw, list):
            reason_categories_raw = []
        reason_categories = [str(x).strip() for x in reason_categories_raw if str(x).strip()]
        for cat in reason_categories:
            reason_category_counts[cat] += 1

        code_refs_raw = raw.get("code_references")
        if not isinstance(code_refs_raw, list):
            code_refs_raw = []
        code_refs = [str(x).strip() for x in code_refs_raw if str(x).strip()]
        for code_ref in code_refs:
            code_reference_counts[code_ref] += 1

        reason_lines_raw = raw.get("reason_lines")
        reason_lines: list[dict[str, Any]] = []
        if isinstance(reason_lines_raw, list):
            for line in reason_lines_raw:
                if isinstance(line, dict):
                    reason_lines.append(line)

        section = {
            "section_id": f"ES{idx:03d}",
            "session_key": str(raw.get("session_key") or "").strip(),
            "candidate_status": candidate_status,
            "heading_line_number": heading_line_number,
            "heading_text": heading_text,
            "start_line": start_line,
            "end_line": end_line,
            "reason_line_count": reason_line_count,
            "reason_categories": reason_categories,
            "code_references": code_refs,
            "reason_lines": reason_lines,
            "heading_line_match": bool(raw.get("heading_line_match")),
            "reason_line_matches": to_int(raw.get("reason_line_matches"), default=0),
            "reason_line_total": to_int(raw.get("reason_line_total"), default=0),
            "text": text,
            "text_sha256": sha256_text(text),
        }
        sections.append(section)

    glossary_section = build_glossary_section(
        executive_session_code=candidate.executive_session_code,
        source_pdf_code=candidate.source_pdf_code,
        sections=sections,
    )

    source_integrity_score = candidate.source_integrity.get("integrity_score")
    source_integrity_pass = candidate.source_integrity.get("pass")
    source_integrity_threshold = candidate.source_integrity.get("threshold")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "executive_session_preparse_record",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "preparse_run_id": run_id,
        "source_lane": candidate.source_lane or SOURCE_LANE,
        "jurisdiction": JURISDICTION,
        "executive_session_code": candidate.executive_session_code,
        "artifact_machine_code": candidate.executive_session_code,
        "linked_source_pdf_code": candidate.source_pdf_code,
        "meeting_context": {
            "anchor_meeting_date": anchor_meeting_date,
            "anchor_meeting_type": None,
        },
        "lineage": {
            "source_stage_run_id": candidate.source_stage_run_id,
            "source_stage_captured_at": candidate.source_stage_captured_at,
            "source_stage_machine_code": candidate.source_stage_machine_code,
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "source_txt_path": str(candidate.source_txt_path),
            "source_txt_sha256": candidate.source_txt_sha256,
            "source_bundle_txt": str(candidate.source_bundle_txt) if candidate.source_bundle_txt else None,
            "factsheet_path": str(candidate.factsheet_path),
            "source_pdf_original_name": candidate.source_pdf_original_name,
            "source_pdf_internal_name": candidate.source_pdf_internal_name,
            "source_pdf_hash": candidate.source_pdf_hash,
            "source_pdf_page_count": candidate.page_count,
        },
        "executive_session_summary": {
            "section_count": len(sections),
            "status_counts": dict(status_counts),
            "reason_category_counts": dict(reason_category_counts),
            "code_reference_counts": dict(code_reference_counts),
            "total_reason_lines": total_reason_lines,
            "source_integrity_score": source_integrity_score,
            "source_integrity_pass": source_integrity_pass,
            "source_integrity_threshold": source_integrity_threshold,
        },
        "executive_session_sections": sections,
        "glossary": glossary_section,
        "pusher_ready": {
            "report_packet_id": candidate.executive_session_code,
            "source_id": candidate.source_pdf_code,
            "content_mode": "session_sections",
            "is_complete_document": False,
            "glossary_scope_text_hint": "executive_session_sections[].text",
        },
    }
    return payload


def run_preparse(
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

    discovered = 0
    mapped = 0
    prepared = 0
    skipped_unchanged = 0
    failed = 0

    failure_rows: list[dict[str, Any]] = []
    prepared_rows: list[dict[str, Any]] = []
    manifest_codes = load_manifest_codes()

    groups: dict[str, list[StageCandidate]] = {}
    scoped_stage_files, effective_source_run_id = iter_stage_json_files_scoped(
        STAGING_ROOT,
        source_run_id=source_run_id,
        all_staging=all_staging,
    )

    for stage_json in scoped_stage_files:
        discovered += 1
        candidate, error = parse_stage_candidate(stage_json)
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
        assert candidate is not None
        mapped += 1
        groups.setdefault(candidate.executive_session_code, []).append(candidate)

    chosen: list[StageCandidate] = []
    for _, group in groups.items():
        chosen.append(choose_best_candidate(group))

    chosen.sort(key=lambda c: c.executive_session_code)
    if limit is not None:
        chosen = chosen[:limit]

    run_dir = RUNS_ROOT / run_id
    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)

    for candidate in chosen:
        prev = state_records.get(candidate.executive_session_code, {})
        out_dir = OUTPUT_ROOT / candidate.executive_session_code
        output_json = out_dir / f"{candidate.executive_session_code}.preparse.json"
        output_txt = out_dir / f"{candidate.executive_session_code}.preparse.txt"

        if (
            not force
            and prev.get("source_stage_json_sha256") == candidate.stage_json_sha256
            and prev.get("source_txt_sha256") == candidate.source_txt_sha256
            and output_json.exists()
            and output_txt.exists()
        ):
            skipped_unchanged += 1
            continue

        try:
            payload = build_payload(candidate, run_id=run_id)
            payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
            payload_sha256 = sha256_text(payload_text)

            if not dry_run:
                out_dir.mkdir(parents=True, exist_ok=True)
                output_json.write_text(payload_text, encoding="utf-8")
                output_txt.write_text(render_summary_text(payload), encoding="utf-8")

            prepared += 1
            row = {
                "run_id": run_id,
                "prepared_at": datetime.now().isoformat(timespec="seconds"),
                "schema_version": SCHEMA_VERSION,
                "executive_session_code": candidate.executive_session_code,
                "linked_source_pdf_code": candidate.source_pdf_code,
                "source_stage_json_path": str(candidate.stage_json_path),
                "source_stage_json_sha256": candidate.stage_json_sha256,
                "source_txt_path": str(candidate.source_txt_path),
                "source_txt_sha256": candidate.source_txt_sha256,
                "payload_sha256": payload_sha256,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
                "section_count": len(payload.get("executive_session_sections", [])),
                "glossary_entities_total": int(payload.get("glossary", {}).get("summary", {}).get("entries_total", 0)),
            }
            prepared_rows.append(row)

            state_records[candidate.executive_session_code] = {
                "last_run_id": run_id,
                "last_status": "prepared",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "source_stage_json_path": str(candidate.stage_json_path),
                "source_stage_json_sha256": candidate.stage_json_sha256,
                "source_txt_path": str(candidate.source_txt_path),
                "source_txt_sha256": candidate.source_txt_sha256,
                "linked_source_pdf_code": candidate.source_pdf_code,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
                "payload_sha256": payload_sha256,
                "section_count": len(payload.get("executive_session_sections", [])),
                "glossary_entities_total": int(payload.get("glossary", {}).get("summary", {}).get("entries_total", 0)),
            }
            if not dry_run:
                save_state(state)
                if candidate.executive_session_code not in manifest_codes:
                    append_manifest_rows([row])
                    manifest_codes.add(candidate.executive_session_code)
        except Exception as exc:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "executive_session_code": candidate.executive_session_code,
                    "source_stage_json_path": str(candidate.stage_json_path),
                    "error": str(exc),
                }
            )

    if not dry_run:
        run_manifest = run_dir / "executive_session_preparse_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in prepared_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            run_failures = run_dir / "executive_session_preparse_failures.jsonl"
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
            "mapped_executive_session_codes": mapped,
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
        save_state(state)

    print("=" * 64)
    print("EXECUTIVE SESSION PRE_PARSE SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Source lane: {SOURCE_LANE}")
    print(f"  Staging root: {STAGING_ROOT}")
    print(f"  Source stage scope: {effective_source_run_id}")
    print(f"  Output root: {OUTPUT_ROOT}")
    print(f"  Stage files discovered: {discovered}")
    print(f"  Executive-session codes mapped: {mapped}")
    print(f"  Records prepared: {prepared}")
    print(f"  Records skipped (unchanged): {skipped_unchanged}")
    print(f"  Records failed: {failed}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Run artifacts: {run_dir}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
    print("=" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize staged Executive Session artifacts into pusher-ready Executive Session schema."
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N executive-session codes.")
    parser.add_argument("--force", action="store_true", help="Rebuild outputs even when unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan/map only; do not write outputs.")
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Use a specific pull run id under _staging (for example RUN_20260522T222706). Default: latest run only.",
    )
    parser.add_argument(
        "--all-staging",
        action="store_true",
        help="Process all staged runs under _staging.",
    )
    args = parser.parse_args()

    run_preparse(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        source_run_id=args.source_run_id,
        all_staging=args.all_staging,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
