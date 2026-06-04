import os
import sys
import json
import shutil
import hashlib
import psycopg2
import subprocess
import time
import re
from pathlib import Path
from datetime import datetime

# --- CONFIGURATION ---
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "catalyst_civic")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "postgres")

# Paths (Relative to the script location)
BASE_DIR = Path(__file__).resolve().parent
PULL_DIR = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas"
VAULT_ROOT = PULL_DIR / "_output"
MODES_DIR = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Modes" / "M1" / "Agenda"
PYTHON_EXE = sys.executable
PULSE_COUNTER_FILE = BASE_DIR / "pulse_counter.json"

# Sub-scripts (State Machine Workers)
PULLER = BASE_DIR / "PULL" / "orchestrator.py"
SCAFFOLDER = BASE_DIR / "Schema_Scaffolder" / "run_schema_scaffold.py"
SCULPTOR = BASE_DIR / "schema sculptor" / "schema_sculptor.py"
MIGRATOR = BASE_DIR / "Migrator" / "migrate_agenda_scaffold.py"
REGISTRY = BASE_DIR / "Registry_Loader" / "registry_loader.py"
ENGINE_ORCHESTRATOR = BASE_DIR / "PDF_Parser_Engine" / "scripts" / "pulse_orchestrator.py"

def get_pulse_batch():
    if PULSE_COUNTER_FILE.exists():
        try:
            data = json.loads(PULSE_COUNTER_FILE.read_text())
            return data.get("batch", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []

def add_to_batch(pulse_id):
    batch = get_pulse_batch()
    batch.append(pulse_id)
    PULSE_COUNTER_FILE.write_text(json.dumps({"count": len(batch), "batch": batch}))
    return len(batch), batch

def trigger_deep_inspection_batch(batch_list):
    """Physically displays source vs. DB for all 10 pulses in the batch."""
    print("\n" + "!" * 80)
    print("!!! BATCH DEEP INSPECTION PROTOCOL TRIGGERED (The Rule of 10) !!!")
    print("!" * 80)
    
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()

        for idx, pulse_id in enumerate(batch_list, 1):
            print(f"\n[{idx}/10] AUDITING PULSE: {pulse_id}")
            print("-" * 40)
            
            # 1. Fetch DB Samples
            print(">>> DATABASE SAMPLES:")
            cur.execute(
                "SELECT item_label, title FROM m1_agenda.items WHERE meeting_id LIKE %s ORDER BY section_ordinal, item_ordinal LIMIT 3",
                (f"{pulse_id}%",)
            )
            rows = cur.fetchall()
            if rows:
                for r in rows: print(f"  [DB] {r[0] or ''} {r[1] or ''}")
            else:
                print("  [DB] No items found.")

            # 2. Fetch OCR Samples
            # We look in the _output directory for the corresponding pulse artifacts
            pulse_output_dirs = list(VAULT_ROOT.glob(f"*{pulse_id}*"))
            if pulse_output_dirs:
                pulse_dir = pulse_output_dirs[0]
                text_file = pulse_dir / f"{pulse_id}.txt"
                if text_file.exists():
                    print("\n>>> SOURCE TEXT SAMPLES:")
                    lines = [l for l in text_file.read_text(encoding="utf-8").splitlines() if l.strip() and "--- PAGE" not in l][:5]
                    for line in lines: print(f"  [SOURCE] {line.strip()}")
                else:
                    print(f"  [SOURCE] Factsheet/Text file not found in {pulse_dir}")
            else:
                print(f"  [SOURCE] Output folder not found in {VAULT_ROOT}")
            
            print("-" * 40)

        cur.close()
        conn.close()
    except Exception as e:
        print(f"  ! Batch Audit failed: {e}")
    
    print("\n" + "=" * 80)
    print("BATCH INSPECTION COMPLETE: Do all 10 DB entries match Source semantics?")
    print("If YES: Run with --clear-batch to start the next set.")
    print("If NO: Fix the engine before processing Source #11.")
    print("=" * 80)
    sys.exit(0)

def generate_machine_code(pdf_path):
    """Generates a unique machine code based on date and file hash."""
    file_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16].upper()
    date_str = datetime.now().strftime("%Y%m%d")
    return f"M1.AG.{date_str}.{file_hash}"

def update_ledger(cur, pulse_id, state, manifest):
    """Updates the pipeline ledger with the current state."""
    cur.execute("""
        INSERT INTO m1_agenda.pipeline_ledger (pulse_id, current_state, file_manifest)
        VALUES (%s, %s, %s)
        ON CONFLICT (pulse_id) DO UPDATE SET
            current_state = EXCLUDED.current_state,
            file_manifest = m1_agenda.pipeline_ledger.file_manifest || EXCLUDED.file_manifest,
            updated_at = CURRENT_TIMESTAMP;
    """, (pulse_id, state, json.dumps(manifest)))

