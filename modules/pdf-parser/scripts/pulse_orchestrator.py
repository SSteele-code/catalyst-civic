import sys
from pathlib import Path

# Add project root to sys.path (CRIT-011 Refinement)
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

import os
import time
import shutil
import datetime
import uuid
import subprocess
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from src.common.validation import validate_run_id, validate_filename, validate_pdf
from src.common.manifest_handler import ManifestHandler
from src.common.constants import QUARANTINE_AUTHORITY, MAX_STAGE_RETRIES, SUBPROCESS_TIMEOUT_SECONDS

# --- CONFIGURATION ---
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

INBOX = BASE_DIR / "inbox"
OUTBOX = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas" / "_output"
WORK_DIR = BASE_DIR / "work" / "runs"
MANIFEST_DIR = BASE_DIR / "manifests" / "runs"
LOG_DIR = BASE_DIR / "logs"
QUARANTINE_DIR = BASE_DIR / "quarantine"
PYTHON_EXE = sys.executable

# Resource Limits & Budgets
MAX_MB = 500
MAX_PAGES = 2000
SUBPROCESS_TIMEOUT = SUBPROCESS_TIMEOUT_SECONDS
MAX_RETRIES = MAX_STAGE_RETRIES
MAX_CONCURRENT_RUNS = 4

def get_file_hash(file_path):
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def log(message, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] [{level}] {message}"
    print(log_msg, flush=True) # Ensure immediate flush
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "pulse.log", "a") as f:
        f.write(log_msg + "\n")

def generate_run_id():
    now = datetime.datetime.now()
    run_id = f"RUN_{now.strftime('%Y_%m_%d')}_{uuid.uuid4().hex[:4].upper()}"
    return validate_run_id(run_id)

def quarantine_run(run_id, reason):
    """3.1.3: Isolate failed runs under the authority of Integrity class."""
    run_path = WORK_DIR / run_id
    if not run_path.exists():
        return
    
    dest_path = QUARANTINE_DIR / run_id
    dest_path.mkdir(parents=True, exist_ok=True)
    
    log(f"QUARANTINE TRIGGERED BY {QUARANTINE_AUTHORITY} for {run_id}. Reason: {reason}", level="ERROR")
    
    try:
        # Move internal work to quarantine
        shutil.move(str(run_path), str(dest_path / "work"))
        
        # Move manifest to quarantine
        manifest_path = MANIFEST_DIR / f"{run_id}.json"
        if manifest_path.exists():
            shutil.move(str(manifest_path), str(dest_path / f"{run_id}.json"))
            
        with open(dest_path / "FAILURE_REASON.txt", "w") as f:
            f.write(f"Authority: {QUARANTINE_AUTHORITY}\nReason: {reason}\nTimestamp: {datetime.datetime.now().isoformat()}")
            
    except Exception as e:
        log(f"[{QUARANTINE_AUTHORITY}] Quarantine failed for {run_id}: {str(e)}", level="CRITICAL")

def run_stage(stage_name, run_id, script_relative_path, handler=None):
    script_path = BASE_DIR / script_relative_path
    
    if handler:
        try:
            manifest = handler.load(run_id)
            completed_stages = manifest.get("completed_stages", [])
            if stage_name in completed_stages:
                log(f"[{run_id}] Stage {stage_name} already completed. Skipping.", level="INFO")
                return True
        except Exception:
            pass

    for attempt in range(1, MAX_RETRIES + 2):
        try:
            log(f"[{run_id}] {stage_name} - Attempt {attempt}")
            subprocess.run(
                [PYTHON_EXE, str(script_path), run_id],
                check=True,
                timeout=SUBPROCESS_TIMEOUT,
                capture_output=True
            )
            
            if handler:
                manifest = handler.load(run_id)
                stages = manifest.get("completed_stages", [])
                if stage_name not in stages:
                    stages.append(stage_name)
                handler.update(run_id, {"completed_stages": stages, "last_stage": stage_name})
                
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            err_msg = e.stderr.decode() if hasattr(e, 'stderr') and e.stderr else str(e)
            log(f"[{run_id}] {stage_name} FAILED (Attempt {attempt}): {err_msg}", level="WARNING")
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"Stage {stage_name} failed after {attempt} attempts: {err_msg}")
            time.sleep(5)
    return False

