#!/usr/bin/env python
"""
Transcript PUSH (DB Loader)

Loads transcript output JSON records from `_Sources/M1-Meetings/Transcripts/_output`
into `m1_transcript` tables:
  - m1_transcript.meetings
  - m1_transcript.turns

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
RUNS_ROOT = OUTPUT_ROOT / "_runs"

DISPOSITION_STATE_FILE = TRANSCRIPTS_ROOT / "transcript_disposition_state.json"
STATE_FILE = TRANSCRIPTS_ROOT / "transcript_push_state.json"
MANIFEST_FILE = TRANSCRIPTS_ROOT / "M1_TS_PUSH_MANIFEST.jsonl"

SOURCE_SCHEMA_VERSION = "m1.transcript.output.v1"
PUSH_SCHEMA_VERSION = "m1.transcript.push.v1"
SOURCE_LANE = "transcript_output"
TARGET_SCHEMA = "m1_transcript"

TS_JSON_RE = re.compile(r"^(M1\.TS\.\d{6}\.[A-Za-z0-9_-]+\.\d{8})\.json$", re.IGNORECASE)

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


def iter_transcript_jsons() -> list[Path]:
    if not OUTPUT_ROOT.exists():
        return []
    files: list[Path] = []
    for p in sorted(OUTPUT_ROOT.glob("M1.TS.*.json")):
        if TS_JSON_RE.match(p.name):
            files.append(p)
    return files


def parse_dispositions(raw: str) -> set[str]:
    vals = set()
    for token in (raw or "").split(","):
        t = token.strip().upper()
        if t:
            vals.add(t)
    return vals or {"OK95"}


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


def meeting_date_from_machine_code(machine_code: str) -> str | None:
    parts = machine_code.split(".")
    if not parts:
        return None
    tail = parts[-1]
    if re.fullmatch(r"\d{8}", tail):
        try:
            return datetime.strptime(tail, "%Y%m%d").date().isoformat()
        except Exception:
            return None
    return None


def parse_timestamp(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.isoformat()
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


def build_transcript_text(turns: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = normalize_ws(str(turn.get("text") or ""))
        if text:
            lines.append(text)
    return "\n\n".join(lines)


def ensure_target_tables(cur: Any) -> None:
    cur.execute("SELECT to_regclass('m1_transcript.meetings'), to_regclass('m1_transcript.turns')")
    meetings_reg, turns_reg = cur.fetchone()
    if not meetings_reg or not turns_reg:
        raise RuntimeError(
            "Target transcript tables missing. Apply _Infra/DATABASE/init/014_transcript_schema.sql before PUSH."
        )


def derive_meeting_row(
    machine_code: str,
    payload: dict[str, Any],
    source_output_json: Path,
    source_output_sha256: str,
    disposition_code: str,
    machine_code_with_disposition: str,
    disposition_record: dict[str, Any],
) -> dict[str, Any]:
    qa_metrics = payload.get("qa_metrics") if isinstance(payload.get("qa_metrics"), dict) else {}
    quoter_metrics = payload.get("quoter_metrics") if isinstance(payload.get("quoter_metrics"), dict) else {}
    roster = payload.get("roster") if isinstance(payload.get("roster"), dict) else {}
    turns = payload.get("turns") if isinstance(payload.get("turns"), list) else []

    transcript_text = build_transcript_text(turns)
    transcript_text_sha256 = sha256_text(transcript_text)

    qa_squeezed_ratio = to_float(qa_metrics.get("squeezed_coverage_ratio"))
    qa_unknown_ratio = to_float(qa_metrics.get("unknown_ratio"))
    total_turns = to_int(qa_metrics.get("total_turns"))
    if total_turns is None:
        total_turns = len(turns)

    parts = machine_code.split(".")
    locality_code = parts[3] if len(parts) >= 4 else None

    metadata = {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "source_lane": SOURCE_LANE,
        "source_output_json": str(source_output_json),
        "source_output_sha256": source_output_sha256,
        "locality_code": locality_code,
        "disposition_reason": disposition_record.get("reason"),
        "disposition_updated_at": disposition_record.get("updated_at"),
        "glossary_hover": payload.get("glossary_hover") if isinstance(payload.get("glossary_hover"), dict) else {},
        "push_loaded_at": datetime.now().isoformat(timespec="seconds"),
    }

    return {
        "meeting_id": machine_code,
        "source_id": machine_code_with_disposition,
        "source_lane": SOURCE_LANE,
        "jurisdiction": locality_code,
        "meeting_date": meeting_date_from_machine_code(machine_code),
        "disposition_code": disposition_code,
        "pass_95_gate": disposition_code == "OK95",
        "qa_squeezed_coverage_ratio": qa_squeezed_ratio,
        "qa_unknown_ratio": qa_unknown_ratio,
        "total_turns": total_turns,
        "transcript_text": transcript_text,
        "transcript_text_sha256": transcript_text_sha256,
        "processed_at": parse_timestamp(payload.get("processed_at")),
        "completed_at": parse_timestamp(payload.get("completed_at")),
        "roster": roster,
        "qa_metrics": qa_metrics,
        "quoter_metrics": quoter_metrics,
        "metadata": metadata,
    }


def derive_turn_rows(meeting_id: str, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_turn_ids: set[int] = set()

    for idx, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue

        raw_turn_id = to_int(turn.get("turn_id"))
        if raw_turn_id is None or raw_turn_id in seen_turn_ids:
            raw_turn_id = idx
        seen_turn_ids.add(raw_turn_id)

        speaker = turn.get("speaker") if isinstance(turn.get("speaker"), dict) else {}
        content = str(turn.get("text") or "")
        phase = str(turn.get("phase") or "").strip() or None
        speaker_role = str(speaker.get("role") or "").strip() or None
        speaker_name = str(speaker.get("name") or "").strip() or None
        speaker_confidence = to_float(speaker.get("confidence"))

        metadata = {}
        if "quoter_pass" in turn:
            metadata["quoter_pass"] = turn.get("quoter_pass")

        rows.append(
            {
                "turn_row_id": f"{meeting_id}.{raw_turn_id}",
                "meeting_id": meeting_id,
                "turn_id": raw_turn_id,
                "ordinal": idx,
                "phase": phase,
                "speaker_role": speaker_role,
                "speaker_name": speaker_name,
                "speaker_confidence": speaker_confidence,
                "content": content,
                "content_sha256": sha256_text(content),
                "metadata": metadata,
            }
        )

    return rows


def upsert_meeting(cur: Any, row: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO m1_transcript.meetings (
            meeting_id, source_id, source_lane, jurisdiction, meeting_date,
            disposition_code, pass_95_gate, qa_squeezed_coverage_ratio, qa_unknown_ratio, total_turns,
            transcript_text, transcript_text_sha256, processed_at, completed_at,
            roster, qa_metrics, quoter_metrics, metadata
        )
        VALUES (
            %(meeting_id)s, %(source_id)s, %(source_lane)s, %(jurisdiction)s, %(meeting_date)s,
            %(disposition_code)s, %(pass_95_gate)s, %(qa_squeezed_coverage_ratio)s, %(qa_unknown_ratio)s, %(total_turns)s,
            %(transcript_text)s, %(transcript_text_sha256)s, %(processed_at)s, %(completed_at)s,
            %(roster)s::jsonb, %(qa_metrics)s::jsonb, %(quoter_metrics)s::jsonb, %(metadata)s::jsonb
        )
        ON CONFLICT (meeting_id) DO UPDATE SET
            source_id = EXCLUDED.source_id,
            source_lane = EXCLUDED.source_lane,
            jurisdiction = EXCLUDED.jurisdiction,
            meeting_date = EXCLUDED.meeting_date,
            disposition_code = EXCLUDED.disposition_code,
            pass_95_gate = EXCLUDED.pass_95_gate,
            qa_squeezed_coverage_ratio = EXCLUDED.qa_squeezed_coverage_ratio,
            qa_unknown_ratio = EXCLUDED.qa_unknown_ratio,
            total_turns = EXCLUDED.total_turns,
            transcript_text = EXCLUDED.transcript_text,
            transcript_text_sha256 = EXCLUDED.transcript_text_sha256,
            processed_at = EXCLUDED.processed_at,
            completed_at = EXCLUDED.completed_at,
            roster = EXCLUDED.roster,
            qa_metrics = EXCLUDED.qa_metrics,
            quoter_metrics = EXCLUDED.quoter_metrics,
            metadata = EXCLUDED.metadata,
            updated_at = CURRENT_TIMESTAMP;
        """,
        {
            **row,
            "roster": json.dumps(row["roster"], ensure_ascii=True),
            "qa_metrics": json.dumps(row["qa_metrics"], ensure_ascii=True),
            "quoter_metrics": json.dumps(row["quoter_metrics"], ensure_ascii=True),
            "metadata": json.dumps(row["metadata"], ensure_ascii=True),
        },
    )


