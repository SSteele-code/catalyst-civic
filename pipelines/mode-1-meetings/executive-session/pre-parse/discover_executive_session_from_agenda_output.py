#!/usr/bin/env python
"""
Executive Session discovery pass for M1 agenda output packets.

Goal:
- identify executive-session sections directly from full agenda packet text outputs
- extract stated reason lines (e.g., personnel, legal, 2.2-3711 references)
- count candidate session occurrences for downstream PULL design
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


AGENDAS_OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output")
EXECUTIVE_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Executive_Session")
RUNS_ROOT = EXECUTIVE_ROOT / "_output" / "_runs"

TOP_LEVEL_MANIFEST = EXECUTIVE_ROOT / "M1_EXECUTIVE_SESSION_DISCOVERY_MANIFEST.jsonl"
TOP_LEVEL_SUMMARY = EXECUTIVE_ROOT / "executive_session_discovery_summary.json"

DEFAULT_MIN_PAGE_COUNT = 1
DEFAULT_MIN_TXT_BYTES = 1


PAGE_MARKER_RE = re.compile(r"^\s*---\s*page\b", re.IGNORECASE)
ROMAN_HEADING_RE = re.compile(r"^\s*[ivxlcdm]+\s*[\.\),]\s+\S+", re.IGNORECASE)
UPPER_HEADING_RE = re.compile(r"^\s*[A-Z][A-Za-z/&,\- ]{3,}:\s*$")
EXECUTIVE_HEADING_RE = re.compile(
    r"\b(executive(?:/closed)?\s+session|closed\s+session)\b",
    re.IGNORECASE,
)
ITEM_PREFIX_RE = re.compile(r"^\s*(?:[ivxlcdm]{1,6}|[a-z0-9]{1,3})\s*[\.\)\-:]\s+", re.IGNORECASE)
HEADING_START_RE = re.compile(r"^\s*(executive(?:/closed)?\s+session|closed\s+session)\b", re.IGNORECASE)
STATUTE_RE = re.compile(r"2[\.\s]*2[\-\s]*37[1l\]]{2}", re.IGNORECASE)
SPEAKER_PREFIX_RE = re.compile(
    r"^\s*[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2}\s*[-–—:]\s+\S+"
)

REASON_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("personnel", re.compile(r"\b(personnel|hiring|appointment|discipline|evaluation|compensation)\b", re.IGNORECASE)),
    ("legal_litigation", re.compile(r"\b(legal\s+counsel|litigation|attorney)\b", re.IGNORECASE)),
    ("contract_negotiation", re.compile(r"\b(contract|negotiation)\b", re.IGNORECASE)),
    ("property_real_estate", re.compile(r"\b(property|real\s+estate|land|building)\b", re.IGNORECASE)),
    ("prospective_business", re.compile(r"\b(prospective\s+business|industry|industrial|economic\s+development)\b", re.IGNORECASE)),
    ("security_emergency", re.compile(r"\b(security|emergency)\b", re.IGNORECASE)),
]

REASON_SIGNAL_RE = re.compile(
    r"(2[\.\s]*2[\-\s]*37[1l\]]{2}|"
    r"personnel|hiring|appointment|discipline|evaluation|compensation|"
    r"legal\s+counsel|litigation|attorney|"
    r"contract|negotiation|"
    r"property|real\s+estate|land|building|"
    r"prospective\s+business|industry|industrial|economic\s+development|"
    r"security|emergency)",
    re.IGNORECASE,
)

CODE_REF_RE = re.compile(
    r"2[\.\s]*2[\-\s]*37[1l\]]{2}(?:\s*[\(\[]\s*[A-Za-z0-9]+\s*[\)\]]){0,4}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SessionHit:
    agenda_code: str
    source_txt: str
    line_number: int
    heading_text: str
    reason_lines: list[dict[str, Any]]
    reason_categories: list[str]
    code_references: list[str]
    candidate_status: str


def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").replace("\t", " ")).strip()


def iter_agenda_txt_files(
    root: Path,
    min_page_count: int,
    min_txt_bytes: int,
) -> tuple[list[Path], dict[str, int]]:
    if not root.exists():
        return [], {"root_missing": 1}

    stats: Counter[str] = Counter()
    accepted: list[Path] = []

    for p in sorted(root.glob("*/*.txt")):
        if not p.is_file():
            continue
        stats["total_txt_seen"] += 1

        try:
            txt_size = int(p.stat().st_size)
        except Exception:
            stats["skip_stat_error"] += 1
            continue
        if txt_size < min_txt_bytes:
            stats["skip_small_txt"] += 1
            continue

        factsheet_path = p.parent / f"{p.stem}.factsheet.json"
        if not factsheet_path.exists():
            stats["skip_missing_factsheet"] += 1
            continue

        try:
            factsheet = json.loads(factsheet_path.read_text(encoding="utf-8"))
        except Exception:
            stats["skip_bad_factsheet"] += 1
            continue

        if bool(factsheet.get("quarantine_flag")):
            stats["skip_quarantine"] += 1
            continue

        page_count_value = factsheet.get("page_count")
        try:
            page_count = int(page_count_value)
        except Exception:
            page_count = 0
        if page_count < min_page_count:
            stats["skip_below_page_threshold"] += 1
            continue

        accepted.append(p)
        stats["accepted_full_packets"] += 1

    return accepted, dict(stats)


def is_boundary(line: str) -> bool:
    s = clean_line(line)
    if not s:
        return False
    if PAGE_MARKER_RE.search(s):
        return True
    if ROMAN_HEADING_RE.search(s):
        return True
    if UPPER_HEADING_RE.search(s):
        return True
    return False


def classify_categories(text: str) -> list[str]:
    found: list[str] = []
    for label, pattern in REASON_PATTERNS:
        if pattern.search(text):
            found.append(label)
    if not found and STATUTE_RE.search(text):
        found.append("statutory_reference")
    if not found:
        found.append("other")
    return found


def extract_code_refs(text: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for m in CODE_REF_RE.finditer(text):
        raw = clean_line(m.group(0))
        if not raw:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        refs.append(raw)
    return refs


def looks_like_executive_heading(line: str) -> bool:
    s = clean_line(line)
    if not s:
        return False
    if SPEAKER_PREFIX_RE.match(s):
        return False
    if len(s) > 220:
        return False
    if not EXECUTIVE_HEADING_RE.search(s):
        return False

    # Remove meeting-dialogue phrasing and keep agenda-heading structure.
    lower = s.lower()
    if "motion to go into" in lower:
        return False
    if "on the agenda it says" in lower:
        return False
    if "conversation was precisely" in lower:
        return False
    if "is there a motion" in lower:
        return False
    if "would be going into executive session" in lower:
        return False
    if "provided a second for the motion" in lower:
        return False
    if "all members voted" in lower:
        return False
    if "?" in s:
        return False

    words = s.split()
    if len(words) > 20 and not ITEM_PREFIX_RE.match(s):
        return False

    if ITEM_PREFIX_RE.match(s):
        return True
    if HEADING_START_RE.match(s):
        return True
    if UPPER_HEADING_RE.match(s):
        return True
    return False


def discover_for_file(txt_path: Path) -> list[SessionHit]:
    agenda_code = txt_path.parent.name
    lines = txt_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    hits: list[SessionHit] = []

    for idx, raw in enumerate(lines, start=1):
        line = clean_line(raw)
        if not looks_like_executive_heading(line):
            continue

        reason_lines: list[dict[str, Any]] = []
        category_counts: Counter[str] = Counter()
        code_refs: list[str] = []
        seen_reason_keys: set[str] = set()

        window_end = min(len(lines), idx + 24)
        for j in range(idx, window_end + 1):
            cur = clean_line(lines[j - 1])
            if not cur:
                continue
            if j > idx and is_boundary(cur):
                break
            if not REASON_SIGNAL_RE.search(cur):
                continue

            key = f"{j}|{cur.lower()}"
            if key in seen_reason_keys:
                continue
            seen_reason_keys.add(key)

            cats = classify_categories(cur)
            for c in cats:
                category_counts[c] += 1
            refs = extract_code_refs(cur)
            for r in refs:
                if r not in code_refs:
                    code_refs.append(r)

            reason_lines.append(
                {
                    "line_number": j,
                    "text": cur,
                    "categories": cats,
                    "code_references": refs,
                }
            )

        # The heading itself may be the only reason-bearing line.
        if not reason_lines and (REASON_SIGNAL_RE.search(line) or STATUTE_RE.search(line)):
            cats = classify_categories(line)
            refs = extract_code_refs(line)
            for c in cats:
                category_counts[c] += 1
            for r in refs:
                if r not in code_refs:
                    code_refs.append(r)
            reason_lines.append(
                {
                    "line_number": idx,
                    "text": line,
                    "categories": cats,
                    "code_references": refs,
                }
            )

        categories_sorted = [k for k, _ in category_counts.most_common()]
        status = "candidate" if reason_lines else "adjacent"

        hits.append(
            SessionHit(
                agenda_code=agenda_code,
                source_txt=str(txt_path),
                line_number=idx,
                heading_text=line,
                reason_lines=reason_lines,
                reason_categories=categories_sorted,
                code_references=code_refs,
                candidate_status=status,
            )
        )

    return hits


def discover(root: Path, min_page_count: int, min_txt_bytes: int) -> dict[str, Any]:
    run_id = f"RUN-ES-DISCOVERY-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    started_at = datetime.now().isoformat(timespec="seconds")
    files, source_gate_stats = iter_agenda_txt_files(
        root=root,
        min_page_count=min_page_count,
        min_txt_bytes=min_txt_bytes,
    )

    manifest_rows: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    code_ref_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    per_packet_hits: Counter[str] = Counter()

    for txt in files:
        hits = discover_for_file(txt)
        for hit in hits:
            status_counts[hit.candidate_status] += 1
            per_packet_hits[hit.agenda_code] += 1
            for c in hit.reason_categories:
                category_counts[c] += 1
            for r in hit.code_references:
                code_ref_counts[r] += 1

            manifest_rows.append(
                {
                    "run_id": run_id,
                    "agenda_code": hit.agenda_code,
                    "source_txt": hit.source_txt,
                    "candidate_status": hit.candidate_status,
                    "heading_line_number": hit.line_number,
                    "heading_text": hit.heading_text,
                    "reason_line_count": len(hit.reason_lines),
                    "reason_categories": hit.reason_categories,
                    "code_references": hit.code_references,
                    "reason_lines": hit.reason_lines[:20],
                }
            )

    packets_with_signals = len(per_packet_hits)

    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "discovery_schema_version": "m1.executive_session.discovery.v1",
        "agendas_output_root": str(root),
        "input_agenda_txt_files": len(files),
        "source_gate": {
            "min_page_count": min_page_count,
            "min_txt_bytes": min_txt_bytes,
            "stats": source_gate_stats,
        },
        "records_with_executive_signals": len(manifest_rows),
        "packets_with_executive_signals": packets_with_signals,
        "candidate_records": status_counts.get("candidate", 0),
        "adjacent_records": status_counts.get("adjacent", 0),
        "status_counts": dict(status_counts),
        "top_reason_categories": [{"category": k, "count": v} for k, v in category_counts.most_common(20)],
        "top_code_references": [{"code_reference": k, "count": v} for k, v in code_ref_counts.most_common(25)],
    }
    return {"summary": summary, "manifest_rows": manifest_rows}


def write_outputs(summary: dict[str, Any], manifest_rows: list[dict[str, Any]], dry_run: bool) -> None:
    run_id = summary["run_id"]
    run_dir = RUNS_ROOT / run_id
    run_manifest = run_dir / "executive_session_discovery_manifest.jsonl"
    run_summary = run_dir / "executive_session_discovery_summary.json"

    if dry_run:
        print("EXECUTIVE SESSION DISCOVERY DRY RUN")
        print(f"  Run ID: {run_id}")
        print(f"  Input agenda txt files: {summary['input_agenda_txt_files']}")
        print(f"  Source gate: {summary.get('source_gate')}")
        print(f"  Records with executive signals: {summary['records_with_executive_signals']}")
        print(f"  Packets with executive signals: {summary['packets_with_executive_signals']}")
        print(f"  Candidate records: {summary['candidate_records']}")
        print(f"  Adjacent records: {summary['adjacent_records']}")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    EXECUTIVE_ROOT.mkdir(parents=True, exist_ok=True)

    with TOP_LEVEL_MANIFEST.open("a", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    with run_manifest.open("w", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    TOP_LEVEL_SUMMARY.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    run_summary.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    print("EXECUTIVE SESSION DISCOVERY COMPLETE")
    print(f"  Run ID: {run_id}")
    print(f"  Input agenda txt files: {summary['input_agenda_txt_files']}")
    print(f"  Source gate: {summary.get('source_gate')}")
    print(f"  Records with executive signals: {summary['records_with_executive_signals']}")
    print(f"  Packets with executive signals: {summary['packets_with_executive_signals']}")
    print(f"  Candidate records: {summary['candidate_records']}")
    print(f"  Adjacent records: {summary['adjacent_records']}")
    print(f"  Top-level manifest: {TOP_LEVEL_MANIFEST}")
    print(f"  Top-level summary: {TOP_LEVEL_SUMMARY}")
    print(f"  Run manifest: {run_manifest}")
    print(f"  Run summary: {run_summary}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover Executive Session candidates and reason lines from M1 agenda output text files."
    )
    parser.add_argument(
        "--agendas-output-root",
        default=str(AGENDAS_OUTPUT_ROOT),
        help="Path to agenda _output root containing <agenda_code>/<agenda_code>.txt files.",
    )
    parser.add_argument(
        "--min-page-count",
        type=int,
        default=DEFAULT_MIN_PAGE_COUNT,
        help=f"Require factsheet page_count >= this value (default: {DEFAULT_MIN_PAGE_COUNT}).",
    )
    parser.add_argument(
        "--min-txt-bytes",
        type=int,
        default=DEFAULT_MIN_TXT_BYTES,
        help=f"Require txt file byte size >= this value (default: {DEFAULT_MIN_TXT_BYTES}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print summary without writing manifest/summary files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = discover(
        root=Path(args.agendas_output_root),
        min_page_count=int(args.min_page_count),
        min_txt_bytes=int(args.min_txt_bytes),
    )
    write_outputs(payload["summary"], payload["manifest_rows"], dry_run=bool(args.dry_run))


if __name__ == "__main__":
    main()