def process_pdf(pdf_path):
    pdf_path = Path(pdf_path)
    run_id = None
    handler = ManifestHandler(MANIFEST_DIR)
    
    try:
        original_filename = validate_filename(pdf_path.name)
        validate_pdf(pdf_path, max_mb=MAX_MB, max_pages=MAX_PAGES)
        
        run_id = generate_run_id()
        source_hash = get_file_hash(pdf_path)
        run_path = WORK_DIR / run_id
        
        log(f"NEW ASSET DETECTED: {original_filename} -> ASSIGNED ID: {run_id}")
        
        (run_path / "input").mkdir(parents=True, exist_ok=True)
        (run_path / "output").mkdir(parents=True, exist_ok=True)
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        
        renamed_source_path = run_path / "input" / f"{run_id}.pdf"
        shutil.move(str(pdf_path), str(renamed_source_path))
        
        manifest = {
            "run_id": run_id,
            "source_pdf_original_name": original_filename,
            "source_pdf_internal_name": f"{run_id}.pdf",
            "source_pdf_hash": source_hash,
            "created_at": datetime.datetime.now().isoformat(),
            "status": "registered",
            "page_count": 0,
            "quarantine_flag": False,
            "completed_stages": []
        }
        handler.save(run_id, manifest)

        run_stage("SPLIT", run_id, "scripts/orchestrators/split/run_split_pipeline.py", handler)
        run_stage("DETECT", run_id, "scripts/orchestrators/detect/run_native_detection_pipeline.py", handler)
        run_stage("LAYOUT", run_id, "scripts/orchestrators/layout/run_layout_pipeline.py", handler)
        run_stage("EXTRACT", run_id, "scripts/orchestrators/extract/run_extraction_pipeline.py", handler)
        run_stage("CLASSIFY", run_id, "scripts/orchestrators/classify/run_classification_pipeline.py", handler)
        run_stage("SEGMENT", run_id, "scripts/orchestrators/segment/run_segmentation_pipeline.py", handler)

        # --- COLLECTION (Assemble all artifacts for delivery) ---
        log(f"[{run_id}] COLLECTING ARTIFACTS FOR DELIVERY...")
        for artifact in ["segments", "buckets", "ocr_text", "pages_rendered", "pages_normalized"]:
            src_art = run_path / artifact
            if src_art.exists():
                shutil.move(str(src_art), str(run_path / "output" / artifact))

        with open(run_path / "output" / "SUCCESS.txt", "w") as f:
            f.write(f"Run {run_id} completed successfully.")
            
        final_delivery_path = OUTBOX / f"{run_id}_{original_filename[:-4]}"
        shutil.copytree(str(run_path / "output"), str(final_delivery_path))
        # shutil.rmtree(run_path) # Disable cleanup for forensic deep dive
        
        log(f"[{run_id}] SUCCESS. OUTPUT DELIVERED TO: {final_delivery_path}")
        
    except Exception as e:
        log(f"CRITICAL FAILURE: {str(e)}", level="ERROR")
        if run_id:
            quarantine_run(run_id, str(e))
        else:
            QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
            if pdf_path.exists():
                shutil.move(str(pdf_path), str(QUARANTINE_DIR / f"FAILED_INGEST_{pdf_path.name}"))