def replace_turns(cur: Any, meeting_id: str, turn_rows: list[dict[str, Any]]) -> None:
    cur.execute("DELETE FROM m1_transcript.turns WHERE meeting_id = %s", (meeting_id,))
    if not turn_rows:
        return
    for row in turn_rows:
        cur.execute(
            """
            INSERT INTO m1_transcript.turns (
                turn_row_id, meeting_id, turn_id, ordinal, phase,
                speaker_role, speaker_name, speaker_confidence,
                content, content_sha256, metadata
            )
            VALUES (
                %(turn_row_id)s, %(meeting_id)s, %(turn_id)s, %(ordinal)s, %(phase)s,
                %(speaker_role)s, %(speaker_name)s, %(speaker_confidence)s,
                %(content)s, %(content_sha256)s, %(metadata)s::jsonb
            )
            ON CONFLICT (turn_row_id) DO UPDATE SET
                meeting_id = EXCLUDED.meeting_id,
                turn_id = EXCLUDED.turn_id,
                ordinal = EXCLUDED.ordinal,
                phase = EXCLUDED.phase,
                speaker_role = EXCLUDED.speaker_role,
                speaker_name = EXCLUDED.speaker_name,
                speaker_confidence = EXCLUDED.speaker_confidence,
                content = EXCLUDED.content,
                content_sha256 = EXCLUDED.content_sha256,
                metadata = EXCLUDED.metadata;
            """,
            {
                **row,
                "metadata": json.dumps(row["metadata"], ensure_ascii=True),
            },
        )


