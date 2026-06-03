#!/usr/bin/env python
"""
Minutes PUSH (DB Loader)

Loads normalized minutes PRE_PARSE records from `_Sources/M1-Meetings/Minutes/_output`
into `m1_minutes` tables.

Strict invariant:
  - DB load only for minutes tables
  - No glossary writes
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import psycopg2
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency: psycopg2. Install with `py -m pip install psycopg2-binary` "
        "or run this script with the Python interpreter that has psycopg2."
    ) from exc


MINUTES_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Minutes")
OUTPUT_ROOT = MINUTES_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = MINUTES_ROOT / "minutes_push_state.json"
MANIFEST_FILE = MINUTES_ROOT / "M1_MINUTES_PUSH_MANIFEST.jsonl"

SOURCE_SCHEMA_VERSION = "m1.minutes.preparse.v1"
PUSH_SCHEMA_VERSION = "m1.minutes.push.v1"
TARGET_SCHEMA = "m1_minutes"
JURISDICTION_DEFAULT = "Richlands"

MINUTES_DIR_RE = re.compile(r"^M1\.(?:AG\.)?MN\.\d{6}\.\d{8}\.\d{8}$", re.IGNORECASE)

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
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    MINUTES_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def iter_preparse_jsons() -> list[Path]:
    if not OUTPUT_ROOT.exists():
        return []
    out: list[Path] = []
    for d in sorted(OUTPUT_ROOT.iterdir()):
        if not d.is_dir() or not MINUTES_DIR_RE.match(d.name):
            continue
        p = d / f"{d.name}.preparse.json"
        if p.exists():
            out.append(p)
    return out


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


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s != "" else None


def ensure_target_tables(cur: Any) -> None:
    cur.execute("SELECT to_regclass('m1_minutes.meetings'), to_regclass('m1_minutes.excerpts')")
    meetings_reg, excerpts_reg = cur.fetchone()
    if not meetings_reg or not excerpts_reg:
        raise RuntimeError(
            "Target tables missing. Apply _Infra/DATABASE/init/013_minutes_schema.sql before PUSH."
        )


def derive_meeting_row(payload: dict, minutes_code: str, source_preparse_json: Path, source_preparse_sha256: str) -> dict:
    pusher = payload.get("pusher_ready") if isinstance(payload.get("pusher_ready"), dict) else {}
    context = payload.get("meeting_context") if isinstance(payload.get("meeting_context"), dict) else {}
    lineage = payload.get("lineage") if isinstance(payload.get("lineage"), dict) else {}
    source_lane = str(payload.get("source_lane") or "").strip()

    meeting_id = str(pusher.get("meeting_id") or payload.get("minutes_code") or minutes_code).strip()
    source_id = str(
        pusher.get("source_id")
        or payload.get("linked_source_pdf_code")
        or payload.get("artifact_machine_code")
        or meeting_id
    ).strip()
    meeting_type = clean_text(context.get("anchor_meeting_type"))
    meeting_date = to_iso_date(context.get("anchor_meeting_date"))
    content_mode = clean_text(pusher.get("content_mode"))
    is_complete = bool(pusher.get("is_complete_minutes_document"))

    metadata = {
        "source_schema_version": str(payload.get("schema_version") or ""),
        "source_record_type": str(payload.get("record_type") or ""),
        "source_preparse_run_id": str(payload.get("preparse_run_id") or ""),
        "lineage": lineage,
        "minutes_excerpt_summary": payload.get("minutes_excerpt_summary") or {},
        "ocr_summary": payload.get("ocr_summary") or {},
        "push_source_preparse_json": str(source_preparse_json),
        "push_source_preparse_sha256": source_preparse_sha256,
        "push_loaded_at": datetime.now().isoformat(timespec="seconds"),
    }

    return {
        "meeting_id": meeting_id,
        "source_id": source_id,
        "source_lane": source_lane,
        "jurisdiction": str(payload.get("jurisdiction") or JURISDICTION_DEFAULT),
        "meeting_type": meeting_type,
        "meeting_date": meeting_date,
        "meeting_time": None,
        "location": None,
        "content_mode": content_mode,
        "is_complete_minutes_document": is_complete,
        "linked_source_pdf_code": clean_text(payload.get("linked_source_pdf_code")),
        "metadata": metadata,
    }


def derive_excerpt_rows(payload: dict, meeting_id: str) -> list[dict]:
    excerpts = payload.get("minutes_excerpts")
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
                "page_number": ex.get("page_number"),
                "start_line": ex.get("start_line"),
                "end_line": ex.get("end_line"),
                "source_method": ex.get("source_method"),
            }
        }
        out.append(
            {
                "excerpt_row_id": f"{meeting_id}.{excerpt_id}",
                "meeting_id": meeting_id,
                "excerpt_id": excerpt_id,
                "ordinal": idx,
                "kind": clean_text(ex.get("kind")),
                "page_number": to_int(ex.get("page_number")),
                "start_line": to_int(ex.get("start_line")),
                "end_line": to_int(ex.get("end_line")),
                "source_method": clean_text(ex.get("source_method")),
                "content": content,
                "content_sha256": content_sha256,
                "signals": signals,
                "metadata": metadata,
            }
        )
    return out


def upsert_meeting(cur: Any, row: dict) -> None:
    cur.execute(
        """
        INSERT INTO m1_minutes.meetings (
            meeting_id, source_id, source_lane, jurisdiction, meeting_type, meeting_date,
            meeting_time, location, content_mode, is_complete_minutes_document,
            linked_source_pdf_code, metadata
        )
        VALUES (
            %(meeting_id)s, %(source_id)s, %(source_lane)s, %(jurisdiction)s, %(meeting_type)s, %(meeting_date)s,
            %(meeting_time)s, %(location)s, %(content_mode)s, %(is_complete_minutes_document)s,
            %(linked_source_pdf_code)s, %(metadata)s::jsonb
        )
        ON CONFLICT (meeting_id) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            source_lane = EXCLUDED.source_lane,
            jurisdiction = EXCLUDED.jurisdiction,
            meeting_type = EXCLUDED.meeting_type,
            meeting_date = EXCLUDED.meeting_date,
            meeting_time = EXCLUDED.meeting_time,
            location = EXCLUDED.location,
            content_mode = EXCLUDED.content_mode,
            is_complete_minutes_document = EXCLUDED.is_complete_minutes_document,
            linked_source_pdf_code = EXCLUDED.linked_source_pdf_code,
            metadata = EXCLUDED.metadata,
            updated_at = CURRENT_TIMESTAMP;
        """,
        {
            **row,
            "metadata": json.dumps(row["metadata"], ensure_ascii=True),
        },
    )


def replace_excerpts(cur: Any, meeting_id: str, excerpt_rows: list[dict]) -> None:
    cur.execute("DELETE FROM m1_minutes.excerpts WHERE meeting_id = %s", (meeting_id,))
    if not excerpt_rows:
        return
    for row in excerpt_rows:
        cur.execute(
            """
            INSERT INTO m1_minutes.excerpts (
                excerpt_row_id, meeting_id, excerpt_id, ordinal, kind, page_number,
                start_line, end_line, source_method, content, content_sha256, signals, metadata
            )
            VALUES (
                %(excerpt_row_id)s, %(meeting_id)s, %(excerpt_id)s, %(ordinal)s, %(kind)s, %(page_number)s,
                %(start_line)s, %(end_line)s, %(source_method)s, %(content)s, %(content_sha256)s, %(signals)s::jsonb, %(metadata)s::jsonb
            )
            ON CONFLICT (excerpt_row_id) DO UPDATE SET
                meeting_id = EXCLUDED.meeting_id,
                excerpt_id = EXCLUDED.excerpt_id,
                ordinal = EXCLUDED.ordinal,
                kind = EXCLUDED.kind,
                page_number = EXCLUDED.page_number,
                start_line = EXCLUDED.start_line,
                end_line = EXCLUDED.end_line,
                source_method = EXCLUDED.source_method,
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


