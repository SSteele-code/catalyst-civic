import os
#!/usr/bin/env python
"""
Department Reports PRE_PARSE (Agenda-Staging Lane)

Transforms staged agenda-mined department-report excerpts into normalized schema
and writes pusher-ready artifacts into:
  _Sources/M1-Meetings/DepartmentReports/_output/<department_report_code>/

Strict invariant:
  - PRE_PARSE only (schema normalization + lineage packaging)
  - No DB writes
  - No glossary writes

Linkage contract:
  - source_pdf_code:        M1.AG.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
  - department_report_code: M1.AG.DR.<docnum>.<created_yyyymmdd>.<pulled_yyyymmdd>
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


DEPARTMENT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "DepartmentReports"
STAGING_ROOT = DEPARTMENT_ROOT / "_staging"
OUTPUT_ROOT = DEPARTMENT_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = DEPARTMENT_ROOT / "department_reports_preparse_state.json"
MANIFEST_FILE = DEPARTMENT_ROOT / "M1_DEPARTMENT_REPORTS_PREPARSE_MANIFEST.jsonl"

SOURCE_SCHEMA_VERSION = "m1.department_reports.pull.v1"
SCHEMA_VERSION = "m1.department_reports.preparse.v1"
SOURCE_LANE = "agenda_output_department_reports_sections"
JURISDICTION = "Richlands"

AG_CODE_RE = re.compile(r"^M1\.AG\.(\d{6})\.(\d{8})\.(\d{8})$", re.IGNORECASE)


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
    department_report_code: str
    candidate_status: str
    primary_label: str
    positive_labels: list[str]
    heading_positive_labels: list[str]
    source_integrity: dict[str, Any]
    excerpts: list[dict[str, Any]]


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
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: Sequence[dict]) -> None:
    if not rows:
        return
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)
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
        code = str(row.get("department_report_code") or "").strip()
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


def iter_stage_json_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.department_reports.json"))


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
    files = sorted(run_dir.rglob("*.department_reports.json"))
    return files, run_id


def build_department_report_code(source_pdf_code: str) -> str | None:
    match = AG_CODE_RE.match(source_pdf_code.strip())
    if not match:
        return None
    docnum, created_ymd, pulled_ymd = match.group(1), match.group(2), match.group(3)
    return f"M1.AG.DR.{docnum}.{created_ymd}.{pulled_ymd}"


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
) -> tuple[Path | None, dict | None]:
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

    department_report_code = build_department_report_code(source_pdf_code)
    if not department_report_code:
        return None, f"unmappable_source_pdf_code: {source_pdf_code}"

    excerpts_raw = payload.get("excerpts")
    if not isinstance(excerpts_raw, list):
        return None, "missing_excerpts"

    source_txt_sha256 = sha256_file(source_txt_path)
    source_bundle_txt_raw = str(payload.get("source_bundle_txt") or "").strip()
    source_bundle_txt = Path(source_bundle_txt_raw) if source_bundle_txt_raw else None

    positive_labels_raw = payload.get("positive_labels")
    if not isinstance(positive_labels_raw, list):
        positive_labels_raw = []
    heading_positive_labels_raw = payload.get("heading_positive_labels")
    if not isinstance(heading_positive_labels_raw, list):
        heading_positive_labels_raw = []

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
        source_txt_sha256=source_txt_sha256,
        source_bundle_txt=source_bundle_txt,
        factsheet_path=factsheet_path if factsheet_path else Path(""),
        source_pdf_code=source_pdf_code,
        source_pdf_original_name=source_pdf_original_name,
        source_pdf_internal_name=str(facts.get("source_pdf_internal_name") or "").strip(),
        source_pdf_hash=str(facts.get("source_pdf_hash") or "").strip(),
        page_count=facts.get("page_count") if isinstance(facts.get("page_count"), int) else None,
        department_report_code=department_report_code,
        candidate_status=str(payload.get("candidate_status") or "").strip(),
        primary_label=str(payload.get("primary_label") or "").strip(),
        positive_labels=[str(x).strip() for x in positive_labels_raw if str(x).strip()],
        heading_positive_labels=[str(x).strip() for x in heading_positive_labels_raw if str(x).strip()],
        source_integrity=source_integrity,
        excerpts=[x for x in excerpts_raw if isinstance(x, dict)],
    )
    return candidate, None


def choose_best_candidate(candidates: Sequence[StageCandidate]) -> StageCandidate:
    def rank_key(c: StageCandidate) -> tuple[str, str]:
        return (c.source_stage_captured_at, str(c.stage_json_path))

    return sorted(candidates, key=rank_key, reverse=True)[0]


def render_summary_text(payload: dict) -> str:
    summary = payload.get("department_report_summary") if isinstance(payload.get("department_report_summary"), dict) else {}
    header = [
        f"DEPARTMENT_REPORT_CODE: {payload.get('department_report_code')}",
        f"SOURCE_PDF_CODE: {payload.get('linked_source_pdf_code')}",
        f"SOURCE_LANE: {payload.get('source_lane')}",
        f"EXCERPT_COUNT: {summary.get('excerpt_count')}",
        f"PRIMARY_LABEL: {summary.get('primary_label')}",
        "",
    ]
    body: list[str] = []
    for ex in payload.get("department_report_excerpts", []):
        body.append(
            f"[{ex['excerpt_id']}] {ex['label']} lines {ex['start_line']}-{ex['end_line']} "
            f"(anchor={ex['anchor_line']})"
        )
        body.append(ex["text"])
        body.append("")
    return "\n".join(header + body).strip() + "\n"


def build_payload(candidate: StageCandidate, run_id: str) -> dict:
    source_match = AG_CODE_RE.match(candidate.source_pdf_code)
    assert source_match is not None
    created_ymd = source_match.group(2)
    anchor_meeting_date = ymd_to_iso(created_ymd)

    excerpt_rows: list[dict[str, Any]] = []
    kind_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}

    for idx, raw in enumerate(candidate.excerpts, start=1):
        kind = str(raw.get("kind") or "department_report_section").strip()
        label = str(raw.get("label") or "DEPARTMENT_REPORT").strip()
        start_line = to_int(raw.get("start_line"), default=0)
        end_line = to_int(raw.get("end_line"), default=0)
        anchor_line = to_int(raw.get("anchor_line"), default=0)
        anchor_text = str(raw.get("anchor_text") or "").strip()
        text = str(raw.get("text") or "").strip()
        signals = raw.get("signals") if isinstance(raw.get("signals"), dict) else {}

        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        label_counts[label] = label_counts.get(label, 0) + 1

        excerpt_rows.append(
            {
                "excerpt_id": f"EX{idx:03d}",
                "kind": kind,
                "label": label,
                "start_line": start_line,
                "end_line": end_line,
                "anchor_line": anchor_line,
                "anchor_text": anchor_text,
                "text": text,
                "text_sha256": sha256_text(text),
                "signals": signals,
            }
        )

    source_integrity_score = candidate.source_integrity.get("integrity_score")
    source_integrity_pass = candidate.source_integrity.get("pass")
    source_integrity_threshold = candidate.source_integrity.get("threshold")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "department_reports_preparse_record",
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
        "preparse_run_id": run_id,
        "source_lane": candidate.source_lane or SOURCE_LANE,
        "jurisdiction": JURISDICTION,
        "department_report_code": candidate.department_report_code,
        "artifact_machine_code": candidate.department_report_code,
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
        "department_report_summary": {
            "excerpt_count": len(excerpt_rows),
            "excerpt_kind_counts": kind_counts,
            "excerpt_label_counts": label_counts,
            "candidate_status": candidate.candidate_status,
            "primary_label": candidate.primary_label,
            "positive_labels": candidate.positive_labels,
            "heading_positive_labels": candidate.heading_positive_labels,
            "source_integrity_score": source_integrity_score,
            "source_integrity_pass": source_integrity_pass,
            "source_integrity_threshold": source_integrity_threshold,
        },
        "department_report_excerpts": excerpt_rows,
        "pusher_ready": {
            "report_packet_id": candidate.department_report_code,
            "source_id": candidate.source_pdf_code,
            "content_mode": "excerpt_pack",
            "is_complete_report_document": False,
            "glossary_scope_text_hint": "department_report_excerpts[].text",
            "primary_label": candidate.primary_label,
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
        groups.setdefault(candidate.department_report_code, []).append(candidate)

    chosen: list[StageCandidate] = []
    for _, group in groups.items():
        chosen.append(choose_best_candidate(group))

    chosen.sort(key=lambda c: c.department_report_code)
    if limit is not None:
        chosen = chosen[:limit]

    run_dir = RUNS_ROOT / run_id
    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)

    for candidate in chosen:
        prev = state_records.get(candidate.department_report_code, {})
        output_json = OUTPUT_ROOT / candidate.department_report_code / f"{candidate.department_report_code}.preparse.json"
        output_txt = OUTPUT_ROOT / candidate.department_report_code / f"{candidate.department_report_code}.preparse.txt"

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
                out_dir = OUTPUT_ROOT / candidate.department_report_code
                out_dir.mkdir(parents=True, exist_ok=True)
                output_json.write_text(payload_text, encoding="utf-8")
                output_txt.write_text(render_summary_text(payload), encoding="utf-8")

            prepared += 1
            row = {
                "run_id": run_id,
                "prepared_at": datetime.now().isoformat(timespec="seconds"),
                "schema_version": SCHEMA_VERSION,
                "department_report_code": candidate.department_report_code,
                "linked_source_pdf_code": candidate.source_pdf_code,
                "source_stage_json_path": str(candidate.stage_json_path),
                "source_stage_json_sha256": candidate.stage_json_sha256,
                "source_txt_path": str(candidate.source_txt_path),
                "source_txt_sha256": candidate.source_txt_sha256,
                "payload_sha256": payload_sha256,
                "output_json": str(output_json),
                "output_txt": str(output_txt),
                "excerpt_count": len(payload.get("department_report_excerpts", [])),
                "primary_label": candidate.primary_label,
            }
            prepared_rows.append(row)

            state_records[candidate.department_report_code] = {
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
                "excerpt_count": len(payload.get("department_report_excerpts", [])),
                "primary_label": candidate.primary_label,
            }
            if not dry_run:
                save_state(state)
                if candidate.department_report_code not in manifest_codes:
                    append_manifest_rows([row])
                    manifest_codes.add(candidate.department_report_code)
        except Exception as exc:
            failed += 1
            failure_rows.append(
                {
                    "run_id": run_id,
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "department_report_code": candidate.department_report_code,
                    "source_stage_json_path": str(candidate.stage_json_path),
                    "error": str(exc),
                }
            )
            continue

    if not dry_run:
        run_manifest = run_dir / "department_reports_preparse_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in prepared_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            run_failures = run_dir / "department_reports_preparse_failures.jsonl"
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
            "mapped_department_report_codes": mapped,
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
    print("DEPARTMENT REPORTS PRE_PARSE SUMMARY")
    print(f"  Run ID: {run_id}")
    print(f"  Source lane: {SOURCE_LANE}")
    print(f"  Staging root: {STAGING_ROOT}")
    print(f"  Source stage scope: {effective_source_run_id}")
    print(f"  Output root: {OUTPUT_ROOT}")
    print(f"  Stage files discovered: {discovered}")
    print(f"  Department-report codes mapped: {mapped}")
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
        description="Normalize staged DepartmentReports artifacts into pusher-ready DepartmentReports schema."
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only first N department-report codes.")
    parser.add_argument("--force", action="store_true", help="Rebuild outputs even when unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan/map only; do not write outputs.")
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Use a specific pull run id under _staging (for example RUN_20260519T212719). Default: latest run only.",
    )
    parser.add_argument(
        "--all-staging",
        action="store_true",
        help="Process all staged runs under _staging (legacy behavior).",
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