def run_step(cmd, description, cwd=None):
    """Runs a subprocess and logs the result with local PYTHONPATH, streaming output in real-time."""
    print(f"--- STEP: {description} ---")

    env = os.environ.copy()
    ppath = [str(BASE_DIR), str(BASE_DIR / "PDF_Parser_Engine")]
    if env.get("PYTHONPATH"):
        ppath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(ppath)

    process = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT, 
        text=True, 
        env=env, 
        cwd=cwd,
        bufsize=1,
        universal_newlines=True
    )

    full_output = []
    for line in process.stdout:
        print(line, end='', flush=True)
        full_output.append(line)

    process.wait()

    if process.returncode != 0:
        print(f"FAILED: {description}")
        return False, "".join(full_output)

    print(f"SUCCESS: {description}")
    return True, "".join(full_output)

def verify_accuracy(pulse_id):
    """Checks if the DB reflects exactly what the parser produced."""
    print(f"\n>>> VERIFYING DB ACCURACY FOR {pulse_id}...")
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM m1_agenda.items WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        db_count = cur.fetchone()[0]
        
        pulse_mode_dir = MODES_DIR / pulse_id
        md_files = list(pulse_mode_dir.glob("*.SCF1.md"))
        if not md_files:
            print(f"  ! ERROR: No Scaffold MD found in {pulse_mode_dir}")
            return False
        
        md_text = md_files[0].read_text(encoding="utf-8")
        sections_count = md_text.count("###")
        items_count = len(re.findall(r"^- \d+\.", md_text, re.MULTILINE))
        parser_count = sections_count + items_count
        
        print(f"    Parser Units (Sections: {sections_count}, Items: {items_count}): {parser_count}")
        print(f"    Database Items (items table):  {db_count}")
        
        cur.close()
        conn.close()
        return db_count == parser_count
    except Exception as e:
        print(f"  ! Verification Error: {e}")
        return False