def run_push(limit: int | None = None, force: bool = False, dry_run: bool = False) -> dict:
    run_id = datetime.now().strftime("RUN_%Y%m%dT%H%M%S")
    started_at = datetime.now().isoformat(timespec="seconds")

    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = iter_preparse_jsons()
    discovered = len(candidates)

    pushed = 0
    skipped_unchanged = 0
    failed = 0
    run_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    if not dry_run:
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        run_dir = RUNS_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = RUNS_ROOT / run_id

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
        for preparse_json in candidates:
            if limit is not None and pushed >= limit:
                break

            minutes_code = preparse_json.parent.name
            try:
                source_text = preparse_json.read_text(encoding="utf-8")
                source_preparse_sha256 = sha256_text(source_text)
                payload = json.loads(source_text)
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

            if str(payload.get("schema_version") or "") != SOURCE_SCHEMA_VERSION:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "minutes_code": minutes_code,
                        "source_preparse_json": str(preparse_json),
                        "error": f"unsupported_schema_version: {payload.get('schema_version')}",
                    }
                )
                continue

            meeting_row = derive_meeting_row(payload, minutes_code, preparse_json, source_preparse_sha256)
            meeting_id = meeting_row["meeting_id"]
            excerpt_rows = derive_excerpt_rows(payload, meeting_id)

            prev = state_records.get(meeting_id, {})
            if (
                not force
                and prev.get("source_preparse_sha256") == source_preparse_sha256
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
                upsert_meeting(cur, meeting_row)
                replace_excerpts(cur, meeting_id, excerpt_rows)
                conn.commit()

                pushed += 1
                row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "meeting_id": meeting_id,
                    "source_id": meeting_row["source_id"],
                    "source_lane": meeting_row["source_lane"],
                    "meeting_date": meeting_row["meeting_date"],
                    "meeting_type": meeting_row["meeting_type"],
                    "content_mode": meeting_row["content_mode"],
                    "is_complete_minutes_document": meeting_row["is_complete_minutes_document"],
                    "source_preparse_json": str(preparse_json),
                    "source_preparse_sha256": source_preparse_sha256,
                    "excerpts_count": len(excerpt_rows),
                    "db_schema": TARGET_SCHEMA,
                }
                run_rows.append(row)
                append_manifest_rows([row])

                state_records[meeting_id] = {
                    "last_run_id": run_id,
                    "last_status": "pushed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source_preparse_json": str(preparse_json),
                    "source_preparse_sha256": source_preparse_sha256,
                    "meeting_id": meeting_id,
                    "source_lane": meeting_row["source_lane"],
                    "excerpts_count": len(excerpt_rows),
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
                        "meeting_id": meeting_id,
                        "minutes_code": minutes_code,
                        "source_preparse_json": str(preparse_json),
                        "error": str(exc),
                    }
                )
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

    if not dry_run:
        run_manifest = run_dir / "minutes_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "minutes_push_failures.jsonl"
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
            "discovered_records": discovered,
            "pushed_records": pushed,
            "skipped_unchanged": skipped_unchanged,
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
        "skipped_unchanged": skipped_unchanged,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 60)
    print("MINUTES PUSH SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Target schema: {summary['target_schema']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Pushed records: {summary['pushed_records']}")
    print(f"  Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"  Failed: {summary['failed']}")
    if dry_run:
        print("  Dry run: yes (no DB writes)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 60)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Push minutes preparse output records into m1_minutes tables.")
    parser.add_argument("--limit", type=int, default=None, help="Push first N minutes records.")
    parser.add_argument("--force", action="store_true", help="Re-push even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write to DB.")
    args = parser.parse_args()

    run_push(limit=args.limit, force=args.force, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