def run_push(limit: int | None, force: bool, dry_run: bool, allowed_dispositions: set[str]) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    run_id = f"RUN-TS-PUSH-{datetime.now().strftime('%Y%m%dT%H%M%S')}"
    run_dir = RUNS_ROOT / run_id

    disposition_state = load_disposition_records()
    state = load_state()
    state_records = state.setdefault("records", {})

    candidates = iter_transcript_jsons()
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

            m = TS_JSON_RE.match(path.name)
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
                        "meeting_id": machine_code,
                        "source_output_json": str(path),
                        "error": f"invalid_output_json: {exc}",
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

            prev = state_records.get(machine_code, {})
            if (
                not force
                and prev.get("source_output_sha256") == source_sha256
                and str(prev.get("last_status") or "") == "pushed"
            ):
                skipped_unchanged += 1
                continue

            meeting_row = derive_meeting_row(
                machine_code=machine_code,
                payload=payload,
                source_output_json=path,
                source_output_sha256=source_sha256,
                disposition_code=disposition_code,
                machine_code_with_disposition=machine_code_with_disposition,
                disposition_record=disp_rec,
            )
            turns_payload = payload.get("turns") if isinstance(payload.get("turns"), list) else []
            turn_rows = derive_turn_rows(machine_id := meeting_row["meeting_id"], turns_payload)

            if dry_run:
                pushed += 1
                continue

            try:
                assert cur is not None
                assert conn is not None
                upsert_meeting(cur, meeting_row)
                replace_turns(cur, machine_id, turn_rows)
                conn.commit()

                pushed += 1
                row = {
                    "run_id": run_id,
                    "pushed_at": datetime.now().isoformat(timespec="seconds"),
                    "schema_version": PUSH_SCHEMA_VERSION,
                    "meeting_id": meeting_row["meeting_id"],
                    "source_id": meeting_row["source_id"],
                    "source_lane": meeting_row["source_lane"],
                    "disposition_code": meeting_row["disposition_code"],
                    "pass_95_gate": meeting_row["pass_95_gate"],
                    "meeting_date": meeting_row["meeting_date"],
                    "source_output_json": str(path),
                    "source_output_sha256": source_sha256,
                    "turns_count": len(turn_rows),
                    "qa_squeezed_coverage_ratio": meeting_row["qa_squeezed_coverage_ratio"],
                    "qa_unknown_ratio": meeting_row["qa_unknown_ratio"],
                    "db_schema": TARGET_SCHEMA,
                }
                run_rows.append(row)
                append_manifest_rows([row])

                state_records[machine_code] = {
                    "last_run_id": run_id,
                    "last_status": "pushed",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source_output_json": str(path),
                    "source_output_sha256": source_sha256,
                    "meeting_id": meeting_row["meeting_id"],
                    "source_id": meeting_row["source_id"],
                    "disposition_code": meeting_row["disposition_code"],
                    "turns_count": len(turn_rows),
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
                        "meeting_id": machine_code,
                        "source_output_json": str(path),
                        "error": str(exc),
                    }
                )
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

    if not dry_run:
        run_manifest = run_dir / "transcript_push_manifest.jsonl"
        with run_manifest.open("w", encoding="utf-8") as f:
            for row in run_rows:
                f.write(json.dumps(row, ensure_ascii=True) + "\n")

        if failure_rows:
            failure_out = run_dir / "transcript_push_failures.jsonl"
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
    print("TRANSCRIPT PUSH SUMMARY")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Push transcript output records into m1_transcript tables.")
    parser.add_argument("--limit", type=int, default=None, help="Push first N transcript records.")
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
