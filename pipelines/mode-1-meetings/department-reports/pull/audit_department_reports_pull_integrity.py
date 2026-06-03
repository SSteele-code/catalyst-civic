#!/usr/bin/env python
"""
Department Reports Pull: External Integrity Audit

Independent audit for staged DepartmentReports pull artifacts.
This gate validates source traceability and section hygiene directly from
staged JSON + source TXT files, without trusting pull-time integrity fields.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


DEPARTMENT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports")
STAGING_ROOT = DEPARTMENT_ROOT / "_staging"

EXCLUDED_HEADING_RE = re.compile(
    r"^\s*(?:[ivxlcdm]+\s*[\.\),]\s*)?"
    r"(?:council\s+members?\s+reports?(?:\s*\(.*\))?|"
    r"attorney(?:.?s)?\s+(?:report|comments?)|"
    r"mayor.?s\s+comments?)\s*:?\s*$",
    re.IGNORECASE,
)


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def latest_run_dir(staging_root: Path) -> Path | None:
    if not staging_root.exists():
        return None
    run_dirs = [p for p in staging_root.iterdir() if p.is_dir() and p.name.upper().startswith("RUN_")]
    if not run_dirs:
        return None
    run_dirs.sort(key=lambda p: p.name, reverse=True)
    return run_dirs[0]


def iter_stage_json_files(run_dir: Path) -> list[Path]:
    return sorted(p for p in run_dir.glob("*.department_reports.json") if p.is_file())


def audit_run(run_dir: Path, threshold: float) -> dict:
    records = iter_stage_json_files(run_dir)
    summary: dict = {
        "schema_version": "m1.department_reports.pull.external_audit.v1",
        "audited_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "documents_total": 0,
        "documents_pass": 0,
        "documents_fail": 0,
        "excerpt_total": 0,
        "checks": {
            "missing_source": 0,
            "invalid_stage_json": 0,
            "zero_excerpts": 0,
            "bad_line_range": 0,
            "excerpt_text_mismatch": 0,
            "anchor_out_of_range": 0,
            "anchor_text_mismatch": 0,
            "contaminated_heading": 0,
        },
        "failures": [],
        "integrity_threshold": threshold,
    }

    for stage_json in records:
        summary["documents_total"] += 1
        doc_failures: set[str] = set()

        try:
            payload = json.loads(stage_json.read_text(encoding="utf-8"))
        except Exception as exc:
            summary["checks"]["invalid_stage_json"] += 1
            summary["documents_fail"] += 1
            summary["failures"].append(
                {
                    "stage_json": str(stage_json),
                    "machine_code": None,
                    "failure_types": ["invalid_stage_json"],
                    "error": str(exc),
                }
            )
            continue

        source_txt = Path(str(payload.get("source_txt") or ""))
        excerpts = payload.get("excerpts")
        if not isinstance(excerpts, list):
            excerpts = []
        summary["excerpt_total"] += len(excerpts)

        if not excerpts:
            summary["checks"]["zero_excerpts"] += 1
            doc_failures.add("zero_excerpts")

        lines: list[str] = []
        if source_txt.exists():
            lines = source_txt.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            summary["checks"]["missing_source"] += 1
            doc_failures.add("missing_source")

        for ex in excerpts:
            start_line = int(ex.get("start_line") or 0)
            end_line = int(ex.get("end_line") or 0)
            anchor_line = int(ex.get("anchor_line") or 0)
            ex_text = str(ex.get("text") or "")
            anchor_text = str(ex.get("anchor_text") or "")

            if lines:
                if start_line < 1 or end_line < start_line or end_line > len(lines):
                    summary["checks"]["bad_line_range"] += 1
                    doc_failures.add("bad_line_range")
                    continue

                source_block = "\n".join(ln.rstrip() for ln in lines[start_line - 1 : end_line]).strip()
                if source_block != ex_text.strip():
                    summary["checks"]["excerpt_text_mismatch"] += 1
                    doc_failures.add("excerpt_text_mismatch")

                if not (start_line <= anchor_line <= end_line):
                    summary["checks"]["anchor_out_of_range"] += 1
                    doc_failures.add("anchor_out_of_range")
                elif normalize(anchor_text) and normalize(anchor_text) not in normalize(lines[anchor_line - 1]):
                    summary["checks"]["anchor_text_mismatch"] += 1
                    doc_failures.add("anchor_text_mismatch")

            for line in ex_text.splitlines():
                if EXCLUDED_HEADING_RE.search(line or ""):
                    summary["checks"]["contaminated_heading"] += 1
                    doc_failures.add("contaminated_heading")
                    break

        if doc_failures:
            summary["documents_fail"] += 1
            summary["failures"].append(
                {
                    "stage_json": str(stage_json),
                    "machine_code": payload.get("machine_code"),
                    "failure_types": sorted(doc_failures),
                    "excerpt_count": len(excerpts),
                }
            )
        else:
            summary["documents_pass"] += 1

    total = int(summary["documents_total"])
    passed = int(summary["documents_pass"])
    rate = (passed / total) if total else 0.0
    summary["external_document_integrity_rate"] = round(rate, 4)
    summary["integrity_gate_pass"] = bool(rate >= threshold)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run independent integrity audit on DepartmentReports pull staged artifacts."
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Specific staging run dir to audit. Default: latest run under DepartmentReports/_staging.",
    )
    parser.add_argument(
        "--integrity-threshold",
        type=float,
        default=0.95,
        help="Minimum required external document integrity rate (default 0.95).",
    )
    parser.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="Path for audit report JSON. Default: <run_dir>/external_integrity_audit.json",
    )
    parser.add_argument(
        "--no-enforce-integrity",
        action="store_true",
        help="Do not fail exit code when rate is below threshold.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        run_dir = latest_run_dir(STAGING_ROOT)
        if run_dir is None:
            print("No run directory found under DepartmentReports/_staging.")
            return 1

    if not run_dir.exists() or not run_dir.is_dir():
        print(f"Run directory not found: {run_dir}")
        return 1

    summary = audit_run(run_dir=run_dir, threshold=float(args.integrity_threshold))
    json_out = Path(args.json_out) if args.json_out else (run_dir / "external_integrity_audit.json")
    json_out.write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    print("=" * 64)
    print("DEPARTMENT REPORTS EXTERNAL INTEGRITY AUDIT")
    print(f"  Run dir: {run_dir}")
    print(f"  Documents: {summary['documents_total']}")
    print(f"  Documents pass: {summary['documents_pass']}")
    print(f"  Documents fail: {summary['documents_fail']}")
    print(f"  Excerpts: {summary['excerpt_total']}")
    print(f"  Threshold: {summary['integrity_threshold']:.2f}")
    print(f"  External integrity rate: {summary['external_document_integrity_rate']:.4f}")
    print(f"  Integrity gate pass: {summary['integrity_gate_pass']}")
    print(f"  Audit report: {json_out}")
    print("=" * 64)

    if not args.no_enforce_integrity and not bool(summary["integrity_gate_pass"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
