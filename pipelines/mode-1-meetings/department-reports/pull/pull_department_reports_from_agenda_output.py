#!/usr/bin/env python
"""
Department Reports Pull (Agenda Output Pass)

Stages department-report material out of existing Agenda parser outputs into:
  _Sources/M1-Meetings/DepartmentReports/_staging/<RUN_ID>/

Strict invariant:
  - Pull only (copy/stage relevant material)
  - No DB writes
  - No downstream parsing/enrichment
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


AGENDA_OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Agendas\_output")
DEPARTMENT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\DepartmentReports")
SOURCE_ROOT = DEPARTMENT_ROOT / "_sources"
STAGING_ROOT = DEPARTMENT_ROOT / "_staging"

DISCOVERY_MANIFEST_FILE = DEPARTMENT_ROOT / "M1_DEPARTMENT_REPORTS_DISCOVERY_MANIFEST.jsonl"
STATE_FILE = DEPARTMENT_ROOT / "department_reports_output_pull_state.json"
MANIFEST_FILE = DEPARTMENT_ROOT / "M1_DEPARTMENT_REPORTS_OUTPUT_PULL_MANIFEST.jsonl"

SOURCE_LANE = "agenda_output_department_reports_sections"
SCHEMA_VERSION = "m1.department_reports.pull.v1"
EXTERNAL_AUDIT_SCHEMA_VERSION = "m1.department_reports.pull.external_audit.v1"

DEPARTMENT_LABELS = {
    "TOWN_MANAGER_REPORT",
    "DEPARTMENT_HEAD_REPORTS",
    "MANAGER_REPORTS_NOTES",
    "DEPARTMENT_REPORTS",
    "FINANCE_MANAGER_REPORT",
}

BOUNDARY_PAGE_RE = re.compile(r"^\s*---\s*page\b", re.IGNORECASE)
BOUNDARY_ROMAN_RE = re.compile(r"^\s*[ivxlcdm]+\s*[\.\),]\s+\S+", re.IGNORECASE)
BOUNDARY_HEADING_RE = re.compile(r"^\s*[A-Z][A-Za-z/&,\- ]{3,}:\s*$")
BOUNDARY_PUBLIC_COMMENT_RE = re.compile(r"\b(?:un)?scheduled\s+public\s+comments?\b", re.IGNORECASE)
BOUNDARY_IN_RE_RE = re.compile(r"^\s*IN\s+RE\s*:\s+\S+", re.IGNORECASE)
BOUNDARY_REPORT_HEADING_RE = re.compile(
    r"^\s*(?:[ivxlcdm]+\s*[\.\),]\s*)?"
    r"(?:town\s+manager\s+report|"
    r"department\s+head\s+reports?|"
    r"manager.?s\s+reports?\s+and\s+notes|"
    r"council\s+members?\s+reports?(?:\s*\(.*\))?|"
    r"attorney(?:.?s)?\s+(?:report|comments?)|"
    r"mayor.?s\s+comments?)"
    r"\s*:?\s*$",
    re.IGNORECASE,
)
BAD_HEADING_RE = re.compile(
    r"\b(council\s+members?\s+reports?|attorney(?:.?s)?\s+(?:report|comments?)|mayor.?s\s+comments?)\b",
    re.IGNORECASE,
)
IN_RE_PREFIX_RE = re.compile(r"^\s*IN\s+RE\s*:", re.IGNORECASE)
LETTER_BULLET_RE = re.compile(r"^\s*[a-z]\s*[\.\)]\s+\S+", re.IGNORECASE)


@dataclass(frozen=True)
class Excerpt:
    kind: str
    label: str
    start_line: int
    end_line: int
    text: str
    anchor_line: int
    anchor_text: str
    signals: dict[str, Any]

    @property
    def key(self) -> str:
        payload = (
            f"{self.kind}|{self.label}|{self.start_line}|{self.end_line}|"
            f"{self.anchor_line}|{self.text}"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_line(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sources": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sources": {}}


def save_state(state: dict) -> None:
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def iter_agenda_output_texts(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    out: list[Path] = []
    for packet_dir in sorted(root.iterdir()):
        if not packet_dir.is_dir():
            continue
        txt_path = packet_dir / f"{packet_dir.name}.txt"
        if txt_path.exists():
            out.append(txt_path)
    return out


def load_discovery_rows(include_adjacent: bool = False) -> list[dict[str, Any]]:
    if not DISCOVERY_MANIFEST_FILE.exists():
        return []

    allowed = {"candidate"}
    if include_adjacent:
        allowed.add("adjacent")

    latest_by_agenda: dict[str, dict[str, Any]] = {}
    for raw in DISCOVERY_MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        agenda_code = str(row.get("agenda_code") or "").strip()
        if not agenda_code:
            continue
        latest_by_agenda[agenda_code] = row

    out: list[dict[str, Any]] = []
    for agenda_code in sorted(latest_by_agenda):
        row = latest_by_agenda[agenda_code]
        status = str(row.get("candidate_status") or "").strip().lower()
        if status in allowed:
            out.append(row)
    return out


def is_section_boundary(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if BOUNDARY_PAGE_RE.search(s):
        return True
    if BOUNDARY_ROMAN_RE.search(s):
        return True
    if BOUNDARY_HEADING_RE.search(s):
        return True
    if BOUNDARY_PUBLIC_COMMENT_RE.search(s):
        return True
    if BOUNDARY_IN_RE_RE.search(s):
        return True
    if BOUNDARY_REPORT_HEADING_RE.search(s):
        return True
    return False


def extract_report_block(lines: Sequence[str], anchor_line: int, max_forward: int = 80) -> tuple[int, int, str]:
    if not lines:
        return 1, 1, ""

    start_idx = max(0, min(len(lines) - 1, anchor_line - 1))
    if start_idx > 0 and IN_RE_PREFIX_RE.search(lines[start_idx]):
        prev = lines[start_idx - 1]
        if BOUNDARY_ROMAN_RE.search(prev) or LETTER_BULLET_RE.search(prev):
            start_idx -= 1

    collected: list[str] = []
    end_idx = start_idx
    blank_streak = 0
    limit = min(len(lines), start_idx + max_forward + 1)

    for idx in range(start_idx, limit):
        line = lines[idx]
        if idx > start_idx and is_section_boundary(line):
            break
        collected.append(line.rstrip())
        end_idx = idx

        if not line.strip():
            blank_streak += 1
            if blank_streak >= 2 and idx > start_idx + 2:
                break
        else:
            blank_streak = 0

    while collected and not collected[-1].strip():
        collected.pop()
        end_idx -= 1

    text = "\n".join(collected).strip()
    return start_idx + 1, end_idx + 1, text


def pick_candidate_evidence(row: dict[str, Any]) -> list[dict[str, Any]]:
    ev = row.get("candidate_evidence")
    if not isinstance(ev, list):
        ev = row.get("evidence")
    if not isinstance(ev, list):
        return []

    out: list[dict[str, Any]] = []
    for item in ev:
        if not isinstance(item, dict):
            continue
        labels = item.get("positive_labels")
        if not isinstance(labels, list):
            labels = []
        labels = [str(x).strip() for x in labels if str(x).strip()]
        has_department_label = any(label in DEPARTMENT_LABELS for label in labels)
        if not has_department_label:
            continue

        line_no = item.get("line_number")
        try:
            line_no_int = int(line_no)
        except Exception:
            continue
        if line_no_int < 1:
            continue

        out.append(
            {
                "line_number": line_no_int,
                "line_text": str(item.get("line_text") or "").strip(),
                "heading_like": bool(item.get("heading_like")),
                "positive_labels": labels,
            }
        )

    heading_hits = [d for d in out if d.get("heading_like")]
    if heading_hits:
        return heading_hits
    return out


def build_excerpts_from_row(row: dict[str, Any], lines: Sequence[str]) -> tuple[list[Excerpt], dict[str, Any]]:
    candidate_evidence = pick_candidate_evidence(row)
    excerpts: list[Excerpt] = []
    seen_keys: set[str] = set()

    anchor_total = len(candidate_evidence)
    anchor_line_matches = 0
    contaminated_excerpt_count = 0

    for ev in candidate_evidence:
        line_no = int(ev["line_number"])
        if line_no <= len(lines):
            src_line = normalize_line(lines[line_no - 1])
            expected = normalize_line(str(ev.get("line_text") or ""))
            if expected and (expected.lower() == src_line.lower() or expected.lower() in src_line.lower()):
                anchor_line_matches += 1

        start_line, end_line, block = extract_report_block(lines, line_no)
        if not block:
            continue

        labels = ev.get("positive_labels") or []
        primary_label = labels[0] if labels else "DEPARTMENT_REPORT"
        ex = Excerpt(
            kind="department_report_section",
            label=primary_label,
            start_line=start_line,
            end_line=end_line,
            text=block,
            anchor_line=line_no,
            anchor_text=str(ev.get("line_text") or ""),
            signals={
                "match_strength": "discovery_anchor",
                "positive_labels": labels,
            },
        )
        if ex.key in seen_keys:
            continue
        seen_keys.add(ex.key)
        first_lines = "\n".join(ex.text.splitlines()[:16])
        if BAD_HEADING_RE.search(first_lines):
            contaminated_excerpt_count += 1
        excerpts.append(ex)

    excerpts.sort(key=lambda e: (e.start_line, e.end_line, e.label))

    nonempty_excerpt_count = sum(1 for e in excerpts if e.text.strip())
    anchor_ratio = (anchor_line_matches / anchor_total) if anchor_total else 0.0
    excerpt_ratio = min(1.0, nonempty_excerpt_count / anchor_total) if anchor_total else 0.0
    boundary_clean_ratio = 1.0
    if excerpts:
        boundary_clean_ratio = 1.0 - (contaminated_excerpt_count / len(excerpts))

    # Integrity focuses on traceability to source anchors and clean section boundaries.
    integrity_score = (0.75 * anchor_ratio) + (0.15 * excerpt_ratio) + (0.10 * boundary_clean_ratio)

    metrics = {
        "anchor_total": anchor_total,
        "anchor_line_matches": anchor_line_matches,
        "anchor_line_match_ratio": round(anchor_ratio, 4),
        "excerpt_count": len(excerpts),
        "nonempty_excerpt_count": nonempty_excerpt_count,
        "excerpt_coverage_ratio": round(excerpt_ratio, 4),
        "contaminated_excerpt_count": contaminated_excerpt_count,
        "boundary_clean_ratio": round(boundary_clean_ratio, 4),
        "integrity_score": round(integrity_score, 4),
    }
    return excerpts, metrics


def render_source_bundle_txt(source_txt: Path, excerpts: Sequence[Excerpt]) -> str:
    header = [
        f"SOURCE: {source_txt}",
        f"EXCERPTS: {len(excerpts)}",
        "",
    ]
    body: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        body.append(f"[{i:03d}] {ex.label} lines {ex.start_line}-{ex.end_line} (anchor {ex.anchor_line})")
        body.append(ex.text)
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def write_source_bundle(
    source_txt: Path,
    agenda_code: str,
    excerpts: Sequence[Excerpt],
    dry_run: bool,
) -> tuple[Path, Path | None, int]:
    packet_dir = SOURCE_ROOT / agenda_code
    report_txt = packet_dir / f"{agenda_code}.department_reports.txt"

    factsheet_src = source_txt.parent / f"{agenda_code}.factsheet.json"
    factsheet_dst = packet_dir / f"{agenda_code}.factsheet.json"
    has_factsheet = factsheet_src.exists()

    planned_files = 2 if has_factsheet else 1
    if dry_run:
        return report_txt, (factsheet_dst if has_factsheet else None), planned_files

    SOURCE_ROOT.mkdir(parents=True, exist_ok=True)
    if packet_dir.exists():
        shutil.rmtree(packet_dir)
    packet_dir.mkdir(parents=True, exist_ok=True)

    report_txt.write_text(render_source_bundle_txt(source_txt, excerpts), encoding="utf-8")
    written_files = 1

    factsheet_path: Path | None = None
    if has_factsheet:
        shutil.copy2(factsheet_src, factsheet_dst)
        factsheet_path = factsheet_dst
        written_files += 1

    return report_txt, factsheet_path, written_files


def render_staging_summary(source_txt: Path, row: dict[str, Any], excerpts: Sequence[Excerpt], metrics: dict[str, Any]) -> str:
    header = [
        f"SOURCE: {source_txt}",
        f"AGENDA_CODE: {row.get('agenda_code')}",
        f"DISCOVERY_STATUS: {row.get('candidate_status')}",
        f"PRIMARY_LABEL: {row.get('primary_label')}",
        f"EXCERPTS: {len(excerpts)}",
        f"INTEGRITY_SCORE: {metrics.get('integrity_score')}",
        "",
    ]
    body: list[str] = []
    for i, ex in enumerate(excerpts, start=1):
        body.append(f"[{i:03d}] {ex.label} lines {ex.start_line}-{ex.end_line}")
        body.append(ex.text)
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def run_external_integrity_audit(
    staging_dir: Path,
    integrity_threshold: float,
    enforce_integrity: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """
    Run independent post-pull audit over staged JSON files.

    This audit does not trust pull-time integrity fields and revalidates staged
    outputs against source TXT line ranges and heading contamination rules.
    """
    audit_script = Path(__file__).resolve().parent / "audit_department_reports_pull_integrity.py"
    audit_report = staging_dir / "external_integrity_audit.json"

    if not audit_script.exists():
        return (
            {
                "schema_version": EXTERNAL_AUDIT_SCHEMA_VERSION,
                "audited_at": datetime.now().isoformat(timespec="seconds"),
                "run_dir": str(staging_dir),
                "error": f"missing_audit_script: {audit_script}",
                "integrity_threshold": integrity_threshold,
                "integrity_gate_pass": False,
            },
            False,
        )

    cmd = [
        sys.executable,
        str(audit_script),
        "--run-dir",
        str(staging_dir),
        "--integrity-threshold",
        f"{integrity_threshold:.6f}",
        "--json-out",
        str(audit_report),
    ]
    if not enforce_integrity:
        cmd.append("--no-enforce-integrity")

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.stderr.strip():
        print(proc.stderr.strip())

    summary: dict[str, Any] | None = None
    if audit_report.exists():
        try:
            summary = json.loads(audit_report.read_text(encoding="utf-8"))
        except Exception:
            summary = None

    audit_pass = False
    if isinstance(summary, dict):
        audit_pass = bool(summary.get("integrity_gate_pass"))
    elif proc.returncode == 0:
        audit_pass = True

    if enforce_integrity and proc.returncode != 0:
        audit_pass = False

    return summary, audit_pass


def run_pull(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    include_adjacent: bool = False,
    integrity_threshold: float = 0.95,
    enforce_integrity: bool = True,
    external_audit: bool = True,
) -> int:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    run_started_at = datetime.now().isoformat(timespec="seconds")

    state = load_state()
    source_state = state.setdefault("sources", {})

    discovery_rows = load_discovery_rows(include_adjacent=include_adjacent)
    discovery_index = {str(Path(p).stem): p for p in iter_agenda_output_texts(AGENDA_OUTPUT_ROOT)}

    run_rows: list[dict[str, Any]] = []
    staged = 0
    scanned = 0
    skipped_unchanged = 0
    no_hits = 0
    missing_source = 0
    integrity_pass_count = 0

    staging_dir = STAGING_ROOT / run_id
    if not dry_run:
        staging_dir.mkdir(parents=True, exist_ok=True)

    for row in discovery_rows:
        scanned += 1
        if limit is not None and staged >= limit:
            break

        agenda_code = str(row.get("agenda_code") or "").strip()
        if not agenda_code:
            continue

        source_txt_value = str(row.get("source_txt") or "").strip()
        source_txt_path = Path(source_txt_value) if source_txt_value else discovery_index.get(agenda_code)
        if not source_txt_path or not source_txt_path.exists():
            missing_source += 1
            continue

        source_key = str(source_txt_path)
        source_sha256 = sha256_file(source_txt_path)
        discovery_sha256 = sha256_text(json.dumps(row, ensure_ascii=True, sort_keys=True))
        prev = source_state.get(source_key, {})

        if (
            not force
            and prev.get("source_sha256") == source_sha256
            and prev.get("discovery_sha256") == discovery_sha256
            and prev.get("last_status") == "staged"
        ):
            skipped_unchanged += 1
            continue

        text = source_txt_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        excerpts, metrics = build_excerpts_from_row(row, lines)

        if not excerpts:
            no_hits += 1
            source_state[source_key] = {
                "source_sha256": source_sha256,
                "discovery_sha256": discovery_sha256,
                "last_status": "no_hits",
                "last_run_id": run_id,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            if not dry_run:
                save_state(state)
            continue

        staged += 1
        machine_code = agenda_code
        integrity_score = float(metrics.get("integrity_score") or 0.0)
        integrity_pass = integrity_score >= integrity_threshold
        if integrity_pass:
            integrity_pass_count += 1

        source_bundle_txt, source_bundle_factsheet, source_bundle_count = write_source_bundle(
            source_txt=source_txt_path,
            agenda_code=agenda_code,
            excerpts=excerpts,
            dry_run=dry_run,
        )

        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "department_reports_pull_record",
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source_lane": SOURCE_LANE,
            "candidate_status": str(row.get("candidate_status") or ""),
            "agenda_code": agenda_code,
            "machine_code": machine_code,
            "source_txt": str(source_txt_path),
            "source_sha256": source_sha256,
            "source_bundle_txt": str(source_bundle_txt),
            "source_bundle_factsheet": str(source_bundle_factsheet) if source_bundle_factsheet else None,
            "source_bundle_file_count": source_bundle_count,
            "discovery_run_id": str(row.get("run_id") or ""),
            "discovery_sha256": discovery_sha256,
            "primary_label": str(row.get("primary_label") or ""),
            "primary_score": row.get("primary_score"),
            "positive_labels": row.get("positive_labels") if isinstance(row.get("positive_labels"), list) else [],
            "heading_positive_labels": (
                row.get("heading_positive_labels") if isinstance(row.get("heading_positive_labels"), list) else []
            ),
            "integrity": {
                **metrics,
                "threshold": integrity_threshold,
                "pass": integrity_pass,
            },
            "excerpts": [
                {
                    "kind": ex.kind,
                    "label": ex.label,
                    "start_line": ex.start_line,
                    "end_line": ex.end_line,
                    "anchor_line": ex.anchor_line,
                    "anchor_text": ex.anchor_text,
                    "text": ex.text,
                    "signals": ex.signals,
                }
                for ex in excerpts
            ],
        }

        payload_text = json.dumps(payload, ensure_ascii=True, indent=2) + "\n"
        extract_sha256 = sha256_text(payload_text)

        json_out = staging_dir / f"{machine_code}.department_reports.json"
        txt_out = staging_dir / f"{machine_code}.department_reports.txt"

        if not dry_run:
            json_out.write_text(payload_text, encoding="utf-8")
            txt_out.write_text(render_staging_summary(source_txt_path, row, excerpts, metrics), encoding="utf-8")

        manifest_row = {
            "run_id": run_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "machine_code": machine_code,
            "source_txt": str(source_txt_path),
            "source_sha256": source_sha256,
            "extract_sha256": extract_sha256,
            "candidate_status": str(row.get("candidate_status") or ""),
            "primary_label": str(row.get("primary_label") or ""),
            "excerpt_count": len(excerpts),
            "integrity_score": integrity_score,
            "integrity_threshold": integrity_threshold,
            "integrity_pass": integrity_pass,
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }
        run_rows.append(manifest_row)

        source_state[source_key] = {
            "source_sha256": source_sha256,
            "discovery_sha256": discovery_sha256,
            "extract_sha256": extract_sha256,
            "last_status": "staged",
            "last_run_id": run_id,
            "integrity_score": integrity_score,
            "integrity_pass": integrity_pass,
            "excerpt_count": len(excerpts),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "staged_json": str(json_out),
            "staged_txt": str(txt_out),
        }
        if not dry_run:
            append_manifest_rows([manifest_row])
            save_state(state)

    integrity_rate = (integrity_pass_count / staged) if staged else 0.0
    integrity_gate_pass = integrity_rate >= integrity_threshold
    external_audit_summary: dict[str, Any] | None = None
    external_integrity_rate: float | None = None
    external_records_pass: int | None = None
    external_records_total: int | None = None
    external_gate_pass: bool | None = None

    if not dry_run:
        manifest_path = staging_dir / "department_reports_output_pull_manifest.jsonl"
        with manifest_path.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if external_audit and staged > 0:
            external_audit_summary, external_gate_pass = run_external_integrity_audit(
                staging_dir=staging_dir,
                integrity_threshold=integrity_threshold,
                enforce_integrity=enforce_integrity,
            )
            if isinstance(external_audit_summary, dict):
                external_integrity_rate_raw = external_audit_summary.get("external_document_integrity_rate")
                try:
                    if external_integrity_rate_raw is not None:
                        external_integrity_rate = float(external_integrity_rate_raw)
                except Exception:
                    external_integrity_rate = None
                try:
                    external_records_pass = int(external_audit_summary.get("documents_pass"))
                    external_records_total = int(external_audit_summary.get("documents_total"))
                except Exception:
                    external_records_pass = None
                    external_records_total = None
        elif external_audit:
            external_gate_pass = True

        summary = {
            "run_id": run_id,
            "started_at": run_started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
            "source_lane": SOURCE_LANE,
            "scanned_discovery_rows": scanned,
            "staged_files": staged,
            "skipped_unchanged": skipped_unchanged,
            "no_hits": no_hits,
            "missing_source": missing_source,
            "integrity_threshold": integrity_threshold,
            "records_passing_integrity": integrity_pass_count,
            "document_integrity_rate": round(integrity_rate, 4),
            "integrity_gate_pass": integrity_gate_pass,
            "external_integrity_threshold": integrity_threshold if external_audit else None,
            "external_document_integrity_rate": external_integrity_rate,
            "external_records_passing_integrity": external_records_pass,
            "external_documents_total": external_records_total,
            "external_integrity_gate_pass": external_gate_pass,
            "external_audit_schema_version": (
                external_audit_summary.get("schema_version")
                if isinstance(external_audit_summary, dict)
                else (EXTERNAL_AUDIT_SCHEMA_VERSION if external_audit else None)
            ),
            "external_audit_report": str(staging_dir / "external_integrity_audit.json") if external_audit else None,
            "agenda_output_root": str(AGENDA_OUTPUT_ROOT),
            "discovery_manifest": str(DISCOVERY_MANIFEST_FILE),
            "staging_dir": str(staging_dir),
        }
        (staging_dir / "run_summary.json").write_text(
            json.dumps(summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    print("=" * 64)
    print("DEPARTMENT REPORTS OUTPUT PULL SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Discovery manifest: {DISCOVERY_MANIFEST_FILE}")
    print(f"  Candidate rows scanned: {scanned}")
    print(f"  Files staged: {staged}")
    print(f"  Files skipped (unchanged): {skipped_unchanged}")
    print(f"  Files with no report hits: {no_hits}")
    print(f"  Files missing source txt: {missing_source}")
    print(f"  Integrity threshold: {integrity_threshold:.2f}")
    print(f"  Records passing integrity: {integrity_pass_count}/{staged}")
    print(f"  Document integrity rate: {integrity_rate:.4f}")
    print(f"  Integrity gate pass: {integrity_gate_pass}")
    if not dry_run and external_audit:
        if external_integrity_rate is not None and external_records_pass is not None and external_records_total is not None:
            print(
                f"  External integrity rate: {external_integrity_rate:.4f} "
                f"({external_records_pass}/{external_records_total})"
            )
        if external_gate_pass is not None:
            print(f"  External integrity gate pass: {external_gate_pass}")
    if dry_run:
        print("  Dry run: yes (no files written)")
    else:
        print(f"  Staging dir: {staging_dir}")
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
    print("=" * 64)

    if enforce_integrity and staged > 0 and not integrity_gate_pass:
        return 2
    if enforce_integrity and staged > 0 and external_audit and external_gate_pass is False:
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull department report material from agenda parser outputs into DepartmentReports staging."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after staging N source files with department-report hits.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess all source files even if unchanged in state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and score without writing staging/manifests/state.",
    )
    parser.add_argument(
        "--include-adjacent",
        action="store_true",
        help="Include discovery rows marked adjacent (default: candidate only).",
    )
    parser.add_argument(
        "--integrity-threshold",
        type=float,
        default=0.95,
        help="Minimum required integrity score/rate target (default: 0.95).",
    )
    parser.add_argument(
        "--no-enforce-integrity",
        action="store_true",
        help="Do not fail exit code when integrity rate is below threshold.",
    )
    parser.add_argument(
        "--no-external-audit",
        action="store_true",
        help="Skip independent post-pull external integrity audit gate.",
    )
    args = parser.parse_args()

    return run_pull(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        include_adjacent=args.include_adjacent,
        integrity_threshold=float(args.integrity_threshold),
        enforce_integrity=not bool(args.no_enforce_integrity),
        external_audit=not bool(args.no_external_audit),
    )


if __name__ == "__main__":
    raise SystemExit(main())
