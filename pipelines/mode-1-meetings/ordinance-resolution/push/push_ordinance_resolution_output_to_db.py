#!/usr/bin/env python
"""
Ordinance/Resolution PUSH (DB Loader)

Loads normalized ordinance/resolution PARSE records from
`_Sources/M1-Meetings/Ordinance_Resolution/_output` into:
  - m1_ordinance_resolution.documents

Strict invariant:
  - DB load only for ordinance/resolution metadata table
  - No full document body writes
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


OR_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Ordinance_Resolution"
OUTPUT_ROOT = OR_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = OR_ROOT / "ordinance_resolution_push_state.json"
MANIFEST_FILE = OR_ROOT / "M1_ORDINANCE_RESOLUTION_PUSH_MANIFEST.jsonl"
PARSE_STATE_FILE = OR_ROOT / "ordinance_resolution_preparse_state.json"

SOURCE_SCHEMA_VERSION = "m1.ordinance_resolution.parse.v1"
SOURCE_SCHEMA_VERSION_COMPAT = "m1.ordinance_resolution.preparse.v1"
SOURCE_SCHEMA_VERSIONS = {SOURCE_SCHEMA_VERSION, SOURCE_SCHEMA_VERSION_COMPAT}
PUSH_SCHEMA_VERSION = "m1.ordinance_resolution.push.v1"
TARGET_SCHEMA = "m1_ordinance_resolution"
TARGET_TABLE = "documents"
JURISDICTION_DEFAULT = "Richlands"

OR_DIR_RE = re.compile(r"^M1\.AG\.OR\.\d{6}\.\d{8}\.\d{8}\.(?:ORD|RES|DOC)\.[A-Z0-9_]+$", re.IGNORECASE)

# Database configuration
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except Exception:
        return None


def to_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except Exception:
        return None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    OR_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    OR_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def iter_parse_jsons() -> list[Path]:
    if not OUTPUT_ROOT.exists():
        return []
    out: list[Path] = []
    for d in sorted(OUTPUT_ROOT.iterdir()):
        if not d.is_dir() or d.name == "_runs":
            continue
        if not OR_DIR_RE.match(d.name):
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


def ensure_target_tables(cur: Any) -> None:
    cur.execute("SELECT to_regclass('m1_ordinance_resolution.documents')")
    table_reg = cur.fetchone()[0]
    if not table_reg:
        raise RuntimeError(
            "Target table missing. Apply _Infra/DATABASE/init/016_ordinance_resolution_schema.sql before PUSH."
        )


def derive_document_row(
    payload: dict,
    record_code: str,
    source_parse_json: Path,
    source_parse_sha256: str,
) -> dict:
    pusher = payload.get("pusher_ready") if isinstance(payload.get("pusher_ready"), dict) else {}
    meta = payload.get("ordinance_resolution_metadata")
    meta = meta if isinstance(meta, dict) else {}
    context = payload.get("meeting_context")
    context = context if isinstance(context, dict) else {}
    lineage = payload.get("lineage")
    lineage = lineage if isinstance(lineage, dict) else {}
    table_projection = payload.get("table_projection")
    table_projection = table_projection if isinstance(table_projection, dict) else {}

    record_id = clean_text(pusher.get("record_id")) or clean_text(payload.get("ordinance_resolution_code")) or record_code
    source_id = (
        clean_text(pusher.get("source_id"))
        or clean_text(payload.get("linked_source_pdf_code"))
        or clean_text(payload.get("artifact_machine_code"))
        or record_id
    )
    source_lane = clean_text(payload.get("source_lane")) or "agenda_output_ordinance_resolution_metadata_only"
    jurisdiction = clean_text(payload.get("jurisdiction")) or JURISDICTION_DEFAULT
    meeting_date = to_iso_date(context.get("anchor_meeting_date"))

    document_type = clean_text(meta.get("document_type")) or clean_text(table_projection.get("document_type")) or "DOCUMENT"
    document_number = clean_text(meta.get("document_number")) or clean_text(table_projection.get("document_number"))
    document_title = clean_text(meta.get("document_title")) or clean_text(table_projection.get("document_title"))
    header_line = clean_text(meta.get("header_line")) or clean_text(table_projection.get("header_line"))
    packet_code = clean_text(lineage.get("source_packet_code")) or clean_text(table_projection.get("packet_code"))

    start_line = to_int(meta.get("start_line"))
    if start_line is None:
        start_line = to_int(table_projection.get("start_line"))
    end_line = to_int(meta.get("end_line"))
    if end_line is None:
        end_line = to_int(table_projection.get("end_line"))

    match_pattern = clean_text(meta.get("match_pattern")) or clean_text(table_projection.get("match_pattern"))
    confidence = to_float(meta.get("confidence"))
    if confidence is None:
        confidence = to_float(table_projection.get("confidence"))

    metadata = {
        "source_schema_version": str(payload.get("schema_version") or ""),
        "source_record_type": str(payload.get("record_type") or ""),
        "source_parse_run_id": str(payload.get("parse_run_id") or payload.get("preparse_run_id") or ""),
        "lineage": lineage,
        "table_projection": table_projection,
        "glossary_summary": payload.get("glossary", {}).get("summary") if isinstance(payload.get("glossary"), dict) else {},
        "push_source_parse_json": str(source_parse_json),
        "push_source_parse_sha256": source_parse_sha256,
        "push_loaded_at": datetime.now().isoformat(timespec="seconds"),
    }

    return {
        "record_id": record_id,
        "source_id": source_id,
        "source_lane": source_lane,
        "jurisdiction": jurisdiction,
        "meeting_date": meeting_date,
        "document_type": document_type,
        "document_number": document_number,
        "document_title": document_title,
        "header_line": header_line,
        "packet_code": packet_code,
        "start_line": start_line,
        "end_line": end_line,
        "match_pattern": match_pattern,
        "confidence": confidence,
        "metadata": metadata,
    }


def upsert_document(cur: Any, row: dict) -> None:
    cur.execute(
        """
        INSERT INTO m1_ordinance_resolution.documents (
            record_id, source_id, source_lane, jurisdiction, meeting_date,
            document_type, document_number, document_title, header_line, packet_code,
            start_line, end_line, match_pattern, confidence, metadata
        )
        VALUES (
            %(record_id)s, %(source_id)s, %(source_lane)s, %(jurisdiction)s, %(meeting_date)s,
            %(document_type)s, %(document_number)s, %(document_title)s, %(header_line)s, %(packet_code)s,
            %(start_line)s, %(end_line)s, %(match_pattern)s, %(confidence)s, %(metadata)s::jsonb
        )
        ON CONFLICT (record_id) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            source_lane = EXCLUDED.source_lane,
            jurisdiction = EXCLUDED.jurisdiction,
            meeting_date = EXCLUDED.meeting_date,
            document_type = EXCLUDED.document_type,
            document_number = EXCLUDED.document_number,
            document_title = EXCLUDED.document_title,
            header_line = EXCLUDED.header_line,
            packet_code = EXCLUDED.packet_code,
            start_line = EXCLUDED.start_line,
            end_line = EXCLUDED.end_line,
            match_pattern = EXCLUDED.match_pattern,
            confidence = EXCLUDED.confidence,
            metadata = EXCLUDED.metadata,
            updated_at = CURRENT_TIMESTAMP;
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
) -> dict[str, Any]:
    run_id = f"RUN-OR-PUSH-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    started_at = datetime.now().isoformat(timespec="seconds")
    run_dir = RUNS_ROOT / run_id

    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = iter_parse_jsons()
    discovered = len(candidates)
    pushed = 0
    skipped_unchanged = 0
    skipped_source_scope = 0
    failed = 0
    run_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    effective_source_run_id = source_run_id or latest_parse_run_id_from_state()

    conn = None
    cur = None
    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
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
                        "record_code": record_code,
                        "source_parse_json": str(parse_json),
                        "error": f"invalid_parse_json: {exc}",
                    }
                )
                continue

            source_schema = str(payload.get("schema_version") or "")
            if source_schema not in SOURCE_SCHEMA_VERSIONS:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "record_code": record_code,
                        "source_parse_json": str(parse_json),
                        "error": f"unsupported_schema_version: {source_schema}",
                    }
                )
                continue

            payload_run_id = str(payload.get("parse_run_id") or payload.get("preparse_run_id") or "").strip()
            if not all_output and effective_source_run_id:
                if payload_run_id != effective_source_run_id:
                    skipped_source_scope += 1
                    continue

            row = derive_document_row(payload, record_code, parse_json, source_parse_sha256)
            record_id = row["record_id"]

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
                continue

            try:
                assert cur is not None
                assert conn is not None
                upsert_document(cur, row)
                conn.commit()

                pushed += 1
                manifest_row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "record_id": record_id,
                    "source_id": row["source_id"],
                    "source_lane": row["source_lane"],
                    "meeting_date": row["meeting_date"],
                    "document_type": row["document_type"],
                    "document_number": row["document_number"],
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_parse_sha256,
                    "db_schema": TARGET_SCHEMA,
                    "db_table": TARGET_TABLE,
                }
                run_rows.append(manifest_row)
                append_manifest_rows([manifest_row])

                state_records[record_id] = {
                    "last_run_id": run_id,
                    "last_status": "pushed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_parse_sha256,
                    "record_id": record_id,
                    "source_lane": row["source_lane"],
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
                        "record_code": record_code,
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
        run_manifest = run_dir / "ordinance_resolution_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            run_failure = run_dir / "ordinance_resolution_push_failures.jsonl"
            with run_failure.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": PUSH_SCHEMA_VERSION,
            "source_schema_version": SOURCE_SCHEMA_VERSION,
            "target_schema": TARGET_SCHEMA,
            "target_table": TARGET_TABLE,
            "source_run_scope": effective_source_run_id,
            "discovered_records": discovered,
            "pushed_records": pushed,
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
        "target_table": TARGET_TABLE,
        "discovered_records": discovered,
        "pushed_records": pushed,
        "skipped_unchanged": skipped_unchanged,
        "skipped_source_scope": skipped_source_scope,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 72)
    print("ORDINANCE/RESOLUTION PUSH SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Target schema: {summary['target_schema']}")
    print(f"  Target table: {summary['target_table']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Pushed records: {summary['pushed_records']}")
    print(f"  Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"  Skipped (source scope): {summary['skipped_source_scope']}")
    print(f"  Failed: {summary['failed']}")
    if dry_run:
        print("  Dry run: yes (no DB writes)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 72)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Push ordinance/resolution parse output records into m1_ordinance_resolution.documents."
    )
    parser.add_argument("--limit", type=int, default=None, help="Push first N records.")
    parser.add_argument("--force", action="store_true", help="Re-push even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write to DB.")
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Only push records from this parse run id (default: latest parse run id from state).",
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

