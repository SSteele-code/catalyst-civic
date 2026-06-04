#!/usr/bin/env python
"""
Department Reports PUSH (DB Loader)

Loads normalized department-reports parse/preparse records from
`_Sources/M1-Meetings/DepartmentReports/_output` into:
  - m1_department_reports.documents
  - m1_department_reports.excerpts
  - m1_department_reports.figures

Strict invariant:
  - DB load only for department-reports tables
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


DEPARTMENT_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "DepartmentReports"
OUTPUT_ROOT = DEPARTMENT_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = DEPARTMENT_ROOT / "department_reports_push_state.json"
MANIFEST_FILE = DEPARTMENT_ROOT / "M1_DEPARTMENT_REPORTS_PUSH_MANIFEST.jsonl"
PARSE_STATE_FILE = DEPARTMENT_ROOT / "department_reports_preparse_state.json"

SOURCE_SCHEMA_VERSION = "m1.department_reports.preparse.v1"
SOURCE_SCHEMA_VERSION_COMPAT = "m1.department_reports.parse.v1"
SOURCE_SCHEMA_VERSIONS = {SOURCE_SCHEMA_VERSION, SOURCE_SCHEMA_VERSION_COMPAT}
PUSH_SCHEMA_VERSION = "m1.department_reports.push.v1"
TARGET_SCHEMA = "m1_department_reports"
JURISDICTION_DEFAULT = "Richlands"

RECORD_DIR_RE = re.compile(r"^M1\.AG\.DR\.\d{6}\.\d{8}\.\d{8}$", re.IGNORECASE)

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


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    DEPARTMENT_ROOT.mkdir(parents=True, exist_ok=True)
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


def ensure_target_tables(cur: Any) -> None:
    cur.execute(
        "SELECT to_regclass('m1_department_reports.documents'), "
        "to_regclass('m1_department_reports.excerpts'), "
        "to_regclass('m1_department_reports.figures')"
    )
    docs_reg, excerpts_reg, figures_reg = cur.fetchone()
    if not docs_reg or not excerpts_reg or not figures_reg:
        raise RuntimeError(
            "Target tables missing. Apply "
            "_Infra/DATABASE/init/017_department_reports_schema.sql and "
            "_Infra/DATABASE/init/018_department_reports_excerpts_figures.sql before PUSH."
        )


def derive_document_row(
    payload: dict,
    record_code: str,
    source_parse_json: Path,
    source_parse_sha256: str,
) -> dict:
    pusher = payload.get("pusher_ready") if isinstance(payload.get("pusher_ready"), dict) else {}
    context = payload.get("meeting_context") if isinstance(payload.get("meeting_context"), dict) else {}
    summary = payload.get("department_report_summary")
    summary = summary if isinstance(summary, dict) else {}
    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    source_lane = str(payload.get("source_lane") or "").strip()

    record_id = str(pusher.get("report_packet_id") or payload.get("department_report_code") or record_code).strip()
    source_id = str(
        pusher.get("source_id")
        or payload.get("linked_source_pdf_code")
        or payload.get("artifact_machine_code")
        or record_id
    ).strip()

    meeting_date = to_iso_date(context.get("anchor_meeting_date"))
    primary_label = clean_text(pusher.get("primary_label")) or clean_text(summary.get("primary_label"))
    candidate_status = clean_text(summary.get("candidate_status"))
    content_mode = clean_text(pusher.get("content_mode"))
    is_complete = bool(pusher.get("is_complete_report_document"))
    excerpt_count = to_int(summary.get("excerpt_count")) or 0
    source_integrity_score = to_float(summary.get("source_integrity_score"))
    source_integrity_pass = None
    if summary.get("source_integrity_pass") is not None:
        source_integrity_pass = bool(summary.get("source_integrity_pass"))

    metadata = {
        "source_schema_version": str(payload.get("schema_version") or ""),
        "source_record_type": str(payload.get("record_type") or ""),
        "source_parse_run_id": str(payload.get("parse_run_id") or payload.get("preparse_run_id") or ""),
        "lineage": lineage,
        "department_report_summary": summary,
        "push_source_parse_json": str(source_parse_json),
        "push_source_parse_sha256": source_parse_sha256,
        "push_loaded_at": datetime.now().isoformat(timespec="seconds"),
    }

    return {
        "record_id": record_id,
        "source_id": source_id,
        "source_lane": source_lane,
        "jurisdiction": str(payload.get("jurisdiction") or JURISDICTION_DEFAULT),
        "meeting_date": meeting_date,
        "primary_label": primary_label,
        "candidate_status": candidate_status,
        "content_mode": content_mode,
        "is_complete_report_document": is_complete,
        "linked_source_pdf_code": clean_text(payload.get("linked_source_pdf_code")),
        "excerpt_count": excerpt_count,
        "source_integrity_score": source_integrity_score,
        "source_integrity_pass": source_integrity_pass,
        "metadata": metadata,
    }


def derive_excerpt_rows(payload: dict, record_id: str) -> list[dict]:
    excerpts = payload.get("department_report_excerpts")
    if not isinstance(excerpts, list):
        return []
    out: list[dict] = []
    for idx, ex in enumerate(excerpts, start=1):
        if not isinstance(ex, dict):
            continue
        excerpt_id = str(ex.get("excerpt_id") or f"EX{idx:03d}").strip()
        content = str(ex.get("text") or "")
        content_sha256 = str(ex.get("text_sha256") or sha256_text(content))
        signals = ex.get("signals") if isinstance(ex.get("signals"), dict) else {}
        metadata = {
            "source_excerpt_payload": {
                "kind": ex.get("kind"),
                "label": ex.get("label"),
                "anchor_line": ex.get("anchor_line"),
                "start_line": ex.get("start_line"),
                "end_line": ex.get("end_line"),
            }
        }
        out.append(
            {
                "excerpt_row_id": f"{record_id}.{excerpt_id}",
                "record_id": record_id,
                "excerpt_id": excerpt_id,
                "ordinal": idx,
                "kind": clean_text(ex.get("kind")),
                "label": clean_text(ex.get("label")),
                "anchor_line": to_int(ex.get("anchor_line")),
                "start_line": to_int(ex.get("start_line")),
                "end_line": to_int(ex.get("end_line")),
                "anchor_text": clean_text(ex.get("anchor_text")),
                "content": content,
                "content_sha256": content_sha256,
                "signals": signals,
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


def derive_figure_rows(excerpt_rows: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for ex in excerpt_rows:
        text = str(ex.get("content") or "")
        figures = extract_figures_from_text(text)
        for idx, fg in enumerate(figures, start=1):
            figure_row_id = f"{ex['excerpt_row_id']}.FG{idx:03d}"
            rows.append(
                {
                    "figure_row_id": figure_row_id,
                    "record_id": ex["record_id"],
                    "excerpt_row_id": ex["excerpt_row_id"],
                    "excerpt_id": ex["excerpt_id"],
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


def upsert_document(cur: Any, row: dict) -> None:
    cur.execute(
        """
        INSERT INTO m1_department_reports.documents (
            record_id, source_id, source_lane, jurisdiction, meeting_date,
            primary_label, candidate_status, content_mode, is_complete_report_document,
            linked_source_pdf_code, excerpt_count, source_integrity_score, source_integrity_pass, metadata
        )
        VALUES (
            %(record_id)s, %(source_id)s, %(source_lane)s, %(jurisdiction)s, %(meeting_date)s,
            %(primary_label)s, %(candidate_status)s, %(content_mode)s, %(is_complete_report_document)s,
            %(linked_source_pdf_code)s, %(excerpt_count)s, %(source_integrity_score)s, %(source_integrity_pass)s, %(metadata)s::jsonb
        )
        ON CONFLICT (record_id) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            source_lane = EXCLUDED.source_lane,
            jurisdiction = EXCLUDED.jurisdiction,
            meeting_date = EXCLUDED.meeting_date,
            primary_label = EXCLUDED.primary_label,
            candidate_status = EXCLUDED.candidate_status,
            content_mode = EXCLUDED.content_mode,
            is_complete_report_document = EXCLUDED.is_complete_report_document,
            linked_source_pdf_code = EXCLUDED.linked_source_pdf_code,
            excerpt_count = EXCLUDED.excerpt_count,
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


