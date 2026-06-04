import os
#!/usr/bin/env python
"""
Public Hearing PARSE (Agenda-Staging Lane)

Transforms staged agenda-mined official announcement excerpts into normalized schema
and writes push-ready artifacts into:
  _Sources/M1-Meetings/Public_Hearings/_output/<public_hearing_code>/

Strict invariant:
  - PARSE only (schema normalization + lineage packaging + glossary section)
  - No DB writes

Linkage contract:
  - source_pdf_code:      M1.AG.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
  - public_hearing_code:  M1.AG.PH.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


PUBLIC_HEARING_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Public_Hearings"
STAGING_ROOT = PUBLIC_HEARING_ROOT / "_staging"
OUTPUT_ROOT = PUBLIC_HEARING_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = PUBLIC_HEARING_ROOT / "public_hearing_preparse_state.json"
MANIFEST_FILE = PUBLIC_HEARING_ROOT / "M1_PUBLIC_HEARING_PREPARSE_MANIFEST.jsonl"

SCHEMA_VERSION = "m1.public_hearing.parse.v1"
GLOSSARY_SCHEMA_VERSION = "m1.public_hearing.glossary.v1"
SOURCE_LANE = "agenda_output_public_hearing_notice"
JURISDICTION = "Richlands"

AG_CODE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)
ROLE_LINE_RE = re.compile(
    r"\b([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,2})\s*,\s*"
    r"(Mayor|Clerk|Town Manager|Town Attorney|Interim Town Manager)\b"
)
VIRGINIA_CODE_RE = re.compile(
    r"\b(?:Virginia|VA)\s+Code(?:\s+Section)?\s*§?\s*([0-9]{1,2}\.[0-9]+(?:-[0-9]+)*(?:\([A-Za-z0-9]+\))*)\b",
    re.IGNORECASE,
)
TAX_MAP_RE = re.compile(r"\bTax\s+Map(?:\s+Number)?\s*#?\s*[:\-]?\s*([A-Za-z0-9\- ]{4,36})\b", re.IGNORECASE)
ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Za-z0-9'&.\-]+(?:\s+[A-Za-z0-9'&.\-]+){0,8}\s+"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Boulevard|Blvd|Way|Court|Ct))\b",
    re.IGNORECASE,
)
ORG_MARKERS: list[tuple[str, str, str, float]] = [
    ("Richlands Town Council", "BOARD", "MENTIONED_IN_RECORD", 0.95),
    ("Town Council of the Town of Richlands", "BOARD", "MENTIONED_IN_RECORD", 0.95),
    ("Richlands Planning Commission", "BOARD", "MENTIONED_IN_RECORD", 0.95),
    ("Town of Richlands", "ORGANIZATION", "MENTIONED_IN_RECORD", 0.92),
    ("Richlands Municipal Building", "LOCATION", "MEETING_LOCATION", 0.90),
    ("Richlands Town Hall", "LOCATION", "MEETING_LOCATION", 0.90),
]


@dataclass
class StageCandidate:
    stage_json_path: Path
    stage_json_sha256: str
    source_stage_run_id: str
    source_stage_captured_at: str
    source_stage_machine_code: str
    source_txt_path: Path
    source_txt_sha256: str
    source_pdf_code: str
    public_hearing_code: str
    factsheet_path: Path
    source_pdf_original_name: str
    source_pdf_internal_name: str
    source_pdf_hash: str
    page_count: int | None
    excerpts: list[dict]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    PUBLIC_HEARING_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    PUBLIC_HEARING_ROOT.mkdir(parents=True, exist_ok=True)
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
        code = str(row.get("public_hearing_code") or "").strip()
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


def to_float(value: object, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return default
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_name(text: str) -> str:
    return normalize_ws((text or "").strip(" ,;:.|"))


def build_glossary_section(
    public_hearing_code: str,
    source_pdf_code: str,
    excerpts: Sequence[dict],
) -> dict:
    entries: list[dict] = []
    dedupe: set[tuple[str, str, str]] = set()
    by_category: dict[str, int] = {}

    def add_entry(
        category: str,
        canonical_name: str,
        fact_key: str,
        confidence: float,
        excerpt_id: str,
        evidence: str,
        matched_from: str,
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
                    "excerpt_id": excerpt_id,
                },
            }
        )

    for ex in excerpts:
        text = str(ex.get("text") or "")
        excerpt_id = str(ex.get("excerpt_id") or "")

        for marker, category, fact_key, conf in ORG_MARKERS:
            if re.search(rf"\b{re.escape(marker)}\b", text, re.IGNORECASE):
                add_entry(
                    category=category,
                    canonical_name=marker,
                    fact_key=fact_key,
                    confidence=conf,
                    excerpt_id=excerpt_id,
                    evidence=text,
                    matched_from="known_marker",
                )

        for m in VIRGINIA_CODE_RE.finditer(text):
            code_value = clean_name(m.group(1))
            if not code_value:
                continue
            add_entry(
                category="LAW",
                canonical_name=f"Virginia Code {code_value}",
                fact_key="STATUTE_REFERENCE",
                confidence=0.93,
                excerpt_id=excerpt_id,
                evidence=text,
                matched_from="virginia_code_pattern",
            )

        for m in TAX_MAP_RE.finditer(text):
            tax_map_value = clean_name(m.group(1))
            if len(tax_map_value) < 4:
                continue
            add_entry(
                category="LOCATION",
                canonical_name=f"Tax Map {tax_map_value}",
                fact_key="PROPERTY_TAX_MAP",
                confidence=0.90,
                excerpt_id=excerpt_id,
                evidence=text,
                matched_from="tax_map_pattern",
            )

        for m in ADDRESS_RE.finditer(text):
            addr = clean_name(m.group(1))
            if len(addr) < 8:
                continue
            add_entry(
                category="LOCATION",
                canonical_name=addr,
                fact_key="PROPERTY_STREET_ADDRESS",
                confidence=0.88,
                excerpt_id=excerpt_id,
                evidence=text,
                matched_from="address_pattern",
            )

        for m in ROLE_LINE_RE.finditer(text):
            person_name = clean_name(m.group(1))
            role_name = clean_name(m.group(2))
            if len(person_name.split()) < 2:
                continue
            add_entry(
                category="PEOPLE",
                canonical_name=person_name,
                fact_key="POTENTIAL_IDENTITY",
                confidence=0.91,
                excerpt_id=excerpt_id,
                evidence=f"{person_name}, {role_name}",
                matched_from="role_signature_pattern",
            )

    entries.sort(key=lambda e: (e["category"], e["canonical_name"], e["fact_key"]))
    for idx, ent in enumerate(entries, start=1):
        ent["entry_id"] = f"GL{idx:03d}"

    return {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "record_type": "public_hearing_glossary_section",
        "source_public_hearing_code": public_hearing_code,
        "source_pdf_code": source_pdf_code,
        "glossary_scope_text_hint": "public_hearing_excerpts[].text",
        "summary": {
            "entries_total": len(entries),
            "entries_by_category": by_category,
        },
        "entities": entries,
    }


def iter_stage_json_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.public_hearing.json"))


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
    files = sorted(run_dir.rglob("*.public_hearing.json"))
    return files, run_id


def build_public_hearing_code(source_pdf_code: str) -> str | None:
    match = AG_CODE_RE.match(source_pdf_code.strip())
    if not match:
        return None
    docnum, created_ymd, pulled_ymd = match.group(1), match.group(2), match.group(3)
    return f"M1.AG.PH.{docnum}.{created_ymd}.{pulled_ymd}"


def ymd_to_iso(ymd: str) -> str | None:
    if not re.fullmatch(r"\d{8}", ymd):
        return None
    try:
        return datetime.strptime(ymd, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def read_factsheet(source_txt_path: Path, source_stage_machine_code: str) -> tuple[Path | None, dict | None]:
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

    source_stage_machine_code = str(payload.get("machine_code") or "").strip()
    source_txt_raw = str(payload.get("source_txt") or "").strip()
    if not source_stage_machine_code:
        return None, "missing_machine_code"
    if not source_txt_raw:
        return None, "missing_source_txt"

    source_txt_path = Path(source_txt_raw)
    if not source_txt_path.exists():
        return None, "missing_source_txt_file"

    factsheet_path, facts = read_factsheet(source_txt_path, source_stage_machine_code)
    if not facts:
        return None, "missing_or_invalid_factsheet"

    source_pdf_original_name = str(facts.get("source_pdf_original_name") or "").strip()
    if not source_pdf_original_name.lower().endswith(".pdf"):
        return None, "missing_source_pdf_original_name"
    source_pdf_code = source_pdf_original_name[:-4]

    public_hearing_code = build_public_hearing_code(source_pdf_code)
    if not public_hearing_code:
        return None, f"unmappable_source_pdf_code: {source_pdf_code}"

    excerpts = payload.get("excerpts")
    if not isinstance(excerpts, list):
        return None, "missing_excerpts"

    source_txt_sha256 = sha256_file(source_txt_path)

    candidate = StageCandidate(
        stage_json_path=stage_json_path,
        stage_json_sha256=sha256_text(stage_text),
        source_stage_run_id=str(payload.get("run_id") or "").strip(),
        source_stage_captured_at=str(payload.get("captured_at") or "").strip(),
        source_stage_machine_code=source_stage_machine_code,
        source_txt_path=source_txt_path,
        source_txt_sha256=source_txt_sha256,
        source_pdf_code=source_pdf_code,
        public_hearing_code=public_hearing_code,
        factsheet_path=factsheet_path if factsheet_path else Path(""),
        source_pdf_original_name=source_pdf_original_name,
        source_pdf_internal_name=str(facts.get("source_pdf_internal_name") or "").strip(),
        source_pdf_hash=str(facts.get("source_pdf_hash") or "").strip(),
        page_count=facts.get("page_count") if isinstance(facts.get("page_count"), int) else None,
        excerpts=excerpts,
    )
    return candidate, None


def choose_best_candidate(candidates: Sequence[StageCandidate]) -> StageCandidate:
    def rank_key(c: StageCandidate) -> tuple[str, str]:
        return (c.source_stage_captured_at, str(c.stage_json_path))

    return sorted(candidates, key=rank_key, reverse=True)[0]


def render_summary_text(payload: dict) -> str:
    summary = payload["public_hearing_excerpt_summary"]
    header = [
        f"PUBLIC_HEARING_CODE: {payload['public_hearing_code']}",
        f"SOURCE_PDF_CODE: {payload['linked_source_pdf_code']}",
        f"SOURCE_LANE: {payload['source_lane']}",
        f"EXCERPT_COUNT: {summary['excerpt_count']}",
        f"STRONG_EXCERPTS: {summary['strong_excerpt_count']}",
        f"WEAK_EXCERPTS: {summary['weak_excerpt_count']}",
        "",
    ]
    body: list[str] = []
    for ex in payload.get("public_hearing_excerpts", []):
        signals = ex.get("signals") if isinstance(ex.get("signals"), dict) else {}
        body.append(
            f"[{ex['excerpt_id']}] {ex['kind']} lines {ex['start_line']}-{ex['end_line']} "
            f"(pattern={signals.get('match_pattern')}, confidence={signals.get('confidence')})"
        )
        body.append(ex["text"])
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def build_payload(candidate: StageCandidate, run_id: str) -> dict:
    source_match = AG_CODE_RE.match(candidate.source_pdf_code)
    assert source_match is not None
    created_ymd = source_match.group(2)
    anchor_meeting_date = ymd_to_iso(created_ymd)

    excerpt_rows: list[dict] = []
    kind_counts: dict[str, int] = {}
    pattern_counts: dict[str, int] = {}
    strong_count = 0
    weak_count = 0
    max_confidence: float = 0.0

    for idx, raw in enumerate(candidate.excerpts, start=1):
        kind = str(raw.get("kind") or "unknown").strip()
        start_line = to_int(raw.get("start_line"), default=0)
        end_line = to_int(raw.get("end_line"), default=0)
        text = str(raw.get("text") or "").strip()
        signals = raw.get("signals") if isinstance(raw.get("signals"), dict) else {}

        pattern = str(signals.get("match_pattern") or "unknown").strip()
        confidence = to_float(signals.get("confidence"), default=0.0) or 0.0
        strength = str(signals.get("match_strength") or "").strip().lower()

        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
        if strength == "strong":
            strong_count += 1
        elif strength == "weak":
            weak_count += 1
        if confidence > max_confidence:
            max_confidence = confidence

        excerpt_rows.append(
            {
                "excerpt_id": f"EX{idx:03d}",
                "kind": kind,
                "start_line": start_line,
                "end_line": end_line,
                "text": text,
                "text_sha256": sha256_text(text),
                "signals": {
                    "match_strength": strength,
                    "match_pattern": pattern,
                    "confidence": confidence,
                },
            }
        )

    notice_label = "STRONG_NOTICE" if strong_count > 0 else "WEAK_NOTICE"
    glossary_section = build_glossary_section(
        public_hearing_code=candidate.public_hearing_code,
        source_pdf_code=candidate.source_pdf_code,
        excerpts=excerpt_rows,
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "public_hearing_parse_record",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "parse_run_id": run_id,
        "source_lane": SOURCE_LANE,
        "jurisdiction": JURISDICTION,
        "public_hearing_code": candidate.public_hearing_code,
        "artifact_machine_code": candidate.public_hearing_code,
        "linked_source_pdf_code": candidate.source_pdf_code,
        "meeting_context": {
            "anchor_meeting_date": anchor_meeting_date,
            "anchor_meeting_type": "PUBLIC_HEARING",
        },
        "lineage": {
            "source_stage_run_id": candidate.source_stage_run_id,
            "source_stage_captured_at": candidate.source_stage_captured_at,
            "source_stage_machine_code": candidate.source_stage_machine_code,
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "source_txt_path": str(candidate.source_txt_path),
            "source_txt_sha256": candidate.source_txt_sha256,
            "agenda_output_dir": str(candidate.source_txt_path.parent),
            "factsheet_path": str(candidate.factsheet_path),
            "source_pdf_original_name": candidate.source_pdf_original_name,
            "source_pdf_internal_name": candidate.source_pdf_internal_name,
            "source_pdf_hash": candidate.source_pdf_hash,
            "source_pdf_page_count": candidate.page_count,
        },
        "public_hearing_excerpt_summary": {
            "excerpt_count": len(excerpt_rows),
            "excerpt_kind_counts": kind_counts,
            "pattern_counts": pattern_counts,
            "strong_excerpt_count": strong_count,
            "weak_excerpt_count": weak_count,
            "notice_label": notice_label,
            "max_confidence": max_confidence,
        },
        "public_hearing_excerpts": excerpt_rows,
        "glossary": glossary_section,
        "pusher_ready": {
            "notice_id": candidate.public_hearing_code,
            "source_id": candidate.source_pdf_code,
            "content_mode": "excerpt_pack",
            "is_complete_notice_document": False,
            "glossary_scope_text_hint": "public_hearing_excerpts[].text",
            "notice_label": notice_label,
            "max_confidence": max_confidence,
        },
    }
    return payload


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

    discovered = 0
    mapped = 0
    prepared = 0
    skipped_unchanged = 0
    failed = 0

    failure_rows: list[dict] = []
    prepared_rows: list[dict] = []
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
        groups.setdefault(candidate.public_hearing_code, []).append(candidate)

    chosen: list[StageCandidate] = []
    for _, group in groups.items():
        chosen.append(choose_best_candidate(group))

    chosen.sort(key=lambda c: c.public_hearing_code)
    if limit is not None:
        chosen = chosen[:limit]

    run_dir = RUNS_ROOT / run_id
    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)

    for candidate in chosen:
        prev = state_records.get(candidate.public_hearing_code, {})
        output_json = OUTPUT_ROOT / candidate.public_hearing_code / f"{candidate.public_hearing_code}.parse.json"
        output_txt = OUTPUT_ROOT / candidate.public_hearing_code / f"{candidate.public_hearing_code}.parse.txt"
        compat_preparse_json = OUTPUT_ROOT / candidate.public_hearing_code / f"{candidate.public_hearing_code}.preparse.json"
        compat_preparse_txt = OUTPUT_ROOT / candidate.public_hearing_code / f"{candidate.public_hearing_code}.preparse.txt"

        if (
            not force
            and prev.get("source_stage_json_sha256") == candidate.stage_json_sha256
            and output_json.exists()
            and output_txt.exists()
        ):
            skipped_unchanged += 1
            continue

        payload = build_payload(candidate, run_id)
        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        payload_sha256 = sha256_text(payload_text)

        if not dry_run:
            out_dir = OUTPUT_ROOT / candidate.public_hearing_code
            out_dir.mkdir(parents=True, exist_ok=True)
            output_json.write_text(payload_text, encoding="utf-8")
            output_txt.write_text(render_summary_text(payload), encoding="utf-8")
            # Backward compatibility for legacy loaders expecting .preparse.*
            compat_preparse_json.write_text(payload_text, encoding="utf-8")
            compat_preparse_txt.write_text(render_summary_text(payload), encoding="utf-8")

        prepared += 1
        row = {
            "run_id": run_id,
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "public_hearing_code": candidate.public_hearing_code,
            "source_pdf_code": candidate.source_pdf_code,
            "source_stage_machine_code": candidate.source_stage_machine_code,
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "payload_sha256": payload_sha256,
            "output_json": str(output_json),
            "output_txt": str(output_txt),
            "output_json_compat_preparse": str(compat_preparse_json),
            "output_txt_compat_preparse": str(compat_preparse_txt),
            "excerpt_count": len(payload.get("public_hearing_excerpts", [])),
            "glossary_entities_total": int(payload.get("glossary", {}).get("summary", {}).get("entries_total", 0)),
            "strong_excerpt_count": payload["public_hearing_excerpt_summary"].get("strong_excerpt_count"),
            "weak_excerpt_count": payload["public_hearing_excerpt_summary"].get("weak_excerpt_count"),
        }
        prepared_rows.append(row)

        state_records[candidate.public_hearing_code] = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "last_run_id": run_id,
            "last_status": "prepared",
            "source_stage_json_path": str(candidate.stage_json_path),
            "source_stage_json_sha256": candidate.stage_json_sha256,
            "payload_sha256": payload_sha256,
            "output_json": str(output_json),
            "output_txt": str(output_txt),
            "excerpt_count": len(payload.get("public_hearing_excerpts", [])),
            "glossary_entities_total": int(payload.get("glossary", {}).get("summary", {}).get("entries_total", 0)),
        }
        if not dry_run:
            save_state(state)
            if candidate.public_hearing_code not in manifest_codes:
                append_manifest_rows([row])
                manifest_codes.add(candidate.public_hearing_code)

    if not dry_run:
        run_manifest = run_dir / "public_hearing_parse_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in prepared_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            run_failures = run_dir / "public_hearing_parse_failures.jsonl"
            with run_failures.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "source_lane": SOURCE_LANE,
            "staging_root": str(STAGING_ROOT),
            "source_stage_scope": effective_source_run_id,
            "output_root": str(OUTPUT_ROOT),
            "discovered_stage_json": discovered,
            "mapped_public_hearing_codes": mapped,
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

    print("=" * 64)
    print("PUBLIC HEARING PARSE SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Source lane: {SOURCE_LANE}")
    print(f"  Staging root: {STAGING_ROOT}")
    print(f"  Source stage scope: {effective_source_run_id}")
    print(f"  Output root: {OUTPUT_ROOT}")
    print(f"  Stage files discovered: {discovered}")
    print(f"  Public-hearing codes mapped: {mapped}")
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


def run_preparse(limit: int | None = None, force: bool = False, dry_run: bool = False) -> None:
    # Backward-compatible alias for existing callers.
    run_parse(limit=limit, force=force, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse agenda-mined official-announcement staging artifacts into push-ready schema."
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N public-hearing codes.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild outputs even if source stage hash is unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and summarize only; do not write output files.",
    )
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Use a specific pull run id under _staging (for example RUN_20260510T171959). Default: latest run only.",
    )
    parser.add_argument(
        "--all-staging",
        action="store_true",
        help="Process all staged runs under _staging (legacy behavior).",
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
