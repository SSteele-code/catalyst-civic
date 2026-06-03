#!/usr/bin/env python
"""
Transcript Glossary PUSH (Final Authority Loader)

Loads transcript glossary-hover candidate records from
`_Sources/M1-Meetings/Transcripts/_output/_glossary`
into final authority tables:
  - cco.registry
  - cco.identities
  - cco.observations

Default policy:
  - Push only records marked `OK95` in transcript disposition state.
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


TRANSCRIPTS_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts")
OUTPUT_ROOT = TRANSCRIPTS_ROOT / "_output"
GLOSSARY_ROOT = OUTPUT_ROOT / "_glossary"
RUNS_ROOT = OUTPUT_ROOT / "_runs"

DISPOSITION_STATE_FILE = TRANSCRIPTS_ROOT / "transcript_disposition_state.json"
STATE_FILE = TRANSCRIPTS_ROOT / "transcript_glossary_push_state.json"
MANIFEST_FILE = TRANSCRIPTS_ROOT / "M1_TS_GLOSSARY_PUSH_MANIFEST.jsonl"

SOURCE_SCHEMA_VERSION = "m1.transcript.glossary_hover.v1"
PUSH_SCHEMA_VERSION = "m1.transcript.glossary_push.v1"
SOURCE_LANE = "transcript_output_glossary_hover"
TARGET_SCHEMA = "cco"

GLOSSARY_JSON_RE = re.compile(
    r"^(M1\.TS\.\d{6}\.[A-Za-z0-9_-]+\.\d{8})\.glossary_hover\.json$",
    re.IGNORECASE,
)

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
    safe = re.sub(r"[^a-zA-Z0-9]", "_", normalized.upper()).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return f"{category.upper()}_{safe}"


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


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"records": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}}


def save_state(state: dict) -> None:
    TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def append_manifest_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST_FILE.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")


def load_disposition_records() -> dict[str, dict[str, Any]]:
    if not DISPOSITION_STATE_FILE.exists():
        return {}
    try:
        payload = json.loads(DISPOSITION_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    recs = payload.get("records")
    if isinstance(recs, dict):
        return recs
    return {}


def resolve_disposition(
    machine_code: str,
    payload: dict[str, Any],
    disposition_record: dict[str, Any],
) -> tuple[str, str]:
    state_code = str(disposition_record.get("disposition_code") or "").upper().strip()
    state_code_with = str(disposition_record.get("machine_code_with_disposition") or "").strip()
    if state_code == "SKIP":
        state_code = ""
        state_code_with = ""

    qa = payload.get("qa_metrics") if isinstance(payload.get("qa_metrics"), dict) else {}
    qa_code = str(qa.get("disposition_code") or "").upper().strip()
    qa_code_with = str(qa.get("machine_code_with_disposition") or "").strip()
    if qa_code == "SKIP":
        qa_code = ""
        qa_code_with = ""

    # Glossary payloads do not include qa_metrics, so recover from paired transcript output.
    if not qa_code:
        transcript_json = OUTPUT_ROOT / f"{machine_code}.json"
        if transcript_json.exists():
            try:
                transcript_payload = json.loads(transcript_json.read_text(encoding="utf-8"))
            except Exception:
                transcript_payload = {}
            transcript_qa = (
                transcript_payload.get("qa_metrics")
                if isinstance(transcript_payload.get("qa_metrics"), dict)
                else {}
            )
            qa = transcript_qa or qa
            qa_code = str(transcript_qa.get("disposition_code") or qa_code).upper().strip()
            qa_code_with = str(
                transcript_qa.get("machine_code_with_disposition") or qa_code_with
            ).strip()

    disposition_code = state_code or qa_code
    if not disposition_code:
        coverage = to_float(qa.get("squeezed_coverage_ratio"))
        source_words = to_int(qa.get("source_words_squeezed"))
        total_turns = to_int(qa.get("total_turns"))
        structural_issues = qa.get("structural_issues")
        if (
            isinstance(structural_issues, list)
            and len(structural_issues) == 0
            and source_words is not None
            and source_words > 0
            and total_turns is not None
            and total_turns >= 6
            and coverage is not None
            and coverage >= 0.95
        ):
            disposition_code = "OK95"

    if not disposition_code:
        disposition_code = "UNKNOWN"

    machine_code_with_disposition = (
        state_code_with
        or qa_code_with
        or f"{machine_code}.{disposition_code}"
    )
    return disposition_code, machine_code_with_disposition


def iter_glossary_jsons() -> list[Path]:
    if not GLOSSARY_ROOT.exists():
        return []
    files: list[Path] = []
    for p in sorted(GLOSSARY_ROOT.glob("*.glossary_hover.json")):
        if GLOSSARY_JSON_RE.match(p.name):
            files.append(p)
    return files


def ensure_target_tables(cur: Any) -> None:
    cur.execute(
        "SELECT to_regclass('cco.registry'), to_regclass('cco.identities'), to_regclass('cco.observations')"
    )
    registry_reg, identities_reg, observations_reg = cur.fetchone()
    if not registry_reg or not identities_reg or not observations_reg:
        raise RuntimeError(
            "Target CCO tables missing. Apply _Infra/DATABASE/init/012_industrial_glossary.sql before PUSH."
        )


def map_category(source_category: str) -> str:
    src = str(source_category or "").upper().strip()
    if src == "PERSON":
        return "PEOPLE"
    if src == "ORGANIZATION":
        return "ORGANIZATION"
    if src == "LEGAL_REFERENCE":
        return "LEGAL_REFERENCE"
    return src or "UNKNOWN"


def meeting_date_from_machine_code(machine_code: str) -> str | None:
    parts = machine_code.split(".")
    if not parts:
        return None
    last = parts[-1]
    if re.fullmatch(r"\d{8}", last):
        try:
            return datetime.strptime(last, "%Y%m%d").date().isoformat()
        except Exception:
            return None
    return None


def compact_observation_fact(entity: dict[str, Any], source_payload: dict[str, Any], disposition_code: str) -> dict[str, Any]:
    return {
        "schema_version": PUSH_SCHEMA_VERSION,
        "source_schema_version": str(source_payload.get("schema_version") or ""),
        "source_record_type": str(source_payload.get("record_type") or ""),
        "source_lane": SOURCE_LANE,
        "disposition_code": disposition_code,
        "entity_id": str(entity.get("entity_id") or ""),
        "source_category": str(entity.get("category") or ""),
        "source_fact_key": str(entity.get("fact_key") or ""),
        "confidence": float(entity.get("confidence") or 0.0),
        "match_type": str(entity.get("match_type") or ""),
    }


def upsert_entity(cur: Any, machine_code_with_disposition: str, effective_date: str | None, entity: dict[str, Any], source_payload: dict[str, Any], disposition_code: str) -> None:
    category = map_category(str(entity.get("category") or ""))
    canonical_name = clean_name(str(entity.get("canonical_name") or ""))
    if not canonical_name:
        return

    registry_id = generate_id(category, canonical_name)
    source_id = machine_code_with_disposition

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

    fact_key = "TRANSCRIPT_GLOSSARY_ENTITY"
    fact_value = compact_observation_fact(entity, source_payload, disposition_code)
    evidence = normalize_ws(str(entity.get("evidence") or ""))

    cur.execute(
        """
        SELECT 1
        FROM cco.observations
        WHERE registry_id = %s AND source_id = %s AND fact_key = %s
        LIMIT 1
        """,
        (registry_id, source_id, fact_key),
    )
    if cur.fetchone() is None:
        cur.execute(
            """
            INSERT INTO cco.observations (registry_id, fact_key, fact_value, source_id, evidence, effective_date)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s)
            """,
            (registry_id, fact_key, json.dumps(fact_value, ensure_ascii=True), source_id, evidence, effective_date),
        )


def run_push(limit: int | None, force: bool, dry_run: bool, allowed_dispositions: set[str]) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    run_id = f"RUN-TS-GLOSSARY-PUSH-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    run_dir = RUNS_ROOT / run_id
    disposition_state = load_disposition_records()
    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = iter_glossary_jsons()
    discovered = len(candidates)
    pushed = 0
    skipped_unchanged = 0
    skipped_disposition = 0
    failed = 0
    run_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []

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
        for path in candidates:
            if limit is not None and pushed >= limit:
                break

            m = GLOSSARY_JSON_RE.match(path.name)
            if not m:
                continue
            machine_code = m.group(1)

            disp_rec = disposition_state.get(machine_code, {})

            try:
                source_text = path.read_text(encoding="utf-8")
                source_sha256 = sha256_text(source_text)
                payload = json.loads(source_text)
            except Exception as exc:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "machine_code": machine_code,
                        "source_glossary_json": str(path),
                        "error": f"invalid_glossary_json: {exc}",
                    }
                )
                continue

            disposition_code, machine_code_with_disposition = resolve_disposition(
                machine_code=machine_code,
                payload=payload,
                disposition_record=disp_rec,
            )
            if disposition_code not in allowed_dispositions:
                skipped_disposition += 1
                continue

            if str(payload.get("schema_version") or "") != SOURCE_SCHEMA_VERSION:
                failed += 1
                failure_rows.append(
                    {
                        "run_id": run_id,
                        "failed_at": datetime.now().isoformat(timespec="seconds"),
                        "machine_code": machine_code,
                        "source_glossary_json": str(path),
                        "error": f"unsupported_schema_version: {payload.get('schema_version')}",
                    }
                )
                continue

            prev = state_records.get(machine_code, {})
            if (
                not force
                and prev.get("source_glossary_sha256") == source_sha256
                and str(prev.get("last_status") or "") == "pushed"
            ):
                skipped_unchanged += 1
                continue

            entities = payload.get("glossary_entities")
            if not isinstance(entities, list):
                entities = []
            effective_date = meeting_date_from_machine_code(machine_code)

            if dry_run:
                pushed += 1
                continue

            try:
                assert cur is not None
                assert conn is not None
                push_count = 0
                for entity in entities:
                    if not isinstance(entity, dict):
                        continue
                    upsert_entity(
                        cur=cur,
                        machine_code_with_disposition=machine_code_with_disposition,
                        effective_date=effective_date,
                        entity=entity,
                        source_payload=payload,
                        disposition_code=disposition_code,
                    )
                    push_count += 1

                conn.commit()
                pushed += 1

                row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "source_schema_version": SOURCE_SCHEMA_VERSION,
                    "machine_code": machine_code,
                    "machine_code_with_disposition": machine_code_with_disposition,
                    "disposition_code": disposition_code,
                    "source_glossary_json": str(path),
                    "source_glossary_sha256": source_sha256,
                    "entities_total": len(entities),
                    "entities_pushed": push_count,
                    "db_schema": TARGET_SCHEMA,
                }
                run_rows.append(row)
                append_manifest_rows([row])

                state_records[machine_code] = {
                    "last_run_id": run_id,
                    "last_status": "pushed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "machine_code_with_disposition": machine_code_with_disposition,
                    "disposition_code": disposition_code,
                    "source_glossary_json": str(path),
                    "source_glossary_sha256": source_sha256,
                    "entities_total": len(entities),
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
                        "machine_code": machine_code,
                        "machine_code_with_disposition": machine_code_with_disposition,
                        "source_glossary_json": str(path),
                        "error": str(exc),
                    }
                )
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

    if not dry_run:
        run_manifest = run_dir / "transcript_glossary_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "transcript_glossary_push_failures.jsonl"
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
            "source_lane": SOURCE_LANE,
            "discovered_records": discovered,
            "pushed_records": pushed,
            "skipped_unchanged": skipped_unchanged,
            "skipped_disposition": skipped_disposition,
            "failed": failed,
            "limit": limit,
            "force": force,
            "dry_run": dry_run,
            "allowed_dispositions": sorted(list(allowed_dispositions)),
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
        "skipped_disposition": skipped_disposition,
        "failed": failed,
        "dry_run": dry_run,
    }

    print("=" * 60)
    print("TRANSCRIPT GLOSSARY PUSH SUMMARY")
    print(f"  Run ID: {summary['run_id']}")
    print(f"  Target schema: {summary['target_schema']}")
    print(f"  Records discovered: {summary['discovered_records']}")
    print(f"  Pushed records: {summary['pushed_records']}")
    print(f"  Skipped (unchanged): {summary['skipped_unchanged']}")
    print(f"  Skipped (disposition filter): {summary['skipped_disposition']}")
    print(f"  Failed: {summary['failed']}")
    if dry_run:
        print("  Dry run: yes (no DB writes)")
    else:
        print(f"  Global manifest: {MANIFEST_FILE}")
        print(f"  State file: {STATE_FILE}")
        print(f"  Run artifacts: {run_dir}")
    print("=" * 60)
    return summary


def parse_dispositions(raw: str) -> set[str]:
    vals = set()
    for token in (raw or "").split(","):
        t = token.strip().upper()
        if t:
            vals.add(t)
    return vals or {"OK95"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Push transcript glossary-hover records into CCO final authority tables.")
    parser.add_argument("--limit", type=int, default=None, help="Push first N transcript glossary records.")
    parser.add_argument("--force", action="store_true", help="Re-push even if unchanged by state.")
    parser.add_argument("--dry-run", action="store_true", help="Scan only; do not write to DB.")
    parser.add_argument(
        "--allowed-dispositions",
        default="OK95",
        help="Comma-separated disposition filter (default: OK95). Example: OK95,LOWSIG",
    )
    args = parser.parse_args()

    allowed = parse_dispositions(args.allowed_dispositions)
    run_push(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        allowed_dispositions=allowed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