def replace_excerpts(cur: Any, record_id: str, excerpt_rows: list[dict]) -> None:
    cur.execute("DELETE FROM m1_department_reports.excerpts WHERE record_id = %s", (record_id,))
    if not excerpt_rows:
        return
    for row in excerpt_rows:
        cur.execute(
            """
            INSERT INTO m1_department_reports.excerpts (
                excerpt_row_id, record_id, excerpt_id, ordinal, kind, label,
                anchor_line, start_line, end_line, anchor_text, content, content_sha256,
                signals, metadata
            )
            VALUES (
                %(excerpt_row_id)s, %(record_id)s, %(excerpt_id)s, %(ordinal)s, %(kind)s, %(label)s,
                %(anchor_line)s, %(start_line)s, %(end_line)s, %(anchor_text)s, %(content)s, %(content_sha256)s,
                %(signals)s::jsonb, %(metadata)s::jsonb
            )
            ON CONFLICT (excerpt_row_id) DO UPDATE SET
                record_id = EXCLUDED.record_id,
                excerpt_id = EXCLUDED.excerpt_id,
                ordinal = EXCLUDED.ordinal,
                kind = EXCLUDED.kind,
                label = EXCLUDED.label,
                anchor_line = EXCLUDED.anchor_line,
                start_line = EXCLUDED.start_line,
                end_line = EXCLUDED.end_line,
                anchor_text = EXCLUDED.anchor_text,
                content = EXCLUDED.content,
                content_sha256 = EXCLUDED.content_sha256,
                signals = EXCLUDED.signals,
                metadata = EXCLUDED.metadata;
            """,
            {
                **row,
                "signals": json.dumps(row["signals"], ensure_ascii=True),
                "metadata": json.dumps(row["metadata"], ensure_ascii=True),
            },
        )


