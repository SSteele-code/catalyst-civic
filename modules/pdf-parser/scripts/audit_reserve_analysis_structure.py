from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def find_first_index(lines: list[dict], pattern: str) -> int | None:
    normalized_pattern = normalize_text(pattern)
    for index, line in enumerate(lines):
        if normalized_pattern in normalize_text(line.get("text", "")):
            return index
    return None


def find_department_index(lines: list[dict]) -> int | None:
    for index, line in enumerate(lines):
        text = normalize_text(line.get("text", ""))
        if "department" in text or "fund" in text:
            return index
    return None


def audit_page(page_export: dict) -> dict | None:
    page_info = page_export.get("page") or {}
    lines = ((page_export.get("word_witness", {}) or {}).get("data") or {}).get("lines") or []
    if not any("reserve analysis" in normalize_text(line.get("text", "")) for line in lines):
        return None
    if str(page_info.get("sort_lane") or "") != "table_specialist" and str(page_info.get("page_type") or "") != "table_or_mixed_layout":
        return None

    header_indices = {
        "town_of_richlands": find_first_index(lines, "Town of Richlands"),
        "reserve_analysis": find_first_index(lines, "Reserve Analysis"),
        "department_or_fund": find_department_index(lines),
        "as_of": find_first_index(lines, "As of"),
        "restated": find_first_index(lines, "Restated"),
    }
    body_indices = {
        "actual": find_first_index(lines, "Actual"),
        "reserved_cash_balance": find_first_index(lines, "Reserved Cash Balance"),
        "unreserved_cash_balance": find_first_index(lines, "Unreserved Cash Balance"),
        "comments": find_first_index(lines, "Comments"),
    }

    header_anchor_indices = [index for index in header_indices.values() if index is not None]
    body_anchor_indices = [index for index in body_indices.values() if index is not None]
    first_header = min(header_anchor_indices) if header_anchor_indices else None
    first_body = min(body_anchor_indices) if body_anchor_indices else None
    header_before_body = (
        first_header is not None and first_body is not None and first_header < first_body
    )
    comments_after_actual = True
    if body_indices["comments"] is not None and body_indices["actual"] is not None:
        comments_after_actual = body_indices["comments"] > body_indices["actual"]

    expected_headers_present = (
        header_indices["town_of_richlands"] is not None
        and header_indices["reserve_analysis"] is not None
        and header_indices["as_of"] is not None
    )
    expected_body_present = (
        body_indices["actual"] is not None
        and body_indices["reserved_cash_balance"] is not None
        and body_indices["unreserved_cash_balance"] is not None
    )

    passed = expected_headers_present and expected_body_present and header_before_body and comments_after_actual

    route_info = page_export.get("route") or {}
    return {
        "page_id": page_info.get("page_id"),
        "source_page_number": page_info.get("source_page_number"),
        "page_type": page_info.get("page_type"),
        "review_state": page_info.get("review_state"),
        "sort_lane": page_info.get("sort_lane"),
        "line_strategy": (page_export.get("word_witness") or {}).get("line_strategy"),
        "cardinal_rotation_applied": route_info.get("cardinal_rotation_applied"),
        "geometry_normalization_source": route_info.get("geometry_normalization_source"),
        "residual_skew_angle": route_info.get("residual_skew_angle"),
        "header_indices": header_indices,
        "body_indices": body_indices,
        "header_before_body": header_before_body,
        "comments_after_actual": comments_after_actual,
        "passed": passed,
        "line_preview": [line.get("text", "") for line in lines[:18]],
    }


def load_page_exports(machine_readable_dir: Path) -> list[dict]:
    pages_dir = machine_readable_dir / "pages"
    payloads: list[dict] = []
    for page_path in sorted(pages_dir.glob("*.json")):
        payloads.append(json.loads(page_path.read_text(encoding="utf-8")))
    return payloads


def build_markdown(run_id: str, audits: list[dict]) -> str:
    passed = sum(1 for audit in audits if audit["passed"])
    lines = [
        f"# Reserve Analysis Structure Audit: {run_id}",
        "",
        f"- Pages audited: {len(audits)}",
        f"- Passed: {passed}",
        f"- Failed: {len(audits) - passed}",
        "",
    ]
    for audit in audits:
        status = "PASS" if audit["passed"] else "FAIL"
        lines.append(f"## Page {audit['source_page_number']} [{status}]")
        lines.append("")
        lines.append(f"- Page ID: `{audit['page_id']}`")
        lines.append(f"- Page type: `{audit['page_type']}`")
        lines.append(f"- Review state: `{audit['review_state']}`")
        lines.append(f"- Sort lane: `{audit['sort_lane']}`")
        lines.append(f"- Line strategy: `{audit['line_strategy']}`")
        lines.append(f"- Cardinal rotation applied: `{audit['cardinal_rotation_applied']}`")
        lines.append(f"- Geometry normalization source: `{audit['geometry_normalization_source']}`")
        lines.append(f"- Residual skew angle: `{audit['residual_skew_angle']}`")
        lines.append(f"- Header before body: `{audit['header_before_body']}`")
        lines.append(f"- Comments after Actual: `{audit['comments_after_actual']}`")
        lines.append(f"- Header indices: `{audit['header_indices']}`")
        lines.append(f"- Body indices: `{audit['body_indices']}`")
        lines.append("- Line preview:")
        for preview in audit["line_preview"]:
            lines.append(f"  - {preview}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit reserve-analysis page structure for a packaged run.")
    parser.add_argument("run_id", help="Run ID to audit, e.g. RUN_2026_04_10_85F4")
    parser.add_argument(
        "--base-dir",
        default=r"C:\Users\simon\CatalystCivic\_Modules\PDF Parser",
        help="Machine root directory",
    )
    args = parser.parse_args()

    base_dir = Path(args.base_dir)
    machine_readable_dir = base_dir / "outbox" / f"{args.run_id}_source" / "machine_readable"
    if not machine_readable_dir.exists():
        raise SystemExit(f"Machine-readable folder not found: {machine_readable_dir}")

    audits = [
        audit
        for audit in (audit_page(page_export) for page_export in load_page_exports(machine_readable_dir))
        if audit is not None
    ]
    summary = {
        "run_id": args.run_id,
        "page_count": len(audits),
        "passed_count": sum(1 for audit in audits if audit["passed"]),
        "failed_count": sum(1 for audit in audits if not audit["passed"]),
        "pages": audits,
    }

    reports_dir = base_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / f"{args.run_id}_reserve_analysis_structure_audit.json"
    md_path = reports_dir / f"{args.run_id}_reserve_analysis_structure_audit.md"
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(build_markdown(args.run_id, audits), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(md_path)
    print(json_path)


if __name__ == "__main__":
    main()