def main_loop(one_shot=False):
    log(f"FACTORY INITIALIZED. MODE: {'ONE-SHOT' if one_shot else 'DAEMON'} (Max Workers: {MAX_CONCURRENT_RUNS})...")
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS) as executor:
        # Initial check for Interrupted Runs (MED-001)
        if WORK_DIR.exists():
            interrupted_runs = [d for d in WORK_DIR.iterdir() if d.is_dir() and not (d / "output" / "SUCCESS.txt").exists()]
            for run_dir in interrupted_runs:
                log(f"RESUMING INTERRUPTED RUN: {run_dir.name}")
                executor.submit(process_interrupted_run, run_dir.name)

        while True:
            try:
                if not INBOX.exists():
                    INBOX.mkdir(parents=True, exist_ok=True)
                
                files = [f for f in os.listdir(INBOX) if f.lower().endswith(".pdf")]
                if files:
                    futures = []
                    for file in files:
                        full_path = INBOX / file
                        try:
                            # Verify file is not being written to
                            with open(full_path, "rb") as f:
                                pass
                            futures.append(executor.submit(process_pdf, full_path))
                        except IOError:
                            continue
                    
                    if one_shot:
                        # Wait for all submitted tasks to complete
                        for future in futures:
                            future.result()
                        log("ONE-SHOT PROCESSING COMPLETE. SHUTTING DOWN.")
                        break
                elif one_shot:
                    log("INBOX EMPTY. ONE-SHOT COMPLETE.")
                    break
                
                time.sleep(2)
            except KeyboardInterrupt:
                log("FACTORY SHUTTING DOWN.")
                break
            except Exception as e:
                log(f"SYSTEM ERROR: {str(e)}", level="CRITICAL")
                if one_shot: break
                time.sleep(5)

def process_interrupted_run(run_id):
    """Resume a run from its last checkpoint."""
    handler = ManifestHandler(MANIFEST_DIR)
    try:
        run_path = WORK_DIR / run_id
        # We need the original filename for delivery, which is in the manifest
        manifest = handler.load(run_id)
        original_filename = manifest["source_pdf_original_name"]
        
        log(f"[{run_id}] RESUMING PIPELINE FROM CHECKPOINT.")
        
        # 4. EXECUTE PIPELINE (run_stage handles skipping completed stages)
        run_stage("SPLIT", run_id, "scripts/orchestrators/split/run_split_pipeline.py", handler)
        run_stage("DETECT", run_id, "scripts/orchestrators/detect/run_native_detection_pipeline.py", handler)
        run_stage("LAYOUT", run_id, "scripts/orchestrators/layout/run_layout_pipeline.py", handler)
        run_stage("EXTRACT", run_id, "scripts/orchestrators/extract/run_extraction_pipeline.py", handler)
        run_stage("CLASSIFY", run_id, "scripts/orchestrators/classify/run_classification_pipeline.py", handler)
        run_stage("SEGMENT", run_id, "scripts/orchestrators/segment/run_segmentation_pipeline.py", handler)

        # --- COLLECTION (Assemble all artifacts for delivery) ---
        log(f"[{run_id}] COLLECTING ARTIFACTS FOR DELIVERY...")
        for artifact in ["segments", "buckets", "ocr_text", "pages_rendered", "pages_normalized"]:
            src_art = run_path / artifact
            if src_art.exists():
                shutil.move(str(src_art), str(run_path / "output" / artifact))

        # --- DELIVERY ---
        with open(run_path / "output" / "SUCCESS.txt", "w") as f:
            f.write(f"Run {run_id} resumed and completed successfully.")
            
        final_delivery_path = OUTBOX / f"{run_id}_{original_filename[:-4]}"
        shutil.copytree(str(run_path / "output"), str(final_delivery_path))
        shutil.rmtree(run_path)
        
        log(f"[{run_id}] RESUME SUCCESS. OUTPUT DELIVERED TO: {final_delivery_path}")
        
    except Exception as e:
        log(f"RESUME FAILURE [{run_id}]: {str(e)}", level="ERROR")
        quarantine_run(run_id, str(e))

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--one-shot", action="store_true")
    args = parser.parse_args()
    main_loop(one_shot=args.one_shot)