def replace_figures(cur: Any, record_id: str, figure_rows: list[dict]) -> None:
    cur.execute("DELETE FROM m1_department_reports.figures WHERE record_id = %s", (record_id,))
    if not figure_rows:
        return
    for row in figure_rows:
        cur.execute(
            """
            INSERT INTO m1_department_reports.figures (
                figure_row_id, record_id, excerpt_row_id, excerpt_id, ordinal,
                figure_type, raw_value, numeric_value, unit, start_char, end_char,
                context_snippet, metadata
            )
            VALUES (
                %(figure_row_id)s, %(record_id)s, %(excerpt_row_id)s, %(excerpt_id)s, %(ordinal)s,
                %(figure_type)s, %(raw_value)s, %(numeric_value)s, %(unit)s, %(start_char)s, %(end_char)s,
                %(context_snippet)s, %(metadata)s::jsonb
            )
            ON CONFLICT (figure_row_id) DO UPDATE SET
                record_id = EXCLUDED.record_id,
                excerpt_row_id = EXCLUDED.excerpt_row_id,
                excerpt_id = EXCLUDED.excerpt_id,
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


def run_push(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    source_run_id: str | None = None,
    all_output: bool = False,
) -> dict:
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
    excerpts_pushed = 0
    figures_pushed = 0
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
                        "department_report_code": record_code,
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
                        "department_report_code": record_code,
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

            doc_row = derive_document_row(payload, record_code, parse_json, source_parse_sha256)
            record_id = doc_row["record_id"]
            excerpt_rows = derive_excerpt_rows(payload, record_id)
            figure_rows = derive_figure_rows(excerpt_rows)

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
                excerpts_pushed += len(excerpt_rows)
                figures_pushed += len(figure_rows)
                continue

            try:
                assert cur is not None
                assert conn is not None
                upsert_document(cur, doc_row)
                replace_excerpts(cur, record_id, excerpt_rows)
                replace_figures(cur, record_id, figure_rows)
                conn.commit()

                pushed += 1
                excerpts_pushed += len(excerpt_rows)
                figures_pushed += len(figure_rows)
                row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "record_id": record_id,
                    "source_id": doc_row["source_id"],
                    "source_lane": doc_row["source_lane"],
                    "meeting_date": doc_row["meeting_date"],
                    "primary_label": doc_row["primary_label"],
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_parse_sha256,
                    "excerpts_count": len(excerpt_rows),
                    "figures_count": len(figure_rows),
                    "db_schema": TARGET_SCHEMA,
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
                    "source_lane": doc_row["source_lane"],
                    "excerpts_count": len(excerpt_rows),
                    "figures_count": len(figure_rows),
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
                        "department_report_code": record_code,
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
        run_manifest = run_dir / "department_reports_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "department_reports_push_failures.jsonl"
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
            "pushed_excerpts": excerpts_pushed,
            "pushed_figures": figures_pushed,
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
        "pushed_excerpts": excerpts_pushed,
        "pushed_figures": figures_pushed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_source_scope": skipped_source_scope,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 68)
    print("DEPARTMENT REPORTS PUSH SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Target schema: {summary['target_schema']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Pushed records: {summary['pushed_records']}")
    print(f"  Pushed excerpts: {summary['pushed_excerpts']}")
    print(f"  Pushed figures: {summary['pushed_figures']}")
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
        description="Push department-reports parse output records into m1_department_reports tables."
    )
    parser.add_argument("--limit", type=int, default=None, help="Push first N department-report records.")
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