def rollback_pulse(pulse_id):
    """Wipes the database and modes folder for a failed verification."""
    print(f"\n>>> ROLLBACK: CLEARING PULSE {pulse_id}...")
    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()
        cur.execute("DELETE FROM m1_agenda.items WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        cur.execute("DELETE FROM m1_agenda.meetings WHERE meeting_id LIKE %s", (f"{pulse_id}%",))
        cur.execute("DELETE FROM m1_agenda.pipeline_ledger WHERE pulse_id = %s", (pulse_id,))
        conn.commit()
        cur.close()
        conn.close()
        print("  DB entry cleared.")
    except Exception as e:
        print(f"  ! DB rollback failed: {e}")
    
    pulse_mode_dir = MODES_DIR / pulse_id
    if pulse_mode_dir.exists():
        shutil.rmtree(pulse_mode_dir)
        print(f"  Modes folder cleared: {pulse_id}")

def orchestrate(skip_janitor=False, pull_next=False, clear_batch=False):
    if clear_batch:
        print("\n>>> CLEARING BATCH HISTORY...")
        PULSE_COUNTER_FILE.write_text(json.dumps({"count": 0, "batch": []}))
        print("Done. Counter reset to 0.")
        return

    # 0. PULL NEW SOURCE (OPTIONAL)
    if pull_next:
        print("\n>>> STAGE 0: PULLING NEW SOURCE FROM WEB...")
        pull_cmd = [PYTHON_EXE, str(PULLER), "--limit", "1", "--since", "2013"]
        success, out = run_step(pull_cmd, "Richlands Agenda Puller", cwd=str(PULLER.parent))
        if not success or "Newly ingested: 1" not in out:
            print("No new PDFs available or pull failed.")
            return

    # 1. SCOUT
    new_pdfs = [p for p in PULL_DIR.glob("*.pdf") if "_vaulted" not in str(p)]
    if not new_pdfs:
        print("No new PDFs in PULL. Standing by.")
        return

    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, database=PG_DB, user=PG_USER, password=PG_PASS)
        cur = conn.cursor()

        for pdf in sorted(new_pdfs):
            pulse_id = generate_machine_code(pdf)
            print(f"\n>>> PROCESSING PULSE: {pulse_id}")
            
            update_ledger(cur, pulse_id, "INTAKE", {"source_pdf": str(pdf)})
            conn.commit()

            # --- STATE: OPTICAL ---
            ENGINE_INBOX = BASE_DIR / "PDF_Parser_Engine" / "inbox"
            ENGINE_INBOX.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf, ENGINE_INBOX / pdf.name)
            
            success, out = run_step([PYTHON_EXE, str(ENGINE_ORCHESTRATOR), "--one-shot"], f"Optical Engine [{pulse_id}]")
            if not success: break
            
            output_folders = sorted(VAULT_ROOT.iterdir(), key=os.path.getmtime, reverse=True)
            if not output_folders: break
            pulse_output_dir = output_folders[0]
            run_id = pulse_output_dir.name.split("_M1.AG.")[0]
            
            update_ledger(cur, pulse_id, "OPTICAL", {"engine_output": str(pulse_output_dir), "run_id": run_id})
            conn.commit()

            # --- STATE: SEMANTIC ---
            pulse_mode_dir = MODES_DIR / pulse_id
            pulse_mode_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = BASE_DIR / "PDF_Parser_Engine" / "manifests" / "runs" / f"{run_id}.json"
            if manifest_path.exists():
                run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for page_id in run_manifest.get("page_ids", []):
                    page_manifest_path = BASE_DIR / "PDF_Parser_Engine" / "manifests" / "pages" / f"{page_id}.json"
                    if not page_manifest_path.exists(): continue
                    page_meta = json.loads(page_manifest_path.read_text(encoding="utf-8"))
                    page_num = page_meta.get("source_page_number", 1)
                    txt_path = pulse_output_dir / "ocr_text" / f"{page_id}.txt"
                    content = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
                    scaffold_json = {"page": {"page_id": page_id, "source_page_number": page_num, "page_machine_code": f"{pulse_id}.P{page_num:04d}"}, "text": {"content": content}, "extraction": {"agenda_items": []}}
                    (pulse_mode_dir / f"page_{page_num:04d}.json").write_text(json.dumps(scaffold_json, indent=2), encoding="utf-8")
            
            if not run_step([PYTHON_EXE, str(SCAFFOLDER), "--machine-code", pulse_id], "Scaffolder")[0]: break
            scaffold_md = pulse_mode_dir / f"{pulse_id}.SCF1.md"
            update_ledger(cur, pulse_id, "SEMANTIC", {"scaffold_md": str(scaffold_md)})
            conn.commit()

            # --- STATE: SCULPT/LOAD ---
            if not run_step([PYTHON_EXE, str(SCULPTOR), str(scaffold_md)], "Sculptor")[0]: break
            if not run_step([PYTHON_EXE, str(MIGRATOR), str(scaffold_md)], "Migrator")[0]: break
            if not run_step([PYTHON_EXE, str(REGISTRY), str(scaffold_md)], "Registry")[0]: break

            # --- VERIFICATION ---
            if not verify_accuracy(pulse_id):
                print(f"\n❌ ACCURACY FAILURE for {pulse_id}.")
                rollback_pulse(pulse_id)
                return

            # --- STATE: VAULT ---
            pulse_vault = VAULT_ROOT / pulse_id
            pulse_vault.mkdir(parents=True, exist_ok=True)
            ocr_text_dir = pulse_output_dir / "ocr_text"
            if ocr_text_dir.exists():
                with open(pulse_vault / f"{pulse_id}.txt", "w", encoding="utf-8") as out_f:
                    for txt_file in sorted(ocr_text_dir.glob("*.txt")):
                        out_f.write(f"--- PAGE {txt_file.stem} ---\n{txt_file.read_text(encoding='utf-8')}\n\n")
            if manifest_path.exists(): shutil.copy2(manifest_path, pulse_vault / f"{pulse_id}.factsheet.json")
            update_ledger(cur, pulse_id, "DONE", {"vault_path": str(pulse_vault)})
            conn.commit()

            # VAULT SOURCE
            VAULTED_SOURCES = PULL_DIR / "_vaulted"
            VAULTED_SOURCES.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdf), str(VAULTED_SOURCES / pdf.name))
            print(f">>> PULSE {pulse_id} SUCCESSFULLY CLOSED.")

            # --- BATCH PROGRESS ---
            count, batch_list = add_to_batch(pulse_id)
            print(f"--- BATCH PROGRESS: {count}/10 ---")
            if count >= 10:
                trigger_deep_inspection_batch(batch_list)

        cur.close()
        conn.close()

    except Exception as e:
        print(f"CRITICAL ERROR in Orchestrator: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-janitor", action="store_true")
    parser.add_argument("--pull-next", action="store_true")
    parser.add_argument("--clear-batch", action="store_true")
    args = parser.parse_args()
    orchestrate(skip_janitor=args.no_janitor, pull_next=args.pull_next, clear_batch=args.clear_batch)
