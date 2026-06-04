import os
#!/usr/bin/env python
"""
Department Reports discovery pass for M1 agenda outputs.

Goal:
- identify what "department report" looks like in real agenda artifacts
- count how many agenda records appear to contain department-report sections
- emit a manifest for downstream PULL/PRE_PARSE implementation
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


AGENDAS_OUTPUT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas" / "_output"
DEPARTMENT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "DepartmentReports"
RUNS_ROOT = DEPARTMENT_ROOT / "_output" / "_runs"

TOP_LEVEL_MANIFEST = DEPARTMENT_ROOT / "M1_DEPARTMENT_REPORTS_DISCOVERY_MANIFEST.jsonl"
TOP_LEVEL_SUMMARY = DEPARTMENT_ROOT / "department_reports_discovery_summary.json"


ROMAN_NUMERAL_PREFIX_RE = re.compile(r"^\s*[IVXLCDM]+\s*[\.\)]\s*", re.IGNORECASE)
LETTER_BULLET_RE = re.compile(r"^\s*[a-z]\s*[\.\)]\s*", re.IGNORECASE)
IN_RE_PREFIX_RE = re.compile(r"^\s*IN\s+RE\s*:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class PatternRule:
    label: str
    regex: re.Pattern[str]
    score: float
    category: str


POSITIVE_RULES: list[PatternRule] = [
    PatternRule(
        label="TOWN_MANAGER_REPORT",
        regex=re.compile(r"\btown\s+manager\s+reports?\b", re.IGNORECASE),
        score=1.00,
        category="department_reports",
    ),
    PatternRule(
        label="DEPARTMENT_HEAD_REPORTS",
        regex=re.compile(r"\bdepartment\s+head\s+reports?\b", re.IGNORECASE),
        score=0.97,
        category="department_reports",
    ),
    PatternRule(
        label="MANAGER_REPORTS_NOTES",
        regex=re.compile(r"manager.?s\s+reports?\s+and\s+notes", re.IGNORECASE),
        score=0.94,
        category="department_reports",
    ),
    PatternRule(
        label="DEPARTMENT_REPORTS",
        regex=re.compile(r"\bdepartment\s+reports?\b", re.IGNORECASE),
        score=0.92,
        category="department_reports",
    ),
    PatternRule(
        label="FINANCE_MANAGER_REPORT",
        regex=re.compile(r"\bfinance\s+manager\s+report\b", re.IGNORECASE),
        score=0.85,
        category="department_reports",
    ),
    PatternRule(
        label="MONTHLY_FINANCIAL_REPORT",
        regex=re.compile(r"\bmonthly\s+financial\s+reports?\b", re.IGNORECASE),
        score=0.75,
        category="adjacent_reports",
    ),
]


NEGATIVE_RULES: list[PatternRule] = [
    PatternRule(
        label="COUNCIL_MEMBER_REPORTS",
        regex=re.compile(r"\bcouncil\s+member\s+reports?\b", re.IGNORECASE),
        score=0.0,
        category="exclude",
    ),
    PatternRule(
        label="ATTORNEY_REPORT",
        regex=re.compile(r"\battorney(?:'s)?\s+(?:comments|report)\b", re.IGNORECASE),
        score=0.0,
        category="exclude",
    ),
    PatternRule(
        label="MAYOR_REPORT",
        regex=re.compile(r"\bmayor(?:'s)?\s+comments\b", re.IGNORECASE),
        score=0.0,
        category="exclude",
    ),
]


def is_heading_like(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if len(s) > 180:
        return False
    if ROMAN_NUMERAL_PREFIX_RE.match(s):
        return True
    if LETTER_BULLET_RE.match(s):
        return True
    if IN_RE_PREFIX_RE.match(s):
        return True
    # Common heading style in OCR output.
    if s.lower().endswith("report") or s.lower().endswith("reports") or s.lower().endswith("report:"):
        return True
    return False


def clean_line(line: str) -> str:
    s = line.replace("\t", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def iter_agenda_txt_files(agendas_output_root: Path) -> list[Path]:
    if not agendas_output_root.exists():
        return []
    return sorted(p for p in agendas_output_root.glob("*/*.txt") if p.is_file())


def detect_rules(line: str, rules: list[PatternRule]) -> list[PatternRule]:
    out: list[PatternRule] = []
    for rule in rules:
        if rule.regex.search(line):
            out.append(rule)
    return out


def discover(agendas_output_root: Path) -> dict[str, Any]:
    run_id = f"RUN-DR-DISCOVERY-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    started_at = datetime.now().isoformat(timespec="seconds")

    files = iter_agenda_txt_files(agendas_output_root)

    manifest_rows: list[dict[str, Any]] = []
    positive_label_counts: Counter[str] = Counter()
    heading_counts: Counter[str] = Counter()
    category_file_sets: defaultdict[str, set[str]] = defaultdict(set)

    for txt_path in files:
        agenda_code = txt_path.parent.name
        text = txt_path.read_text(encoding="utf-8", errors="ignore")

        evidences: list[dict[str, Any]] = []
        file_positive_labels: set[str] = set()
        file_heading_positive_labels: set[str] = set()
        file_negative_labels: set[str] = set()

        for i, raw_line in enumerate(text.splitlines(), start=1):
            line = clean_line(raw_line)
            if not line:
                continue
            if "report" not in line.lower():
                continue

            pos_hits = detect_rules(line, POSITIVE_RULES)
            neg_hits = detect_rules(line, NEGATIVE_RULES)
            if not pos_hits and not neg_hits:
                continue

            heading_like = is_heading_like(line)
            if not heading_like and pos_hits:
                # Keep strong positive non-heading lines as weak evidence only.
                strong_positive = any(r.score >= 0.95 for r in pos_hits)
                if not strong_positive:
                    continue

            for hit in pos_hits:
                file_positive_labels.add(hit.label)
                positive_label_counts[hit.label] += 1
                category_file_sets[hit.category].add(agenda_code)
                if heading_like:
                    file_heading_positive_labels.add(hit.label)
            for hit in neg_hits:
                file_negative_labels.add(hit.label)

            if heading_like:
                heading_counts[line] += 1

            evidences.append(
                {
                    "line_number": i,
                    "line_text": line,
                    "heading_like": heading_like,
                    "positive_labels": sorted(r.label for r in pos_hits),
                    "negative_labels": sorted(r.label for r in neg_hits),
                }
            )

        if not evidences:
            continue

        # Candidate rule:
        # - at least one department-report positive label
        # - not only excluded report types
        dept_labels = {
            "TOWN_MANAGER_REPORT",
            "DEPARTMENT_HEAD_REPORTS",
            "MANAGER_REPORTS_NOTES",
            "DEPARTMENT_REPORTS",
            "FINANCE_MANAGER_REPORT",
        }
        has_department_heading_label = any(label in dept_labels for label in file_heading_positive_labels)
        has_department_non_heading_label = any(label in dept_labels for label in file_positive_labels)
        has_exclude_label = any(
            label in {"COUNCIL_MEMBER_REPORTS", "ATTORNEY_REPORT", "MAYOR_REPORT"} for label in file_negative_labels
        )

        if has_department_heading_label:
            candidate_status = "candidate"
        elif has_department_non_heading_label:
            candidate_status = "adjacent"
        elif has_exclude_label:
            candidate_status = "excluded"
        else:
            candidate_status = "adjacent"

        primary_label: str | None = None
        primary_score = 0.0
        primary_source_labels = file_heading_positive_labels or file_positive_labels
        for rule in POSITIVE_RULES:
            if rule.label in primary_source_labels and rule.score > primary_score:
                primary_label = rule.label
                primary_score = rule.score

        candidate_evidence = [
            ev
            for ev in evidences
            if ev.get("heading_like")
            and any(label in dept_labels for label in ev.get("positive_labels", []))
        ]
        if not candidate_evidence:
            candidate_evidence = [
                ev for ev in evidences if any(label in dept_labels for label in ev.get("positive_labels", []))
            ]

        row = {
            "run_id": run_id,
            "agenda_code": agenda_code,
            "source_txt": str(txt_path),
            "candidate_status": candidate_status,
            "primary_label": primary_label,
            "primary_score": round(primary_score, 3),
            "positive_labels": sorted(file_positive_labels),
            "heading_positive_labels": sorted(file_heading_positive_labels),
            "negative_labels": sorted(file_negative_labels),
            "evidence_count": len(evidences),
            "candidate_evidence_count": len(candidate_evidence),
            "candidate_evidence": candidate_evidence[:25],
            "evidence": evidences[:25],  # keep manifest compact but useful
        }
        manifest_rows.append(row)

    status_counts: Counter[str] = Counter(row["candidate_status"] for row in manifest_rows)

    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "agendas_output_root": str(agendas_output_root),
        "discovery_schema_version": "m1.department_reports.discovery.v1",
        "input_agenda_txt_files": len(files),
        "records_with_report_signals": len(manifest_rows),
        "candidate_records": status_counts.get("candidate", 0),
        "adjacent_records": status_counts.get("adjacent", 0),
        "excluded_records": status_counts.get("excluded", 0),
        "status_counts": dict(status_counts),
        "positive_label_counts": dict(positive_label_counts),
        "distinct_files_by_category": {
            category: len(file_codes) for category, file_codes in sorted(category_file_sets.items())
        },
        "top_heading_lines": [
            {"line": line, "count": count}
            for line, count in heading_counts.most_common(50)
        ],
    }

    return {"summary": summary, "manifest_rows": manifest_rows}


def write_outputs(summary: dict[str, Any], manifest_rows: list[dict[str, Any]], dry_run: bool) -> None:
    run_id = summary["run_id"]
    run_dir = RUNS_ROOT / run_id
    run_manifest = run_dir / "department_reports_discovery_manifest.jsonl"
    run_summary = run_dir / "department_reports_discovery_summary.json"

    if dry_run:
        print("DR DISCOVERY DRY RUN")
        print(f"  Run ID: {run_id}")
        print(f"  Input agenda txt files: {summary['input_agenda_txt_files']}")
        print(f"  Records with report signals: {summary['records_with_report_signals']}")
        print(f"  Candidate records: {summary['candidate_records']}")
        print(f"  Adjacent records: {summary['adjacent_records']}")
        print(f"  Excluded records: {summary['excluded_records']}")
        return

    run_dir.mkdir(parents=True, exist_ok=True)
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)

    with TOP_LEVEL_MANIFEST.open("a", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    with run_manifest.open("w", encoding="utf-8") as f:
        for row in manifest_rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")

    TOP_LEVEL_SUMMARY.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    run_summary.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    print("DR DISCOVERY COMPLETE")
    print(f"  Run ID: {run_id}")
    print(f"  Input agenda txt files: {summary['input_agenda_txt_files']}")
    print(f"  Records with report signals: {summary['records_with_report_signals']}")
    print(f"  Candidate records: {summary['candidate_records']}")
    print(f"  Adjacent records: {summary['adjacent_records']}")
    print(f"  Excluded records: {summary['excluded_records']}")
    print(f"  Top-level manifest: {TOP_LEVEL_MANIFEST}")
    print(f"  Top-level summary: {TOP_LEVEL_SUMMARY}")
    print(f"  Run manifest: {run_manifest}")
    print(f"  Run summary: {run_summary}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover DepartmentReports candidates from M1 agenda output text files."
    )
    parser.add_argument(
        "--agendas-output-root",
        default=str(AGENDAS_OUTPUT_ROOT),
        help="Path to agenda _output root containing <agenda_code>/<agenda_code>.txt files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print summary without writing manifest/summary files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    agendas_output_root = Path(args.agendas_output_root)
    payload = discover(agendas_output_root=agendas_output_root)
    write_outputs(payload["summary"], payload["manifest_rows"], dry_run=bool(args.dry_run))


if __name__ == "__main__":
    main()
