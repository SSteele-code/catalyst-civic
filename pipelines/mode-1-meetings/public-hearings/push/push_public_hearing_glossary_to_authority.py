#!/usr/bin/env python
"""
Public Hearings PUSH Part 1 (Glossary Authority Loader)

Loads glossary entities from Public Hearings PARSE outputs and pushes them into:
  - cco.registry
  - cco.identities
  - cco.observations

Strict invariant:
  - Glossary authority load only
  - No writes to m1_public_hearing notice/excerpt tables
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
        "or run with a Python interpreter that has psycopg2."
    ) from exc


PUBLIC_HEARING_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Public_Hearings"
OUTPUT_ROOT = PUBLIC_HEARING_ROOT / "_output"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

STATE_FILE = PUBLIC_HEARING_ROOT / "public_hearing_glossary_push_state.json"
MANIFEST_FILE = PUBLIC_HEARING_ROOT / "M1_PUBLIC_HEARING_GLOSSARY_PUSH_MANIFEST.jsonl"
PARSE_STATE_FILE = PUBLIC_HEARING_ROOT / "public_hearing_preparse_state.json"

SOURCE_SCHEMA_VERSION = "m1.public_hearing.parse.v1"
SOURCE_SCHEMA_VERSION_COMPAT = "m1.public_hearing.preparse.v1"
SOURCE_SCHEMA_VERSIONS = {SOURCE_SCHEMA_VERSION, SOURCE_SCHEMA_VERSION_COMPAT}
SOURCE_GLOSSARY_SCHEMA_VERSION = "m1.public_hearing.glossary.v1"
PUSH_SCHEMA_VERSION = "m1.public_hearing.glossary_push.v1"
SOURCE_LANE = "public_hearing_parse_glossary"
TARGET_SCHEMA = "cco"

NOTICE_DIR_RE = re.compile(r"^M1\.AG\.PH\.\d{6}\.\d{8}\.\d{8}$", re.IGNORECASE)

# DB config
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_name(name: str) -> str:
    return normalize_ws(str(name or "")).strip(" ,;:.|-")


def generate_id(category: str, name: str) -> str:
    normalized = normalize_ws(name)
    safe = re.sub(r"[^A-Za-z0-9]", "_", normalized.upper()).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return f"{category.upper()}_{safe}"


def map_category(source_category: str) -> str:
    src = str(source_category or "").upper().strip()
    if src == "PERSON":
        return "PEOPLE"
    if src in {"PEOPLE", "ORGANIZATION", "BOARD", "AGENCY", "LOCATION", "LAW", "LEGAL_REFERENCE"}:
        return src
    return src or "UNKNOWN"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    PUBLIC_HEARING_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    PUBLIC_HEARING_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def iter_parse_jsons() -> list[Path]:
    if not OUTPUT_ROOT.exists():
        return []
    out: list[Path] = []
    for d in sorted(OUTPUT_ROOT.iterdir()):
        if not d.is_dir() or not NOTICE_DIR_RE.match(d.name):
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
    run_ids: list[str] = []
    for rec in records.values():
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("last_run_id") or "").strip()
        if rid.upper().startswith("RUN_"):
            run_ids.append(rid)
    if not run_ids:
        return None
    run_ids.sort(reverse=True)
    return run_ids[0]


def ensure_target_tables(cur: Any) -> None:
    cur.execute(
        "SELECT to_regclass('cco.registry'), to_regclass('cco.identities'), to_regclass('cco.observations')"
    )
    registry_reg, identities_reg, observations_reg = cur.fetchone()
    if not registry_reg or not identities_reg or not observations_reg:
        raise RuntimeError(
            "Target CCO tables missing. Apply _Infra/DATABASE/init/012_industrial_glossary.sql before PUSH."
        )


def effective_date_from_payload(payload: dict[str, Any]) -> str | None:
    context = payload.get("meeting_context") if isinstance(payload.get("meeting_context"), dict) else {}
    date_value = str(context.get("anchor_meeting_date") or "").strip()
    if not date_value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
        return date_value
    return None


def compact_observation_fact(entity: dict[str, Any], source_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": PUSH_SCHEMA_VERSION,
        "source_schema_version": str(source_payload.get("schema_version") or ""),
        "source_record_type": str(source_payload.get("record_type") or ""),
        "source_lane": SOURCE_LANE,
        "source_public_hearing_code": str(source_payload.get("public_hearing_code") or ""),
        "entity_id": str(entity.get("entry_id") or ""),
        "source_category": str(entity.get("category") or ""),
        "source_fact_key": str(entity.get("fact_key") or ""),
        "confidence": float(entity.get("confidence") or 0.0),
        "match_type": str(entity.get("matched_from") or ""),
        "source_span": entity.get("source_span") if isinstance(entity.get("source_span"), dict) else {},
    }


def upsert_entity(
    cur: Any,
    source_id: str,
    effective_date: str | None,
    entity: dict[str, Any],
    source_payload: dict[str, Any],
) -> bool:
    category = map_category(str(entity.get("category") or ""))
    canonical_name = clean_name(str(entity.get("canonical_name") or ""))
    if not canonical_name:
        return False

    registry_id = generate_id(category, canonical_name)
    cur.execute(
        """
        INSERT INTO cco.registry (registry_id, category, canonical_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (registry_id) DO UPDATE
        SET category = EXCLUDED.category,
            canonical_name = EXCLUDED.canonical_name,
            updated_at = CURRENT_TIMESTAMP
        """,
        (registry_id, category, canonical_name),
    )

    cur.execute(
        """
        INSERT INTO cco.identities (registry_id, alias_name, source_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (registry_id, alias_name) DO NOTHING
        """,
        (registry_id, canonical_name, source_id),
    )

    fact_key = clean_name(str(entity.get("fact_key") or "MENTIONED_IN_RECORD")).upper()
    evidence = normalize_ws(str(entity.get("evidence") or ""))[:500]
    if not evidence:
        evidence = canonical_name
    fact_value = compact_observation_fact(entity, source_payload)

    cur.execute(
        """
        SELECT 1
        FROM cco.observations
        WHERE registry_id = %s AND source_id = %s AND fact_key = %s AND evidence = %s
        LIMIT 1
        """,
        (registry_id, source_id, fact_key, evidence),
    )
    if cur.fetchone() is not None:
        return False

    cur.execute(
        """
        INSERT INTO cco.observations (registry_id, fact_key, fact_value, source_id, evidence, effective_date)
        VALUES (%s, %s, %s::jsonb, %s, %s, %s)
        """,
        (
            registry_id,
            fact_key,
            json.dumps(fact_value, ensure_ascii=True),
            source_id,
            evidence,
            effective_date,
        ),
    )
    return True


def run_push(
    limit: int | None,
    force: bool,
    dry_run: bool,
    source_run_id: str | None,
    all_output: bool,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    run_id = f"RUN-PH-GLOSSARY-PUSH-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    run_dir = RUNS_ROOT / run_id

    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = iter_parse_jsons()
    discovered = len(candidates)
    pushed_records = 0
    pushed_entities = 0
    skipped_unchanged = 0
    skipped_source_scope = 0
    failed = 0
    run_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

    effective_source_run_id = source_run_id or latest_parse_run_id_from_state()

    conn = None
    cur = None
    if not dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            database=PG_DB,
            user=PG_USER,
            password=PG_PASS,
        )
        cur = conn.cursor()
        ensure_target_tables(cur)

    try:
        for parse_json in candidates:
            if limit is not None and pushed_records >= limit:
                break

            public_hearing_code = parse_json.parent.name
            try:
                source_text = parse_json.read_text(encoding="utf-8")
                source_sha256 = sha256_text(source_text)
                payload = json.loads(source_text)
            except Exception as exc:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "public_hearing_code": public_hearing_code,
                        "source_parse_json": str(parse_json),
                        "error": f"invalid_parse_json: {exc}",
                    }
                )
                continue

            source_schema_version = str(payload.get("schema_version") or "")
            if source_schema_version not in SOURCE_SCHEMA_VERSIONS:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "public_hearing_code": public_hearing_code,
                        "source_parse_json": str(parse_json),
                        "error": f"unsupported_schema_version: {source_schema_version}",
                    }
                )
                continue

            glossary = payload.get("glossary") if isinstance(payload.get("glossary"), dict) else {}
            glossary_schema_version = str(glossary.get("schema_version") or "")
            entities = glossary.get("entities")
            if not isinstance(entities, list):
                entities = []
            payload_run_id = str(payload.get("parse_run_id") or payload.get("preparse_run_id") or "").strip()

            if not all_output and effective_source_run_id:
                if payload_run_id != effective_source_run_id:
                    skipped_source_scope += 1
                    continue

            prev = state_records.get(public_hearing_code, {})
            if (
                not force
                and prev.get("source_parse_sha256") == source_sha256
                and str(prev.get("last_status") or "") == "pushed"
            ):
                skipped_unchanged += 1
                continue

            source_id = str(payload.get("public_hearing_code") or public_hearing_code).strip()
            effective_date = effective_date_from_payload(payload)

            if dry_run:
                pushed_records += 1
                pushed_entities += len(entities)
                continue

            try:
                assert cur is not None
                assert conn is not None
                record_push_count = 0
                for entity in entities:
                    if not isinstance(entity, dict):
                        continue
                    inserted = upsert_entity(
                        cur=cur,
                        source_id=source_id,
                        effective_date=effective_date,
                        entity=entity,
                        source_payload=payload,
                    )
                    if inserted:
                        record_push_count += 1

                conn.commit()
                pushed_records += 1
                pushed_entities += record_push_count

                row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "source_schema_version": source_schema_version,
                    "source_glossary_schema_version": glossary_schema_version,
                    "public_hearing_code": public_hearing_code,
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_sha256,
                    "entities_total": len(entities),
                    "entities_pushed": record_push_count,
                    "db_schema": TARGET_SCHEMA,
                }
                run_rows.append(row)
                append_manifest_rows([row])

                state_records[public_hearing_code] = {
                    "last_run_id": run_id,
                    "last_status": "pushed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source_parse_json": str(parse_json),
                    "source_parse_sha256": source_sha256,
                    "entities_total": len(entities),
                    "entities_pushed": record_push_count,
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
                        "public_hearing_code": public_hearing_code,
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
        run_manifest = run_dir / "public_hearing_glossary_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "public_hearing_glossary_push_failures.jsonl"
            with failure_out.open("w", encoding="utf-8") as f:
                for row in failure_rows:
                    f.write(json.dumps(row, ensure_ascii=True) + "\n")

        run_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": PUSH_SCHEMA_VERSION,
            "source_schema_versions": sorted(SOURCE_SCHEMA_VERSIONS),
            "source_glossary_schema_version": SOURCE_GLOSSARY_SCHEMA_VERSION,
            "target_schema": TARGET_SCHEMA,
            "source_lane": SOURCE_LANE,
            "source_run_scope": effective_source_run_id if not all_output else "ALL_OUTPUT",
            "discovered_records": discovered,
            "pushed_records": pushed_records,
            "pushed_entities": pushed_entities,
            "skipped_unchanged": skipped_unchanged,
            "skipped_source_scope": skipped_source_scope,
            "failed": failed,
            "limit": limit,
            "force": force,
            "pg_host": PG_HOST,
            "pg_port": PG_PORT,
            "pg_db": PG_DB,
        }
        (run_dir / "run_summary.json").write_text(
            json.dumps(run_summary, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    summary = {
        "run_id": run_id,
        "target_schema": TARGET_SCHEMA,
        "discovered_records": discovered,
        "pushed_records": pushed_records,
        "pushed_entities": pushed_entities,
        "skipped_unchanged": skipped_unchanged,
        "skipped_source_scope": skipped_source_scope,
        "failed": failed,
        "dry_run": dry_run,
    }
    print("=" * 72)
    print("PUBLIC HEARING GLOSSARY PUSH SUMMARY (PART 1)")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Target schema: {summary['target_schema']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Records pushed: {summary['pushed_records']}")
    print(f"  Entities pushed: {summary['pushed_entities']}")
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
    parser = argparse.ArgumentParser(description="Push Public Hearings glossary sections into CCO authority tables.")
    parser.add_argument("--limit", type=int, default=None, help="Push first N Public Hearings records.")
    parser.add_argument("--force", action="store_true", help="Re-push even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write to DB.")
    parser.add_argument(
        "--source-run-id",
        type=str,
        default=None,
        help="Restrict to a specific parse run id (for example RUN_20260510T211211). Default: latest parse run.",
    )
    parser.add_argument(
        "--all-output",
        action="store_true",
        help="Process all output records across runs (ignore parse run scoping).",
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
