import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2


BASE_DIR = Path(__file__).resolve().parent.parent
PULL_DIR = BASE_DIR / "PULL"
PULLER = PULL_DIR / "orchestrator.py"
CONDUCTOR = BASE_DIR / "conductor.py"
PYTHON_EXE = sys.executable
PULSE_COUNTER_FILE = BASE_DIR / "pulse_counter.json"

AGENDAS_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas"
OUTPUT_ROOT = AGENDAS_ROOT / "_output"
VAULT_ROOT = AGENDAS_ROOT / "_vaulted"
PULLER_STATE_FILE = AGENDAS_ROOT / "agenda_state.json"
PULLER_MANIFEST_FILE = AGENDAS_ROOT / "M1_AGENDAS_MANIFEST.jsonl"
MODE_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Modes" / "M1" / "Agenda"
SCHEMA_ROOT = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Schema" / "M1" / "Agenda"

ENGINE_ROOT = BASE_DIR / "PDF_Parser_Engine"
RUNTIME_CLEAN_TARGETS = [
    ENGINE_ROOT / "inbox",
    ENGINE_ROOT / "outbox",
    ENGINE_ROOT / "work",
    ENGINE_ROOT / "manifests",
    ENGINE_ROOT / "logs",
    ENGINE_ROOT / "quarantine",
    AGENDAS_ROOT / "_staging",
]
REPORTS_ROOT = BASE_DIR / "tools" / "reports"

SIGNAL_TERMS = [
    "ordinance",
    "resolution",
    "public hearing",
    "motion",
    "budget",
    "contract",
    "amendment",
    "appropriation",
    "grant",
    "loan",
    "tax",
]
DEFAULT_QUALITY_TARGET = 0.95
DEFAULT_METADATA_TARGET = 0.95
DEFAULT_PULL_TIMEOUT_SECONDS = int(os.getenv("M1_PULL_TIMEOUT_SECONDS", "900"))
DEFAULT_CONDUCTOR_TIMEOUT_SECONDS = int(os.getenv("M1_CONDUCTOR_TIMEOUT_SECONDS", "3600"))
DEFAULT_RESCUE_CONDUCTOR_TIMEOUT_SECONDS = int(os.getenv("M1_RESCUE_CONDUCTOR_TIMEOUT_SECONDS", "3600"))
MISSING_META_VALUES = {"", "unknown", "n/a", "none", "null"}
RESCUE_ERROR_TRIGGERS = {
    "zero_items",
    "metadata_completeness_low",
    "missing_meeting_type",
    "missing_meeting_date",
    "missing_meeting_time",
    "missing_location",
}

STOPWORDS = {
    "about",
    "after",
    "again",
    "agenda",
    "being",
    "between",
    "could",
    "first",
    "from",
    "meeting",
    "minutes",
    "other",
    "shall",
    "should",
    "there",
    "their",
    "these",
    "those",
    "through",
    "under",
    "where",
    "which",
    "while",
    "would",
}


SOURCE_FILENAME_DATE_RE = re.compile(r"^M1\.AG\.\d{6}\.(\d{8})\.\d{8}\.pdf$", re.IGNORECASE)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg: str) -> None:
    print(msg, flush=True)


def kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout_seconds: int | None = None,
) -> tuple[int, str]:
    cmd_str = " ".join(cmd)
    log(f"\n$ {cmd_str}")
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout_seconds)
        output = stdout or ""
        if output:
            print(output, end="")
        return proc.returncode, output
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(proc)
        stdout, _ = proc.communicate()
        timed_output = (exc.stdout or "") + (stdout or "")
        elapsed = int(time.monotonic() - started)
        timeout_msg = (
            f"\n[TIMEOUT] command exceeded {timeout_seconds}s after {elapsed}s "
            f"and was terminated: {cmd_str}\n"
        )
        merged = timed_output + timeout_msg
        if merged:
            print(merged, end="")
        return 124, merged


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_within_catalyst(path: Path) -> None:
    resolved = path.resolve()
    if not str(resolved).startswith(str(Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")))):
        raise RuntimeError(f"Refusing operation outside CatalystCivic: {resolved}")


def clean_runtime_dirs(include_reports: bool = False) -> None:
    log("\n--- Runtime Cleanup ---")
    targets = list(RUNTIME_CLEAN_TARGETS)
    if include_reports:
        targets.append(REPORTS_ROOT)
    for target in targets:
        if not target.exists():
            continue
        ensure_within_catalyst(target)
        for child in list(target.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except PermissionError:
                # Keep runtime loop alive even if a file handle is still open.
                log(f"WARNING: runtime cleanup skipped locked file: {child}")
    log("Runtime cleanup complete.")


def clear_children(path: Path) -> int:
    if not path.exists():
        return 0
    ensure_within_catalyst(path)
    removed = 0
    for child in list(path.iterdir()):
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
        removed += 1
    return removed


def remove_run_debug_folders() -> int:
    removed = 0
    for folder in OUTPUT_ROOT.iterdir():
        if folder.is_dir() and folder.name.startswith("RUN_"):
            ensure_within_catalyst(folder)
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def get_root_pdf_names() -> set[str]:
    return {p.name for p in AGENDAS_ROOT.glob("*.pdf")}


def get_output_pulse_dirs() -> set[str]:
    return {p.name for p in OUTPUT_ROOT.iterdir() if p.is_dir() and p.name.startswith("M1.AG.")}


def discover_total_urls(since_year: int) -> int:
    sys.path.insert(0, str(PULL_DIR))
    from parse_links import fetch_year_links  # pylint: disable=import-error

    current_year = datetime.now().year
    years = list(range(since_year, current_year + 1))
    urls: set[str] = set()
    for year in sorted(years, reverse=True):
        try:
            links = fetch_year_links(year)
            for link in links:
                url = str(link.get("url") or "").strip()
                if url:
                    urls.add(url)
        except Exception as exc:
            log(f"WARNING: discovery failed for {year}: {exc}")
    return len(urls)


def parse_newly_ingested_count(output_text: str) -> int | None:
    m = re.search(r"Newly ingested:\s+(\d+)", output_text)
    if not m:
        return None
    return int(m.group(1))


def reset_pulse_counter() -> None:
    payload = {"count": 0, "batch": []}
    try:
        PULSE_COUNTER_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log(f"WARNING: unable to reset pulse counter: {exc}")


def connect_db():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        database=os.getenv("PG_DB", "catalyst_civic"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", "postgres"),
    )


def wipe_pipeline_db() -> None:
    conn = connect_db()
    cur = conn.cursor()
    try:
        cur.execute("TRUNCATE TABLE m1_agenda.items RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE m1_agenda.meetings RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE m1_agenda.pipeline_ledger RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE cco.observations RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE cco.identities RESTART IDENTITY CASCADE;")
        cur.execute("TRUNCATE TABLE cco.registry RESTART IDENTITY CASCADE;")
        conn.commit()
    finally:
        cur.close()
        conn.close()


def full_reset_pipeline() -> None:
    log("\n=== FULL RESET: DB + PIPELINE STATE ===")
    wipe_pipeline_db()
    log("DB wipe complete.")

    removed_queue = 0
    for pdf in list(AGENDAS_ROOT.glob("*.pdf")):
        ensure_within_catalyst(pdf)
        pdf.unlink(missing_ok=True)
        removed_queue += 1
    log(f"Queue root cleared (*.pdf): {removed_queue}")

    removed_puller_files = 0
    for pull_file in (PULLER_STATE_FILE, PULLER_MANIFEST_FILE):
        if pull_file.exists():
            ensure_within_catalyst(pull_file)
            pull_file.unlink(missing_ok=True)
            removed_puller_files += 1
    log(f"Puller state/manifest files cleared: {removed_puller_files}")

    removed_output = clear_children(OUTPUT_ROOT)
    removed_vault = clear_children(VAULT_ROOT)
    removed_modes = clear_children(MODE_ROOT)
    removed_schema = clear_children(SCHEMA_ROOT)
    log(f"_output entries cleared: {removed_output}")
    log(f"_vaulted entries cleared: {removed_vault}")
    log(f"_Modes/M1/Agenda entries cleared: {removed_modes}")
    log(f"_Schema/M1/Agenda entries cleared: {removed_schema}")

    clean_runtime_dirs(include_reports=True)
    reset_pulse_counter()
    log("Pulse counter reset.")
    log("=== FULL RESET COMPLETE ===")


def load_pulse_factsheet(pulse_id: str) -> dict[str, Any]:
    pulse_dir = OUTPUT_ROOT / pulse_id
    if not pulse_dir.exists():
        return {}
    factsheets = sorted(pulse_dir.glob("*.factsheet.json"))
    if not factsheets:
        return {}
    try:
        return json.loads(factsheets[0].read_text(encoding="utf-8"))
    except Exception:
        return {}


def delete_pulse_rows(pulse_id: str) -> None:
    conn = connect_db()
    cur = conn.cursor()
    try:
        source_ids = [pulse_id, f"{pulse_id}.SCF1"]
        cur.execute("DELETE FROM m1_agenda.items WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        cur.execute("DELETE FROM m1_agenda.meetings WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        cur.execute("DELETE FROM m1_agenda.pipeline_ledger WHERE pulse_id = %s", (pulse_id,))
        cur.execute("DELETE FROM cco.observations WHERE source_id = ANY(%s)", (source_ids,))
        cur.execute("DELETE FROM cco.identities WHERE source_id = ANY(%s)", (source_ids,))
        cur.execute(
            """
            DELETE FROM cco.registry r
            WHERE NOT EXISTS (
                SELECT 1 FROM cco.observations o WHERE o.registry_id = r.registry_id
            )
            """
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def delete_pulse_artifacts(pulse_id: str) -> None:
    targets = [
        OUTPUT_ROOT / pulse_id,
        MODE_ROOT / pulse_id,
        SCHEMA_ROOT / pulse_id,
    ]
    for target in targets:
        if target.exists():
            ensure_within_catalyst(target)
            shutil.rmtree(target, ignore_errors=True)


def should_attempt_rescue(audit: dict[str, Any], rescue_all_unknowns: bool = True) -> bool:
    if rescue_all_unknowns:
        unknown_fields = list(audit.get("unknown_metadata_fields_all") or [])
        if unknown_fields:
            return True
    if audit.get("pass"):
        return False
    errors = set(audit.get("errors") or [])
    return bool(errors & RESCUE_ERROR_TRIGGERS)


def resolve_rescue_pulse_from_source(source_pdf_original_name: str, candidate_pulses: list[str]) -> str | None:
    wanted = (source_pdf_original_name or "").strip()
    if not wanted:
        return None
    for pulse_id in candidate_pulses:
        facts = load_pulse_factsheet(pulse_id)
        if str(facts.get("source_pdf_original_name") or "").strip() == wanted:
            return pulse_id
    return None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def is_missing_metadata(value: Any) -> bool:
    if value is None:
        return True
    text = normalize_text(str(value))
    return text in MISSING_META_VALUES


def source_has_location_signal(text: str) -> bool:
    lowered = normalize_text(text)
    if any(
        term in lowered
        for term in (
            "town hall",
            "council chamber",
            "municipal building",
            "town office",
            "police department",
            "museum",
        )
    ):
        return True
    return re.search(
        r"\b\d{1,5}\s+[A-Za-z0-9'\-]+(?:\s+[A-Za-z0-9'\-]+){0,6}\s+(?:street|st|road|rd|read|avenue|ave|drive|dr|lane|ln|court|ct|boulevard|blvd)\b",
        text or "",
        flags=re.IGNORECASE,
    ) is not None


def source_has_time_signal(text: str) -> bool:
    if re.search(r"\b\d{1,2}\s*[:\.]\s*\d{2}\s*[AP]\.?M\.?\b", text or "", flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d{1,2}\s*[AP]\.?M\.?\b", text or "", flags=re.IGNORECASE):
        return True
    return False


def infer_meeting_date_from_source_filename(filename: str) -> str | None:
    match = SOURCE_FILENAME_DATE_RE.match((filename or "").strip())
    if not match:
        return None
    raw = match.group(1)
    try:
        year = int(raw[0:4])
        month = int(raw[4:6])
        day = int(raw[6:8])
        return f"{year:04d}-{month:02d}-{day:02d}"
    except Exception:
        return None


def lexical_tokens(text: str) -> set[str]:
    tokens = {
        t.lower()
        for t in re.findall(r"[A-Za-z]{5,}", text or "")
        if t.lower() not in STOPWORDS
    }
    return tokens


def flatten_text_values(obj: Any) -> list[str]:
    vals: list[str] = []
    if obj is None:
        return vals
    if isinstance(obj, str):
        vals.append(obj)
        return vals
    if isinstance(obj, (int, float, bool)):
        vals.append(str(obj))
        return vals
    if isinstance(obj, dict):
        for v in obj.values():
            vals.extend(flatten_text_values(v))
        return vals
    if isinstance(obj, (list, tuple)):
        for v in obj:
            vals.extend(flatten_text_values(v))
        return vals
    vals.append(str(obj))
    return vals


def extract_ord_res_refs(text: str) -> list[str]:
    refs = []
    for m in re.finditer(
        r"\b(ordinance|resolution)\s*(?:no\.?|number|#)?\s*([A-Za-z0-9][A-Za-z0-9\-./]{1,40})",
        text or "",
        flags=re.IGNORECASE,
    ):
        kind = m.group(1).lower()
        code = re.sub(r"[^a-z0-9]", "", m.group(2).lower())
        if not code:
            continue
        # Avoid false captures like "ordinance ordinance"/"resolution to".
        # Real agenda refs are expected to carry at least one digit.
        if not any(ch.isdigit() for ch in code):
            continue
        refs.append(f"{kind} {code}")
    return sorted(set(refs))


def sync_meeting_source_metadata(
    cur,
    pulse_id: str,
    source_pdf_original_name: str,
    source_pdf_internal_name: str,
    source_pdf_hash: str,
    page_count: int | None,
    source_text: str,
) -> None:
    cur.execute(
        """
        SELECT meeting_id, meeting_date::text, COALESCE(metadata, '{}'::jsonb)
        FROM m1_agenda.meetings
        WHERE meeting_id LIKE %s
        LIMIT 1
        """,
        (f"{pulse_id}%",),
    )
    row = cur.fetchone()
    if not row:
        return

    meeting_id, meeting_date_text, metadata = row
    meta = dict(metadata or {})
    changed = False

    updates: dict[str, Any] = {
        "source_pulse_id": pulse_id,
        "source_pdf_original_name": source_pdf_original_name,
        "source_pdf_internal_name": source_pdf_internal_name,
        "source_pdf_hash": source_pdf_hash.lower().strip() if source_pdf_hash else "",
        "page_count": page_count if isinstance(page_count, int) else None,
    }

    for key, value in updates.items():
        if value in (None, ""):
            continue
        if meta.get(key) != value:
            meta[key] = value
            changed = True

    if source_text:
        chars = len(source_text)
        if int(meta.get("source_text_chars") or 0) != chars:
            meta["source_text_chars"] = chars
            changed = True
        if not str(meta.get("source_text_full") or "").strip():
            meta["source_text_full"] = source_text
            changed = True

    inferred_date = infer_meeting_date_from_source_filename(source_pdf_original_name)
    if inferred_date and (not str(meeting_date_text or "").strip()):
        cur.execute(
            "UPDATE m1_agenda.meetings SET meeting_date = %s::date, updated_at = CURRENT_TIMESTAMP WHERE meeting_id = %s",
            (inferred_date, meeting_id),
        )
        changed = True

    if changed:
        cur.execute(
            "UPDATE m1_agenda.meetings SET metadata = %s::jsonb, updated_at = CURRENT_TIMESTAMP WHERE meeting_id = %s",
            (json.dumps(meta), meeting_id),
        )


def audit_pulse(
    pulse_id: str,
    quality_target: float = DEFAULT_QUALITY_TARGET,
    metadata_target: float = DEFAULT_METADATA_TARGET,
) -> dict[str, Any]:
    pulse_dir = OUTPUT_ROOT / pulse_id
    factsheets = list(pulse_dir.glob("*.factsheet.json"))
    texts = list(pulse_dir.glob("*.txt"))

    result: dict[str, Any] = {
        "pulse_id": pulse_id,
        "factsheet_count": len(factsheets),
        "text_count": len(texts),
        "source_pdf_original_name": "",
        "source_pdf_internal_name": "",
        "source_pdf_hash": "",
        "hash_ok": False,
        "source_exists": False,
        "scaffold_exists": False,
        "pages_match": False,
        "db_meeting_count": None,
        "db_items_count": None,
        "expected_units": None,
        "items_match": False,
        "ledger_state": None,
        "semantic_chars": 0,
        "lexical_coverage": None,
        "source_signal_terms": [],
        "missing_signal_terms": [],
        "source_ord_res_refs": [],
        "missing_ord_res_refs": [],
        "quality_target": quality_target,
        "metadata_target": metadata_target,
        "metadata_completeness": None,
        "required_metadata_fields": [],
        "missing_metadata_fields": [],
        "unknown_metadata_fields_all": [],
        "rescue_attempted": False,
        "rescue_attempts_used": 0,
        "rescue_source_pulse_id": None,
        "rescue_notes": [],
        "pass": False,
        "errors": [],
    }

    if len(factsheets) != 1:
        result["errors"].append("factsheet_missing_or_multiple")
        return result
    if len(texts) != 1:
        result["errors"].append("text_missing_or_multiple")
        return result

    facts = json.loads(factsheets[0].read_text(encoding="utf-8"))
    src_name = str(facts.get("source_pdf_original_name") or "").strip()
    src_internal = str(facts.get("source_pdf_internal_name") or "").strip()
    src_hash = str(facts.get("source_pdf_hash") or "").lower().strip()
    page_count = facts.get("page_count")
    result["source_pdf_original_name"] = src_name
    result["source_pdf_internal_name"] = src_internal
    result["source_pdf_hash"] = src_hash

    if not src_name:
        result["errors"].append("factsheet_source_name_missing")
    else:
        src_path = VAULT_ROOT / src_name
        if src_path.exists() and src_path.is_file():
            result["source_exists"] = True
            if src_hash:
                result["hash_ok"] = sha256_file(src_path).lower() == src_hash
                if not result["hash_ok"]:
                    result["errors"].append("source_hash_mismatch")
        else:
            result["errors"].append("source_missing_in_vault")

    mode_dir = MODE_ROOT / pulse_id
    scf1 = mode_dir / f"{pulse_id}.SCF1.md"
    if scf1.exists():
        result["scaffold_exists"] = True
        md = scf1.read_text(encoding="utf-8", errors="ignore")
        sections = md.count("###")
        items = len(re.findall(r"^- \d+\.", md, re.MULTILINE))
        expected_units = sections + items
        page_json_count = len(list(mode_dir.glob("page_*.json")))
        result["expected_units"] = expected_units
        result["pages_match"] = isinstance(page_count, int) and page_json_count == page_count
        if not result["pages_match"]:
            result["errors"].append("page_json_count_mismatch")
    else:
        result["errors"].append("missing_scf1")

    source_text = texts[0].read_text(encoding="utf-8", errors="ignore")
    source_text = re.sub(r"--- PAGE .*? ---", " ", source_text)
    source_norm = normalize_text(source_text)

    conn = connect_db()
    cur = conn.cursor()
    try:
        sync_meeting_source_metadata(
            cur=cur,
            pulse_id=pulse_id,
            source_pdf_original_name=src_name,
            source_pdf_internal_name=src_internal,
            source_pdf_hash=src_hash,
            page_count=page_count if isinstance(page_count, int) else None,
            source_text=source_text,
        )
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM m1_agenda.meetings WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        result["db_meeting_count"] = int(cur.fetchone()[0])
        if result["db_meeting_count"] <= 0:
            result["errors"].append("db_meeting_missing")

        cur.execute("SELECT COUNT(*) FROM m1_agenda.items WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        result["db_items_count"] = int(cur.fetchone()[0])
        if (result["db_items_count"] or 0) == 0:
            result["errors"].append("zero_items")

        if result["expected_units"] is not None:
            result["items_match"] = result["db_items_count"] == result["expected_units"]
            if not result["items_match"]:
                result["errors"].append("db_items_mismatch")

        cur.execute(
            """
            SELECT
                COALESCE(label, ''),
                COALESCE(title, ''),
                COALESCE(section_title, ''),
                COALESCE(content, ''),
                COALESCE(item_text, '')
            FROM m1_agenda.items
            WHERE meeting_id LIKE %s
            ORDER BY ordinal
            """,
            (f"{pulse_id}%",),
        )
        db_text_parts: list[str] = []
        semantic_chars = 0
        for label, title, section_title, content, item_text in cur.fetchall():
            db_text_parts.extend([label, title, section_title, content, item_text])
            semantic_chars += len(content or "") + len(item_text or "")
        result["semantic_chars"] = semantic_chars

        cur.execute(
            """
            SELECT
                COALESCE(meeting_type, ''),
                COALESCE(meeting_date::text, ''),
                COALESCE(meeting_time, ''),
                COALESCE(location, ''),
                COALESCE(metadata, '{}'::jsonb)
            FROM m1_agenda.meetings
            WHERE meeting_id LIKE %s
            LIMIT 1
            """,
            (f"{pulse_id}%",),
        )
        meeting_row = cur.fetchone()
        if meeting_row:
            metadata_fields = {
                "meeting_type": meeting_row[0],
                "meeting_date": meeting_row[1],
                "meeting_time": meeting_row[2],
                "location": meeting_row[3],
            }
            unknown_all = [
                field_name
                for field_name in ("meeting_type", "meeting_date", "meeting_time", "location")
                if is_missing_metadata(metadata_fields.get(field_name, ""))
            ]
            result["unknown_metadata_fields_all"] = unknown_all
            required_fields = ["meeting_type", "meeting_date"]
            if source_has_time_signal(source_text):
                required_fields.append("meeting_time")
            if source_has_location_signal(source_text):
                required_fields.append("location")
            result["required_metadata_fields"] = required_fields
            missing_metadata = [
                field_name
                for field_name in required_fields
                if is_missing_metadata(metadata_fields.get(field_name, ""))
            ]
            present_count = len(required_fields) - len(missing_metadata)
            completeness = 1.0 if not required_fields else (present_count / len(required_fields))
            result["metadata_completeness"] = round(completeness, 4)
            result["missing_metadata_fields"] = missing_metadata
            for field_name in missing_metadata:
                result["errors"].append(f"missing_{field_name}")
            if completeness < metadata_target:
                result["errors"].append("metadata_completeness_low")

            db_text_parts.extend(
                [
                    metadata_fields["meeting_type"],
                    metadata_fields["meeting_date"],
                    metadata_fields["meeting_time"],
                    metadata_fields["location"],
                ]
            )
            db_text_parts.extend(flatten_text_values(meeting_row[4]))
        else:
            result["metadata_completeness"] = 0.0
            result["required_metadata_fields"] = ["meeting_type", "meeting_date", "meeting_time", "location"]
            result["missing_metadata_fields"] = ["meeting_type", "meeting_date", "meeting_time", "location"]
            result["unknown_metadata_fields_all"] = ["meeting_type", "meeting_date", "meeting_time", "location"]
            result["errors"].append("metadata_completeness_low")

        db_text = " ".join(db_text_parts)
        db_norm = normalize_text(db_text)

        if (result["db_items_count"] or 0) > 0 and semantic_chars == 0:
            result["errors"].append("semantic_fields_empty")

        source_terms = [t for t in SIGNAL_TERMS if re.search(rf"\b{re.escape(t)}\b", source_norm)]
        missing_terms = [t for t in source_terms if not re.search(rf"\b{re.escape(t)}\b", db_norm)]
        result["source_signal_terms"] = source_terms
        result["missing_signal_terms"] = missing_terms
        if missing_terms:
            result["errors"].append("missing_signal_terms")

        source_refs = extract_ord_res_refs(source_text)
        db_refs = set(extract_ord_res_refs(db_text))
        missing_refs = sorted(set(source_refs) - db_refs)
        result["source_ord_res_refs"] = source_refs
        result["missing_ord_res_refs"] = missing_refs[:10]
        if missing_refs:
            result["errors"].append("missing_ord_res_refs")

        source_lex = lexical_tokens(source_text)
        db_lex = lexical_tokens(db_text)
        lex_cov = 1.0
        if source_lex:
            lex_cov = len(source_lex & db_lex) / len(source_lex)
        result["lexical_coverage"] = round(lex_cov, 4)

        if (result["db_items_count"] or 0) > 0 and len(source_lex) >= 20 and lex_cov < quality_target:
            result["errors"].append("lexical_coverage_low")

        cur.execute("SELECT current_state FROM m1_agenda.pipeline_ledger WHERE pulse_id = %s", (pulse_id,))
        row = cur.fetchone()
        result["ledger_state"] = row[0] if row else None
        if result["ledger_state"] != "DONE":
            result["errors"].append("ledger_not_done")
    finally:
        cur.close()
        conn.close()

    result["pass"] = len(result["errors"]) == 0
    return result


def attempt_rescue_for_audit(
    audit: dict[str, Any],
    quality_target: float,
    metadata_target: float,
    rescue_conductor_timeout_seconds: int,
) -> dict[str, Any]:
    updated = dict(audit)
    updated["rescue_attempted"] = True
    updated["rescue_attempts_used"] = int(updated.get("rescue_attempts_used") or 0) + 1
    notes = list(updated.get("rescue_notes") or [])

    source_pdf_original_name = str(updated.get("source_pdf_original_name") or "").strip()
    if not source_pdf_original_name:
        notes.append("rescue_skipped:missing_source_pdf_original_name")
        updated["rescue_notes"] = notes
        return updated

    source_vault_path = VAULT_ROOT / source_pdf_original_name
    if not source_vault_path.exists():
        notes.append(f"rescue_skipped:missing_vault_source:{source_pdf_original_name}")
        updated["rescue_notes"] = notes
        return updated

    queue_now = get_root_pdf_names()
    unexpected_queue = sorted(queue_now - {source_pdf_original_name})
    if unexpected_queue:
        notes.append(f"rescue_skipped:queue_not_clean:{len(unexpected_queue)}")
        updated["rescue_notes"] = notes
        return updated

    source_pulse_id = str(updated.get("pulse_id") or "").strip()
    try:
        if source_pulse_id:
            delete_pulse_rows(source_pulse_id)
            delete_pulse_artifacts(source_pulse_id)
    except Exception as exc:
        notes.append(f"rescue_warning:cleanup_failed:{exc}")

    queue_target = AGENDAS_ROOT / source_pdf_original_name
    if not queue_target.exists():
        shutil.copy2(source_vault_path, queue_target)
    before_pulses = get_output_pulse_dirs()

    reset_pulse_counter()
    max_conductor_runs = 3
    runs = 0
    while source_pdf_original_name in get_root_pdf_names() and runs < max_conductor_runs:
        runs += 1
        rc_run, _ = run_cmd(
            [str(PYTHON_EXE), str(CONDUCTOR)],
            cwd=BASE_DIR,
            timeout_seconds=rescue_conductor_timeout_seconds,
        )
        if rc_run != 0:
            notes.append(f"rescue_failed:conductor_rc_{rc_run}")
            updated["rescue_notes"] = notes
            return updated

    if source_pdf_original_name in get_root_pdf_names():
        notes.append("rescue_failed:source_still_in_queue")
        updated["rescue_notes"] = notes
        return updated

    after_pulses = get_output_pulse_dirs()
    new_pulses = sorted(after_pulses - before_pulses)
    rescue_pulse_id = resolve_rescue_pulse_from_source(source_pdf_original_name, new_pulses)
    if rescue_pulse_id is None and source_pulse_id:
        rescue_pulse_id = resolve_rescue_pulse_from_source(source_pdf_original_name, [source_pulse_id])
    if rescue_pulse_id is None:
        notes.append("rescue_failed:unable_to_resolve_rescue_pulse")
        updated["rescue_notes"] = notes
        return updated

    rescued = audit_pulse(
        rescue_pulse_id,
        quality_target=quality_target,
        metadata_target=metadata_target,
    )
    rescued["rescue_attempted"] = True
    rescued["rescue_attempts_used"] = int(updated.get("rescue_attempts_used") or 1)
    rescued["rescue_source_pulse_id"] = source_pulse_id or None
    notes.append(f"rescue_reprocessed_as:{rescue_pulse_id}")
    rescued["rescue_notes"] = notes
    return rescued


def apply_rescue_pass(
    audits: list[dict[str, Any]],
    quality_target: float,
    metadata_target: float,
    rescue_attempts: int,
    rescue_all_unknowns: bool,
    rescue_conductor_timeout_seconds: int,
) -> list[dict[str, Any]]:
    if rescue_attempts <= 0:
        return audits

    finalized: list[dict[str, Any]] = []
    for audit in audits:
        current = audit
        attempts_left = rescue_attempts
        while attempts_left > 0 and should_attempt_rescue(current, rescue_all_unknowns=rescue_all_unknowns):
            attempts_left -= 1
            log(f"Rescue attempt for pulse {current.get('pulse_id')} ({rescue_attempts - attempts_left}/{rescue_attempts})")
            current = attempt_rescue_for_audit(
                current,
                quality_target=quality_target,
                metadata_target=metadata_target,
                rescue_conductor_timeout_seconds=rescue_conductor_timeout_seconds,
            )
            if current.get("pass"):
                break
            if any(str(note).startswith("rescue_skipped:") for note in current.get("rescue_notes") or []):
                break
            if any(str(note).startswith("rescue_failed:") for note in current.get("rescue_notes") or []):
                break
        finalized.append(current)
    return finalized


def run_batch(
    batch_size: int,
    since_year: int,
    quality_target: float,
    metadata_target: float,
    rescue_attempts: int,
    rescue_all_unknowns: bool,
    pull_timeout_seconds: int,
    conductor_timeout_seconds: int,
    rescue_conductor_timeout_seconds: int,
    include_existing_queue: bool = False,
) -> dict[str, Any]:
    before_root = get_root_pdf_names()
    before_pulses = get_output_pulse_dirs()

    rc_pull, out_pull = run_cmd(
        [str(PYTHON_EXE), str(PULLER), "--limit", str(batch_size), "--since", str(since_year)],
        cwd=PULL_DIR,
        timeout_seconds=pull_timeout_seconds,
    )
    if rc_pull != 0:
        raise RuntimeError("Puller failed.")

    puller_new = parse_newly_ingested_count(out_pull)
    after_pull_root = get_root_pdf_names()
    pulled_files = sorted(after_pull_root - before_root)

    if not pulled_files and not (include_existing_queue and before_root):
        return {
            "pulled_count": 0,
            "pulled_files": [],
            "new_pulses": [],
            "audits": [],
            "unprocessed_pulled": [],
        }

    if puller_new is not None and puller_new != len(pulled_files):
        log(
            f"WARNING: puller summary said {puller_new} new, "
            f"queue diff shows {len(pulled_files)}."
        )

    # Conductor runs one source at a time; for batch pulls, drain the batch queue.
    target_queue = set(pulled_files)
    if include_existing_queue:
        target_queue |= set(before_root)

    # Keep automated batch runs from triggering manual deep-inspection mode.
    reset_pulse_counter()
    max_runs = max(1, len(target_queue) + 2)
    runs_executed = 0
    while runs_executed < max_runs:
        queue_now = get_root_pdf_names()
        pending = sorted(target_queue & queue_now)
        if not pending:
            break
        runs_executed += 1
        log(f"Draining pulled queue ({len(pending)} pending) - conductor run {runs_executed}/{max_runs}")
        rc_run, _ = run_cmd(
            [str(PYTHON_EXE), str(CONDUCTOR)],
            cwd=BASE_DIR,
            timeout_seconds=conductor_timeout_seconds,
        )
        if rc_run != 0:
            raise RuntimeError("Conductor failed.")

    queue_after = get_root_pdf_names()
    unprocessed_pulled = sorted(target_queue & queue_after)
    after_pulses = get_output_pulse_dirs()
    new_pulses = sorted(after_pulses - before_pulses)

    audits = [
        audit_pulse(
            pulse,
            quality_target=quality_target,
            metadata_target=metadata_target,
        )
        for pulse in new_pulses
    ]
    audits = apply_rescue_pass(
        audits=audits,
        quality_target=quality_target,
        metadata_target=metadata_target,
        rescue_attempts=rescue_attempts,
        rescue_all_unknowns=rescue_all_unknowns,
        rescue_conductor_timeout_seconds=rescue_conductor_timeout_seconds,
    )
    return {
        "pulled_count": len(pulled_files),
        "pulled_files": pulled_files,
        "new_pulses": new_pulses,
        "audits": audits,
        "unprocessed_pulled": unprocessed_pulled,
    }


def summarize_batch(result: dict[str, Any]) -> tuple[int, int]:
    audits = result["audits"]
    passed = sum(1 for a in audits if a.get("pass"))
    total = len(audits)
    log("\n--- Batch Audit ---")
    log(f"Pulled files: {result['pulled_count']}")
    log(f"New pulses: {len(result['new_pulses'])}")
    for row in audits:
        status = "PASS" if row["pass"] else "FAIL"
        log(
            f"{status}  {row['pulse_id']}  "
            f"items={row['db_items_count']} expected={row['expected_units']} "
            f"semantic_chars={row['semantic_chars']} lex_cov={row['lexical_coverage']} "
            f"target={row['quality_target']} meta_cov={row.get('metadata_completeness')} "
            f"meta_target={row.get('metadata_target')} "
            f"ledger={row['ledger_state']} "
            f"rescue={row.get('rescue_attempts_used', 0)} "
            f"unknown_all={len(row.get('unknown_metadata_fields_all') or [])} "
            f"errors={','.join(row['errors']) or 'none'}"
        )
    if result.get("unprocessed_pulled"):
        log("UNPROCESSED PULLED FILES:")
        for name in result["unprocessed_pulled"]:
            log(f"  {name}")
    return passed, total


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch ingest + integrity gate for Agenda pipeline.")
    ap.add_argument("--batch-size", type=int, default=1, help="How many PDFs to pull each batch.")
    ap.add_argument(
        "--quality-target",
        type=float,
        default=DEFAULT_QUALITY_TARGET,
        help="Required lexical coverage target (0.0-1.0). Default is 0.95.",
    )
    ap.add_argument(
        "--metadata-target",
        type=float,
        default=DEFAULT_METADATA_TARGET,
        help="Required meeting metadata completeness target (0.0-1.0). Default is 0.95.",
    )
    ap.add_argument(
        "--rescue-attempts",
        type=int,
        default=1,
        help="How many targeted rescue attempts to run for a failed pulse (default 1). Set 0 to disable.",
    )
    ap.add_argument(
        "--rescue-all-unknowns",
        dest="rescue_all_unknowns",
        action="store_true",
        help="Trigger rescue when any metadata field remains UNKNOWN, even if other gates pass.",
    )
    ap.add_argument(
        "--no-rescue-all-unknowns",
        dest="rescue_all_unknowns",
        action="store_false",
        help="Only rescue hard failures matched by rescue error triggers.",
    )
    ap.set_defaults(rescue_all_unknowns=True)
    ap.add_argument("--since", type=int, default=2013, help="Earliest year to scan.")
    ap.add_argument(
        "--until-percent",
        type=float,
        default=None,
        help="Optional processed coverage target; run multiple batches until reached.",
    )
    ap.add_argument("--max-batches", type=int, default=1, help="Maximum batches to run this invocation.")
    ap.add_argument(
        "--allow-existing-queue",
        action="store_true",
        help="Allow existing root PDFs in Agendas before batch starts.",
    )
    ap.add_argument(
        "--keep-run-debug",
        action="store_true",
        help="Keep RUN_* debug folders in _output (default removes them).",
    )
    ap.add_argument(
        "--skip-runtime-clean",
        action="store_true",
        help="Skip cleaning parser runtime folders after each batch.",
    )
    ap.add_argument(
        "--strict-integrity-fail",
        action="store_true",
        help="Stop immediately and return non-zero when any pulse fails integrity checks.",
    )
    ap.add_argument(
        "--pull-timeout-seconds",
        type=int,
        default=DEFAULT_PULL_TIMEOUT_SECONDS,
        help=f"Timeout for puller command (default {DEFAULT_PULL_TIMEOUT_SECONDS}s).",
    )
    ap.add_argument(
        "--conductor-timeout-seconds",
        type=int,
        default=DEFAULT_CONDUCTOR_TIMEOUT_SECONDS,
        help=f"Timeout for conductor command (default {DEFAULT_CONDUCTOR_TIMEOUT_SECONDS}s).",
    )
    ap.add_argument(
        "--rescue-conductor-timeout-seconds",
        type=int,
        default=DEFAULT_RESCUE_CONDUCTOR_TIMEOUT_SECONDS,
        help=f"Timeout for rescue conductor command (default {DEFAULT_RESCUE_CONDUCTOR_TIMEOUT_SECONDS}s).",
    )
    ap.add_argument(
        "--full-reset",
        action="store_true",
        help="Wipe DB + queue + vault/output/mode/schema/runtime state before running batches.",
    )
    ap.add_argument(
        "--full-reset-only",
        action="store_true",
        help="Run full reset and exit without running any batch.",
    )
    args = ap.parse_args()
    if args.quality_target < 0.0 or args.quality_target > 1.0:
        raise RuntimeError("--quality-target must be between 0.0 and 1.0")
    if args.metadata_target < 0.0 or args.metadata_target > 1.0:
        raise RuntimeError("--metadata-target must be between 0.0 and 1.0")
    if args.rescue_attempts < 0:
        raise RuntimeError("--rescue-attempts must be >= 0")
    if args.pull_timeout_seconds <= 0:
        raise RuntimeError("--pull-timeout-seconds must be > 0")
    if args.conductor_timeout_seconds <= 0:
        raise RuntimeError("--conductor-timeout-seconds must be > 0")
    if args.rescue_conductor_timeout_seconds <= 0:
        raise RuntimeError("--rescue-conductor-timeout-seconds must be > 0")

    if args.full_reset or args.full_reset_only:
        full_reset_pipeline()
        if args.full_reset_only:
            return 0

    if not PULLER.exists() or not CONDUCTOR.exists():
        raise RuntimeError("Missing required scripts (PULL/orchestrator.py or conductor.py).")
    if not PYTHON_EXE.exists():
        raise RuntimeError(f"Python executable not found: {PYTHON_EXE}")

    queue_now = sorted(get_root_pdf_names())
    if queue_now and not args.allow_existing_queue:
        log("ERROR: queue is not empty. Use --allow-existing-queue or clear root PDFs first.")
        for name in queue_now[:20]:
            log(f"  {name}")
        return 2

    total_urls = discover_total_urls(args.since)
    processed_now = len(list(VAULT_ROOT.glob("*.pdf")))
    processed_start = processed_now
    target_processed = None
    if args.until_percent is not None:
        target_processed = math.ceil(total_urls * (args.until_percent / 100.0))

    log("\n=== Coverage Baseline ===")
    log(f"Discovered URLs: {total_urls}")
    log(f"Processed PDFs (_vaulted): {processed_now}")
    if target_processed is not None:
        log(f"Target processed for {args.until_percent:.2f}%: {target_processed}")

    all_results: list[dict[str, Any]] = []
    batches_run = 0
    while batches_run < args.max_batches:
        if target_processed is not None:
            processed_now = len(list(VAULT_ROOT.glob("*.pdf")))
            if processed_now >= target_processed:
                log("\nTarget coverage reached; stopping.")
                break

        batches_run += 1
        log(f"\n=== Running Batch {batches_run} ===")
        try:
            result = run_batch(
                batch_size=args.batch_size,
                since_year=args.since,
                quality_target=args.quality_target,
                metadata_target=args.metadata_target,
                rescue_attempts=args.rescue_attempts,
                rescue_all_unknowns=args.rescue_all_unknowns,
                pull_timeout_seconds=args.pull_timeout_seconds,
                conductor_timeout_seconds=args.conductor_timeout_seconds,
                rescue_conductor_timeout_seconds=args.rescue_conductor_timeout_seconds,
                include_existing_queue=args.allow_existing_queue,
            )
        except Exception as exc:
            log(f"CRITICAL: batch failed: {exc}")
            return 1

        if result["pulled_count"] == 0 and len(result["new_pulses"]) == 0:
            log("No new files pulled; stopping.")
            break

        if result.get("unprocessed_pulled"):
            log("One or more pulled files were not processed; stopping.")
            all_results.append(result)
            break

        passed, total = summarize_batch(result)
        all_results.append(result)

        removed_runs = 0
        if not args.keep_run_debug:
            removed_runs = remove_run_debug_folders()
            log(f"Removed RUN_* debug folders: {removed_runs}")

        if not args.skip_runtime_clean:
            clean_runtime_dirs()

        if passed != total:
            log("One or more pulses failed integrity checks; logging and continuing.")
            if args.strict_integrity_fail:
                log("Strict integrity fail enabled; stopping.")
                break

    processed_end = len(list(VAULT_ROOT.glob("*.pdf")))
    coverage = (processed_end / total_urls * 100.0) if total_urls else 0.0
    summary = {
        "timestamp": now_stamp(),
        "discovered_urls": total_urls,
        "processed_start": processed_start,
        "processed_end": processed_end,
        "coverage_percent": round(coverage, 2),
        "batches_run": batches_run,
        "batch_size": args.batch_size,
        "quality_target": args.quality_target,
        "metadata_target": args.metadata_target,
        "rescue_attempts": args.rescue_attempts,
        "rescue_all_unknowns": args.rescue_all_unknowns,
        "results": all_results,
    }

    reports_dir = BASE_DIR / "tools" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"batch_tdd_report_{now_stamp()}.json"
    report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    log("\n=== Final Summary ===")
    log(f"Processed now: {processed_end} / {total_urls} ({coverage:.2f}%)")
    log(f"Report: {report_path}")

    any_fail = any(
        not pulse.get("pass", False)
        for batch in all_results
        for pulse in batch.get("audits", [])
    )
    any_unprocessed = any(batch.get("unprocessed_pulled") for batch in all_results)
    if any_fail and not args.strict_integrity_fail:
        fail_count = sum(
            1
            for batch in all_results
            for pulse in batch.get("audits", [])
            if not pulse.get("pass", False)
        )
        log(f"Integrity failures logged/skipped: {fail_count}")

    return 1 if ((any_unprocessed) or (args.strict_integrity_fail and any_fail)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
