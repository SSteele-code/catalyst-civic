#!/usr/bin/env python
"""
Minutes HOVER (Glossary Candidate Lane)

Reads normalized Minutes PRE_PARSE/OCR outputs and generates glossary-hover
candidate artifacts for QA and later standalone glossary loading.

Strict invariant:
  - Hover/extract only
  - No DB writes
  - No glossary writes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


MINUTES_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Minutes")
OUTPUT_ROOT = MINUTES_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = MINUTES_ROOT / "minutes_glossary_hover_state.json"
MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_GLOSSARY_HOVER_MANIFEST.jsonl"
CANDIDATES_SNAPSHOT_FILE = MINUTES_ROOT / "M1_MINUTES_GLOSSARY_HOVER_CANDIDATES_SNAPSHOT.jsonl"

SCHEMA_VERSION = "m1.minutes.glossary_hover.v1"
SOURCE_LANE = "minutes_output_glossary_hover"
JURISDICTION = "Richlands"

CCO_ROOT = Path(r"C:\Users\simon\CatalystCivic\_CCO")
CCO_GLOSSARY_FILE = CCO_ROOT / "CORE" / "CCO_ONTOLOGY_EXTRACT.json"
CCO_ROSTER_FILE = (
    CCO_ROOT
    / "Mode_1_MEETINGS"
    / "STATE"
    / "VA-Virginia"
    / "Tazewell County"
    / "Township_City"
    / "Richlands"
    / "Roster"
    / "Richlands_Roster.json"
)

MINUTES_DIR_RE = re.compile(r"^M1\.(?:AG\.)?MN\.\d{6}\.\d{8}\.\d{8}$", re.IGNORECASE)
PREPARSE_JSON_RE = re.compile(r"^M1\.(?:AG\.)?MN\.\d{6}\.\d{8}\.\d{8}\.preparse\.json$", re.IGNORECASE)

ROLE_LINE_PATTERNS: list[tuple[str, str]] = [
    ("MAYOR", r"\bMayor\s*:\s*([^\n\r]+)"),
    ("TOWN_MANAGER", r"\b(?:Interim\s+)?Town\s+Manager\s*:\s*([^\n\r]+)"),
    ("TOWN_ATTORNEY", r"\bTown\s+Attorney\s*:\s*([^\n\r]+)"),
    ("TOWN_CLERK", r"\bTown\s+Clerk\s*:\s*([^\n\r]+)"),
    ("FINANCE_MANAGER", r"\bFinance\s+Manager\s*:\s*([^\n\r]+)"),
    ("OFFICE_MANAGER", r"\bOffice\s+Manager\s*:\s*([^\n\r]+)"),
    ("COUNCIL_MEMBER", r"\bCouncil\s+Members?\s*:\s*([^\n\r]+)"),
    ("PLANNING_COMMISSION", r"\bPlanning\s+Commission\s*:\s*([^\n\r]+)"),
]

PERSON_TITLE_MENTION_RE = re.compile(
    r"\b(?:Mayor|Clerk|Attorney|Council(?:man|woman| Member)?|Manager)\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){1,2})",
    re.IGNORECASE,
)

ORG_RE = re.compile(
    r"\b([A-Z][A-Za-z&'\-]+(?:\s+[A-Z][A-Za-z&'\-]+){0,5}\s+"
    r"(?:Committee|Board|Commission|Authority|Association|Department|Agency|Council|Chambers|Town Hall))\b"
)

ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,6}\s+"
    r"(?:STREET|ST|ROAD|RD|AVENUE|AVE|DRIVE|DR|WAY|COURT|CT|LANE|LN|BOULEVARD|BLVD))\.?\b",
    re.IGNORECASE,
)

IGNORE_PERSON_TOKENS = {
    "absent",
    "none",
    "unknown",
    "n/a",
    "na",
    "vacant",
    "open",
}

ADDRESS_SUFFIXES = {
    "street",
    "st",
    "road",
    "rd",
    "avenue",
    "ave",
    "drive",
    "dr",
    "way",
    "court",
    "ct",
    "lane",
    "ln",
    "boulevard",
    "blvd",
}

CIVIC_PREFIX_TOKENS = {
    "appalachian",
    "authority",
    "board",
    "bluff",
    "bluefield",
    "cedar",
    "chamber",
    "chambers",
    "city",
    "commission",
    "council",
    "county",
    "department",
    "development",
    "electric",
    "fire",
    "hall",
    "health",
    "industrial",
    "planning",
    "police",
    "power",
    "recreation",
    "richlands",
    "street",
    "tazewell",
    "town",
    "virginia",
}

MAX_CCO_SEED_MATCHES_PER_NAME = 25


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
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
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
        code = str(row.get("minutes_code") or "").strip()
        if code:
            codes.add(code)
    return codes


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clip(text: str, limit: int) -> str:
    t = normalize_ws(text)
    if limit <= 0 or len(t) <= limit:
        return t
    return t[: max(1, limit - 1)].rstrip() + "…"


def clean_name(name: str) -> str:
    s = normalize_ws(name)
    s = re.sub(r"\[[^\]]+\]", "", s)
    s = s.strip(" ,;:.|-")
    return normalize_ws(s)


def normalize_org_name(name: str) -> str:
    s = clean_name(name)
    s = re.sub(r"^(?:the)\s+", "", s, flags=re.IGNORECASE)
    return clean_name(s)


def is_noise_org_name(name: str) -> bool:
    low = normalize_ws(name).lower()
    if not low:
        return True
    if low in {"council", "board", "commission", "committee", "agency", "department", "the council"}:
        return True
    if low.startswith("absent ") or low.startswith("present "):
        return True
    if low.startswith("with the ") or low.startswith("for the "):
        return True
    if low.startswith("joint ") or low.startswith("special "):
        return True
    if " meeting " in f" {low} " or " hearing " in f" {low} ":
        return True
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", low)
    if len(toks) < 2 or len(toks) > 7:
        return True
    tail = toks[-1]
    if tail in {"council", "commission", "board", "committee", "department", "agency", "authority"}:
        prefix = " ".join(toks[:-1]).title()
        prefix_toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", prefix)
        maybe_person_prefix = (
            2 <= len(prefix_toks) <= 3
            and all(re.fullmatch(r"[A-Z][a-zA-Z'\-]+", t) for t in prefix_toks)
            and not any(t.lower() in CIVIC_PREFIX_TOKENS for t in prefix_toks)
        )
        if maybe_person_prefix and is_likely_person(prefix):
            return True
        if toks[0] in {"if", "regular", "special", "joint", "section"}:
            return True
        if low in {"town council", "regular council", "special council", "joint council"}:
            return True
    return False


def is_likely_address(addr: str) -> bool:
    s = clean_name(addr)
    if not s:
        return False
    parts = re.findall(r"[A-Za-z0-9#]+", s)
    if len(parts) < 3:
        return False
    if not re.fullmatch(r"\d{1,5}", parts[0]):
        return False
    if parts[-1].lower() not in ADDRESS_SUFFIXES:
        return False
    mid = parts[1:-1]
    if not mid:
        return False
    if any(len(re.sub(r"[^A-Za-z]", "", tok)) == 1 for tok in mid):
        return False
    letters_only = re.sub(r"[^A-Za-z]", "", "".join(mid))
    if len(letters_only) < 4:
        return False
    if not re.search(r"[AEIOUYaeiouy]", letters_only):
        return False
    return True


def has_ocr_like_noise(name: str) -> bool:
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", name or "")
    if not toks:
        return True
    bad = 0
    for tok in toks:
        letters = re.sub(r"[^A-Za-z]", "", tok)
        if len(letters) >= 5 and not re.search(r"[AEIOUYaeiouy]", letters):
            bad += 1
        if re.search(r"(.)\1\1", letters):
            bad += 1
    return bad > 0


def is_likely_person(name: str) -> bool:
    s = clean_name(name)
    if not s:
        return False
    low = s.lower()
    if low in IGNORE_PERSON_TOKENS:
        return False
    if any(tok in low for tok in ("meeting", "minutes", "council", "commission", "department", "hearing")):
        return False
    toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", s)
    if len(toks) < 2 or len(toks) > 4:
        return False
    valid = 0
    for tok in toks:
        if re.fullmatch(r"[A-Z][a-zA-Z'\-]{1,}", tok) or re.fullmatch(r"[A-Z]{2,}", tok):
            valid += 1
    return valid >= 2


def split_people(raw: str) -> list[str]:
    text = normalize_ws(raw)
    if not text:
        return []
    text = re.sub(r"\s+(?:and|&)\s+", ", ", text, flags=re.IGNORECASE)
    parts = [clean_name(p) for p in text.split(",")]
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        p_low = p.lower()
        if p_low in IGNORE_PERSON_TOKENS:
            continue
        out.append(p)
    return out


def context_slice(text: str, start: int, end: int, window: int = 80) -> str:
    s = max(0, start - window)
    e = min(len(text), end + window)
    return clip(text[s:e], 240)


def load_cco_seed() -> dict[str, dict[str, str]]:
    known: dict[str, dict[str, str]] = {}

    if CCO_GLOSSARY_FILE.exists():
        try:
            payload = json.loads(CCO_GLOSSARY_FILE.read_text(encoding="utf-8"))
            for person in payload.get("people", []):
                name = clean_name(str(person.get("full_name") or ""))
                if name:
                    known[name.upper()] = {"category": "PEOPLE", "source": "cco_core", "kind": "person"}
                for alias in person.get("aliases", []):
                    alias_name = clean_name(str(alias or ""))
                    if alias_name:
                        known[alias_name.upper()] = {"category": "PEOPLE", "source": "cco_core", "kind": "person_alias"}

            for board in payload.get("boards", []):
                name = clean_name(str(board.get("legal_name") or ""))
                if name:
                    known[name.upper()] = {"category": "BOARD", "source": "cco_core", "kind": "board"}

            for agency in payload.get("agencies", []):
                name = clean_name(str(agency.get("legal_name") or ""))
                if name:
                    known[name.upper()] = {"category": "AGENCY", "source": "cco_core", "kind": "agency"}
        except Exception:
            pass

    if CCO_ROSTER_FILE.exists():
        try:
            roster = json.loads(CCO_ROSTER_FILE.read_text(encoding="utf-8"))
            if isinstance(roster, dict):
                for _, by_role in roster.items():
                    if not isinstance(by_role, dict):
                        continue
                    for _, value in by_role.items():
                        if isinstance(value, str):
                            names = [value]
                        elif isinstance(value, list):
                            names = [str(v) for v in value]
                        else:
                            names = []
                        for raw_name in names:
                            n = clean_name(raw_name)
                            if not n or "[" in raw_name:
                                continue
                            if is_likely_person(n):
                                known[n.upper()] = {"category": "PEOPLE", "source": "cco_roster", "kind": "roster_name"}
        except Exception:
            pass

    return known


def iter_preparse_jsons() -> list[Path]:
    if not OUTPUT_ROOT.exists():
        return []
    out: list[Path] = []
    for d in sorted(OUTPUT_ROOT.iterdir()):
        if not d.is_dir() or not MINUTES_DIR_RE.match(d.name):
            continue
        p = d / f"{d.name}.preparse.json"
        if p.exists() and PREPARSE_JSON_RE.match(p.name):
            out.append(p)
    return out


def extract_text_from_preparse(payload: dict) -> tuple[str, int]:
    excerpts = payload.get("minutes_excerpts")
    if not isinstance(excerpts, list):
        return "", 0
    parts: list[str] = []
    for ex in excerpts:
        if not isinstance(ex, dict):
            continue
        t = str(ex.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts).strip(), len(parts)


def dedupe_entities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        category = str(row.get("category") or "").upper()
        name = clean_name(str(row.get("canonical_name") or ""))
        fact_key = str(row.get("fact_key") or "").upper()
        if not category or not name or not fact_key:
            continue
        key = (category, name.upper(), fact_key)
        current = unique.get(key)
        if current is None or float(row.get("confidence", 0.0)) > float(current.get("confidence", 0.0)):
            row["category"] = category
            row["canonical_name"] = name
            row["fact_key"] = fact_key
            unique[key] = row
    return sorted(unique.values(), key=lambda r: (r["category"], r["canonical_name"], r["fact_key"]))


def compute_entity_qa_flags(entity: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    category = str(entity.get("category") or "").upper()
    name = clean_name(str(entity.get("canonical_name") or ""))
    conf = float(entity.get("confidence") or 0.0)
    if conf < 0.76:
        flags.append("low_confidence")

    if category in {"ORGANIZATION", "BOARD", "AGENCY"}:
        low = name.lower()
        if len(name.split()) > 6:
            flags.append("long_phrase")
        if " meeting " in f" {low} " or " hearing " in f" {low} ":
            flags.append("procedural_phrase")
        if is_noise_org_name(name):
            flags.append("possible_org_noise")

    fact_key = str(entity.get("fact_key") or "").upper()
    if category == "LOCATION" and fact_key == "PROPERTY_STREET_ADDRESS" and not is_likely_address(name):
        flags.append("possible_ocr_noise")

    if category == "PEOPLE" and not is_likely_person(name):
        flags.append("name_shape_warning")

    return sorted(set(flags))


def flatten_candidate_rows(
    run_id: str,
    prepared_at: str,
    minutes_code: str,
    source_preparse_lane: str,
    source_preparse_json: Path,
    output_json: Path,
    source_preparse_sha256: str,
    payload: dict,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ent in payload.get("glossary_entities", []):
        span = ent.get("source_span") if isinstance(ent.get("source_span"), dict) else {}
        rows.append(
            {
                "run_id": run_id,
                "prepared_at": prepared_at,
                "minutes_code": minutes_code,
                "source_lane": SOURCE_LANE,
                "source_preparse_lane": source_preparse_lane,
                "entity_id": str(ent.get("entity_id") or ""),
                "category": str(ent.get("category") or ""),
                "canonical_name": str(ent.get("canonical_name") or ""),
                "fact_key": str(ent.get("fact_key") or ""),
                "confidence": float(ent.get("confidence") or 0.0),
                "match_type": str(ent.get("matched_from") or ""),
                "evidence_text": str(ent.get("evidence") or ""),
                "evidence_sha256": str(ent.get("evidence_sha256") or ""),
                "qa_flags": ent.get("qa_flags") or [],
                "source_span_char_start": span.get("char_start"),
                "source_span_char_end": span.get("char_end"),
                "source_span_excerpt_id": span.get("excerpt_id"),
                "source_span_page_number": span.get("page_number"),
                "source_preparse_json": str(source_preparse_json),
                "source_preparse_sha256": source_preparse_sha256,
                "output_json": str(output_json),
            }
        )
    return rows


def rebuild_candidates_snapshot() -> int:
    rows_written = 0
    tmp_path = CANDIDATES_SNAPSHOT_FILE.with_suffix(".jsonl.tmp")
    with tmp_path.open("w", encoding="utf-8") as out:
        for d in sorted(OUTPUT_ROOT.iterdir()) if OUTPUT_ROOT.exists() else []:
            if not d.is_dir() or not MINUTES_DIR_RE.match(d.name):
                continue
            hover_json = d / f"{d.name}.glossary_hover.json"
            if not hover_json.exists():
                continue
            try:
                payload = json.loads(hover_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            run_id = str(payload.get("hover_run_id") or "")
            prepared_at = str(payload.get("prepared_at") or "")
            source_preparse_lane = str(((payload.get("lineage") or {}).get("source_preparse_lane")) or "")
            source_preparse_json = Path(str(((payload.get("lineage") or {}).get("source_preparse_json_path")) or ""))
            source_preparse_sha256 = str(((payload.get("lineage") or {}).get("source_preparse_sha256")) or "")
            for row in flatten_candidate_rows(
                run_id=run_id,
                prepared_at=prepared_at,
                minutes_code=d.name,
                source_preparse_lane=source_preparse_lane,
                source_preparse_json=source_preparse_json,
                output_json=hover_json,
                source_preparse_sha256=source_preparse_sha256,
                payload=payload,
            ):
                out.write(json.dumps(row, ensure_ascii=True) + "\n")
                rows_written += 1
    tmp_path.replace(CANDIDATES_SNAPSHOT_FILE)
    return rows_written


def extract_glossary_entities(source_text: str, known_seed: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    upper_text = source_text.upper()

    # 1) Known glossary seed matches (CCO root references).
    for known_name, meta in known_seed.items():
        if not known_name or len(known_name) < 4:
            continue
        seed_pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(known_name)}(?![A-Z0-9])")
        match_count = 0
        for m in seed_pattern.finditer(upper_text):
            context = context_slice(source_text, m.start(), m.end(), window=70)
            found.append(
                {
                    "category": str(meta.get("category") or "PEOPLE").upper(),
                    "canonical_name": clean_name(known_name.title()),
                    "fact_key": "MENTIONED_IN_RECORD",
                    "fact_value": {"seed_source": meta.get("source"), "seed_kind": meta.get("kind"), "context": context},
                    "evidence": f"Known CCO match: {context}",
                    "matched_from": "cco_seed",
                    "confidence": 0.92,
                    "source_span": {"char_start": m.start(), "char_end": m.end()},
                }
            )
            match_count += 1
            if match_count >= MAX_CCO_SEED_MATCHES_PER_NAME:
                break

    # 2) Role-line people extraction.
    for role_key, pattern in ROLE_LINE_PATTERNS:
        for m in re.finditer(pattern, source_text, re.IGNORECASE):
            raw = m.group(1)
            people = split_people(raw)
            role_context = context_slice(source_text, m.start(), m.end(), window=60)
            for person in people:
                if not is_likely_person(person):
                    continue
                found.append(
                    {
                        "category": "PEOPLE",
                        "canonical_name": person,
                        "fact_key": "POTENTIAL_IDENTITY",
                        "fact_value": {"role_title": role_key, "context": role_context},
                        "evidence": f"Role line ({role_key}): {role_context}",
                        "matched_from": "role_line",
                        "confidence": 0.96,
                        "source_span": {"char_start": m.start(), "char_end": m.end()},
                    }
                )

    # 3) Titled mentions.
    for m in re.finditer(PERSON_TITLE_MENTION_RE, source_text):
        name = clean_name(m.group(1))
        if not is_likely_person(name):
            continue
        context = context_slice(source_text, m.start(), m.end(), window=70)
        found.append(
            {
                "category": "PEOPLE",
                "canonical_name": name,
                "fact_key": "POTENTIAL_IDENTITY",
                "fact_value": {"context": context},
                "evidence": f"Title mention: {context}",
                "matched_from": "title_mention",
                "confidence": 0.82,
                "source_span": {"char_start": m.start(), "char_end": m.end()},
            }
        )

    # 4) Organizations.
    for m in re.finditer(ORG_RE, source_text):
        name = normalize_org_name(m.group(1))
        if not name:
            continue
        if is_noise_org_name(name):
            continue
        low = name.lower()
        if "chamber" in low or "town hall" in low:
            category = "LOCATION"
            fact_key = "MEETING_LOCATION"
        elif "commission" in low or "board" in low or "council" in low:
            category = "BOARD"
            fact_key = "MENTIONED_IN_RECORD"
        elif "department" in low or "agency" in low or "authority" in low:
            category = "AGENCY"
            fact_key = "MENTIONED_IN_RECORD"
        else:
            category = "ORGANIZATION"
            fact_key = "MENTIONED_IN_RECORD"
        confidence = 0.86
        if has_ocr_like_noise(name):
            confidence = 0.74
        context = context_slice(source_text, m.start(), m.end(), window=70)
        found.append(
            {
                "category": category,
                "canonical_name": name,
                "fact_key": fact_key,
                "fact_value": {"context": context},
                "evidence": f"Organization mention: {context}",
                "matched_from": "organization_pattern",
                "confidence": confidence,
                "source_span": {"char_start": m.start(), "char_end": m.end()},
            }
        )

    # 5) Street locations.
    for m in re.finditer(ADDRESS_RE, source_text):
        addr = clean_name(m.group(1))
        if not addr:
            continue
        if not is_likely_address(addr):
            continue
        context = context_slice(source_text, m.start(), m.end(), window=70)
        found.append(
            {
                "category": "LOCATION",
                "canonical_name": addr,
                "fact_key": "PROPERTY_STREET_ADDRESS",
                "fact_value": {"full": addr, "context": context},
                "evidence": f"Address mention: {context}",
                "matched_from": "address_pattern",
                "confidence": 0.84,
                "source_span": {"char_start": m.start(), "char_end": m.end()},
            }
        )

    return dedupe_entities(found)


def render_summary_text(payload: dict) -> str:
    header = [
        f"MINUTES_CODE: {payload['minutes_code']}",
        f"SOURCE_LANE: {payload['source_lane']}",
        f"HOVER_ENTITIES: {payload['glossary_hover_summary']['entities_total']}",
        f"SOURCE_TEXT_CHARS: {payload['glossary_hover_summary']['source_text_chars']}",
        "",
    ]
    body: list[str] = []
    for ent in payload.get("glossary_entities", [])[:30]:
        body.append(
            f"[{ent['entity_id']}] {ent['category']} | {ent['canonical_name']} | "
            f"{ent['fact_key']} | conf={ent['confidence']:.2f} | via={ent['matched_from']}"
        )
        body.append(clip(str(ent.get("evidence") or ""), 260))
        body.append("")
    if len(payload.get("glossary_entities", [])) > 30:
        body.append(f"... ({len(payload['glossary_entities']) - 30} more entities)")
    return "\n".join(header + body).strip() + "\n"


def build_manifest_row(
    run_id: str,
    minutes_code: str,
    source_lane: str,
    source_preparse_json: Path,
    source_preparse_sha256: str,
    payload_sha256: str,
    entities_total: int,
    by_category: dict[str, int],
    output_json: Path,
    output_txt: Path,
) -> dict:
    return {
        "run_id": run_id,
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "minutes_code": minutes_code,
        "source_lane": source_lane,
        "source_preparse_json": str(source_preparse_json),
        "source_preparse_sha256": source_preparse_sha256,
        "payload_sha256": payload_sha256,
        "entities_total": entities_total,
        "people_total": int(by_category.get("PEOPLE", 0)),
        "organization_total": int(by_category.get("ORGANIZATION", 0)),
        "board_total": int(by_category.get("BOARD", 0)),
        "agency_total": int(by_category.get("AGENCY", 0)),
        "location_total": int(by_category.get("LOCATION", 0)),
        "output_json": str(output_json),
        "output_txt": str(output_txt),
    }


def run_hover(limit: int | None = None, force: bool = False, dry_run: bool = False) -> dict:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")
    cco_seed_sha256 = sha256_file(CCO_GLOSSARY_FILE) if CCO_GLOSSARY_FILE.exists() else None
    known_seed = load_cco_seed()

    state = load_state()
    state_records = state.setdefault("records", {})
    manifest_codes = load_manifest_codes()

    candidates = iter_preparse_jsons()
    discovered = len(candidates)

    prepared = 0
    skipped_unchanged = 0
    failed = 0
    zero_entity_records = 0
    run_rows: list[dict] = []
    failure_rows: list[dict] = []
    quality_rows: list[dict] = []
    run_candidate_rows: list[dict[str, Any]] = []

    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = RUNS_ROOT / run_id

    for preparse_json in candidates:
        if limit is not None and prepared >= limit:
            break

        minutes_code = preparse_json.parent.name
        try:
            source_preparse_text = preparse_json.read_text(encoding="utf-8")
            source_preparse_sha256 = sha256_text(source_preparse_text)
            preparse_payload = json.loads(source_preparse_text)
        except Exception as exc:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "minutes_code": minutes_code,
                    "source_preparse_json": str(preparse_json),
                    "error": f"invalid_preparse_json: {exc}",
                }
            )
            continue

        source_lane = str(preparse_payload.get("source_lane") or "").strip()
        output_dir = preparse_json.parent
        output_json = output_dir / f"{minutes_code}.glossary_hover.json"
        output_txt = output_dir / f"{minutes_code}.glossary_hover.txt"

        prev = state_records.get(minutes_code, {})
        if (
            not force
            and prev.get("source_preparse_sha256") == source_preparse_sha256
            and output_json.exists()
        ):
            skipped_unchanged += 1
            continue

        source_text, excerpt_count = extract_text_from_preparse(preparse_payload)
        entities = extract_glossary_entities(source_text, known_seed)

        by_category: dict[str, int] = {}
        for i, ent in enumerate(entities, start=1):
            ent["entity_id"] = f"GE{i:03d}"
            ent["canonical_name"] = clean_name(str(ent.get("canonical_name") or ""))
            ent["evidence"] = clip(str(ent.get("evidence") or ""), 500)
            ent["evidence_sha256"] = sha256_text(str(ent.get("evidence") or ""))
            ent["qa_flags"] = compute_entity_qa_flags(ent)
            cat = str(ent.get("category") or "UNKNOWN").upper()
            by_category[cat] = by_category.get(cat, 0) + 1

        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "minutes_glossary_candidates_record",
            "prepared_at": datetime.now().isoformat(timespec="seconds"),
            "hover_run_id": run_id,
            "source_lane": SOURCE_LANE,
            "jurisdiction": JURISDICTION,
            "minutes_code": minutes_code,
            "artifact_machine_code": str(preparse_payload.get("artifact_machine_code") or minutes_code),
            "linked_source_pdf_code": str(preparse_payload.get("linked_source_pdf_code") or minutes_code),
            "meeting_context": preparse_payload.get("meeting_context") or {},
            "lineage": {
                "source_preparse_json_path": str(preparse_json),
                "source_preparse_sha256": source_preparse_sha256,
                "source_preparse_schema_version": str(preparse_payload.get("schema_version") or ""),
                "source_preparse_lane": source_lane,
                "cco_root": str(CCO_ROOT),
                "cco_glossary_seed_path": str(CCO_GLOSSARY_FILE),
                "cco_glossary_seed_sha256": cco_seed_sha256,
                "cco_roster_seed_path": str(CCO_ROSTER_FILE),
            },
            "glossary_hover_summary": {
                "entities_total": len(entities),
                "entities_by_category": by_category,
                "source_text_chars": len(source_text),
                "source_excerpts_count": excerpt_count,
                "known_seed_entries": len(known_seed),
            },
            "glossary_entities": entities,
            "qa": {
                "is_candidate_output_only": True,
                "notes": "Extraction-only hover output. Separate process must handle any CCO writes.",
            },
        }

        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        payload_sha256 = sha256_text(payload_text)

        if dry_run:
            prepared += 1
            continue

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            output_json.write_text(payload_text, encoding="utf-8")
            output_txt.write_text(render_summary_text(payload), encoding="utf-8")

            prepared += 1
            if len(entities) == 0:
                zero_entity_records += 1
            row = build_manifest_row(
                run_id=run_id,
                minutes_code=minutes_code,
                source_lane=source_lane,
                source_preparse_json=preparse_json,
                source_preparse_sha256=source_preparse_sha256,
                payload_sha256=payload_sha256,
                entities_total=len(entities),
                by_category=by_category,
                output_json=output_json,
                output_txt=output_txt,
            )
            run_rows.append(row)
            quality_rows.append(
                {
                    "minutes_code": minutes_code,
                    "source_preparse_lane": source_lane,
                    "source_text_chars": len(source_text),
                    "entities_total": len(entities),
                    "entities_by_category": by_category,
                    "output_json": str(output_json),
                }
            )
            run_candidate_rows.extend(
                flatten_candidate_rows(
                    run_id=run_id,
                    prepared_at=str(payload.get("prepared_at") or ""),
                    minutes_code=minutes_code,
                    source_preparse_lane=source_lane,
                    source_preparse_json=preparse_json,
                    output_json=output_json,
                    source_preparse_sha256=source_preparse_sha256,
                    payload=payload,
                )
            )

            state_records[minutes_code] = {
                "last_run_id": run_id,
                "last_status": "prepared",
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "source_preparse_json": str(preparse_json),
                "source_preparse_sha256": source_preparse_sha256,
                "payload_sha256": payload_sha256,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
                "entities_total": len(entities),
            }

            # Crash-safe bookkeeping.
            save_state(state)
            if minutes_code not in manifest_codes:
                append_manifest_rows([row])
                manifest_codes.add(minutes_code)
        except Exception as exc:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "minutes_code": minutes_code,
                    "source_preparse_json": str(preparse_json),
                    "error": str(exc),
                }
            )

    if not dry_run:
        run_manifest = run_dir / "minutes_glossary_hover_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "minutes_glossary_hover_failures.jsonl"
            with failure_out.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "source_lane": SOURCE_LANE,
            "output_root": str(OUTPUT_ROOT),
            "discovered_records": discovered,
            "prepared_records": prepared,
            "skipped_unchanged": skipped_unchanged,
            "failed": failed,
            "zero_entity_records": zero_entity_records,
            "limit": limit,
            "force": force,
            "cco_glossary_seed_path": str(CCO_GLOSSARY_FILE),
            "cco_roster_seed_path": str(CCO_ROSTER_FILE),
            "candidate_rows_written": len(run_candidate_rows),
        }
        (run_dir / "run_summary.json").write_text(json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n")
        run_candidates_file = run_dir / "minutes_glossary_hover_candidates.jsonl"
        with run_candidates_file.open("w", encoding="utf-8") as f:
            for row in run_candidate_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")
        quality_json = {
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "source_lane": SOURCE_LANE,
            "records_written": prepared,
            "zero_entity_records": zero_entity_records,
            "candidate_rows_written": len(run_candidate_rows),
            "records": quality_rows,
        }
        (run_dir / "minutes_glossary_hover_quality.json").write_text(
            json.dumps(quality_json, ensure_ascii=True, indent=2) + "\n"
        )
        snapshot_rows = rebuild_candidates_snapshot()
        quality_json["candidate_snapshot_path"] = str(CANDIDATES_SNAPSHOT_FILE)
        quality_json["candidate_snapshot_rows"] = snapshot_rows
        (run_dir / "minutes_glossary_hover_quality.json").write_text(
            json.dumps(quality_json, ensure_ascii=True, indent=2) + "\n"
        )

        save_state(state)

    summary = {
        "run_id": run_id,
        "source_lane": SOURCE_LANE,
        "output_root": str(OUTPUT_ROOT),
        "discovered_records": discovered,
        "prepared_records": prepared,
        "skipped_unchanged": skipped_unchanged,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 60)
    print("MINUTES GLOSSARY HOVER SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Source lane: {summary['source_lane']}")
    print(f"  Output root: {summary['output_root']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Prepared records: {summary['prepared_records']}")
    print(f"  Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"  Failed: {summary['failed']}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 60)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Build glossary-hover candidate artifacts from Minutes _output records.")
    parser.add_argument("--limit", type=int, default=None, help="Process first N minutes output records.")
    parser.add_argument("--force", action="store_true", help="Rebuild hover outputs even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write outputs.")
    args = parser.parse_args()

    run_hover(limit=args.limit, force=args.force, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
