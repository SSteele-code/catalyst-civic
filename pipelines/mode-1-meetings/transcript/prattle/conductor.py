import argparse
import json
import subprocess
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

# PRATTLE Conductor
# Mission: Orchestrate the 5 stages of PRATTLE and manage the industrial directories.
# OPTIMIZED: Supports Parallel Batch Processing and Incremental Sync.

VAULT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts\_Vualt\YTT")
STAGING_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts\_staging")
OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts\_output")
TRANSCRIPTS_ROOT = OUTPUT_ROOT.parent
DISPOSITION_LOG_FILE = TRANSCRIPTS_ROOT / "M1_TS_DISPOSITION_LOG.jsonl"
DISPOSITION_STATE_FILE = TRANSCRIPTS_ROOT / "transcript_disposition_state.json"

PRATTLE_DIR = Path(__file__).resolve().parent
PYTHON_EXECUTABLE = sys.executable or "python"
MIN_TURNS = 6
MIN_SQUEEZED_COVERAGE_RATIO = 0.95

def run_stage(script_name: str, args: list):
    cmd = [PYTHON_EXECUTABLE, str(PRATTLE_DIR / script_name)] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        combined = "\n".join([result.stdout.strip(), result.stderr.strip()]).strip()
        return False, combined or f"{script_name} failed with exit code {result.returncode}"
    return True, result.stdout.strip()

def cleanup_staging(machine_code: str):
    stage_dir = STAGING_ROOT / machine_code
    if stage_dir.exists():
        shutil.rmtree(stage_dir, ignore_errors=True)

def classify_disposition_code(machine_code: str, result: str) -> str:
    if result.startswith("[SUCCESS]"):
        return "OK95"
    if result.startswith("[SKIP]"):
        return "SKIP"
    if "No text found in VTT" in result:
        return "EMPTYVTT"
    if "insufficient_turns" in result:
        return "LOWSIG"
    if "low_squeezed_coverage" in result:
        return "LOWCOV95"
    if "unknown_saturation" in result:
        return "UNKSAT"
    if "Source" in result and "not found" in result:
        return "NOSRC"
    return "FAILED"

def infer_existing_disposition(machine_code: str) -> str:
    # 1) Try transcript QA metadata first.
    final_json = OUTPUT_ROOT / f"{machine_code}.json"
    if final_json.exists():
        try:
            payload = json.loads(final_json.read_text(encoding="utf-8"))
            qa = payload.get("qa_metrics") or {}
            disp = str(qa.get("disposition_code") or "").strip().upper()
            if disp and disp != "SKIP":
                return disp

            # Fallback inference for legacy payloads lacking disposition fields.
            total_turns = int(qa.get("total_turns")) if str(qa.get("total_turns", "")).isdigit() else None
            source_words = int(qa.get("source_words_squeezed")) if str(qa.get("source_words_squeezed", "")).isdigit() else None
            coverage = None
            try:
                raw_cov = qa.get("squeezed_coverage_ratio")
                coverage = float(raw_cov) if raw_cov is not None else None
            except Exception:
                coverage = None
            issues = qa.get("structural_issues") if isinstance(qa.get("structural_issues"), list) else None
            if (
                isinstance(issues, list)
                and len(issues) == 0
                and source_words is not None
                and source_words > 0
                and total_turns is not None
                and total_turns >= MIN_TURNS
                and coverage is not None
                and coverage >= MIN_SQUEEZED_COVERAGE_RATIO
            ):
                return "OK95"
        except Exception:
            pass
    # 2) Try state snapshot.
    if DISPOSITION_STATE_FILE.exists():
        try:
            state = json.loads(DISPOSITION_STATE_FILE.read_text(encoding="utf-8"))
            rec = (state.get("records") or {}).get(machine_code) or {}
            disp = str(rec.get("disposition_code") or "").strip().upper()
            if disp and disp != "SKIP":
                return disp
        except Exception:
            pass
    # 3) Fall back to latest non-SKIP in log history.
    if DISPOSITION_LOG_FILE.exists():
        try:
            lines = DISPOSITION_LOG_FILE.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except Exception:
                    continue
                if str(row.get("machine_code")) != machine_code:
                    continue
                disp = str(row.get("disposition_code") or "").strip().upper()
                if disp and disp != "SKIP":
                    return disp
        except Exception:
            pass
    return "SKIP"

def persist_disposition(machine_code: str, result: str):
    code = classify_disposition_code(machine_code, result)
    if code == "SKIP":
        code = infer_existing_disposition(machine_code)
    stamped_code = f"{machine_code}.{code}"
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "machine_code": machine_code,
        "machine_code_with_disposition": stamped_code,
        "disposition_code": code,
        "status": (
            "SUCCESS" if result.startswith("[SUCCESS]") else
            "SKIP" if result.startswith("[SKIP]") else
            "FAIL"
        ),
        "message": result,
    }

    TRANSCRIPTS_ROOT.mkdir(parents=True, exist_ok=True)
    with DISPOSITION_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")

    state = {"records": {}}
    if DISPOSITION_STATE_FILE.exists():
        try:
            state = json.loads(DISPOSITION_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {"records": {}}
    records = state.setdefault("records", {})
    records[machine_code] = {
        "updated_at": entry["timestamp"],
        "machine_code_with_disposition": stamped_code,
        "disposition_code": code,
        "status": entry["status"],
        "message": result,
    }
    DISPOSITION_STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

def process_transcript(machine_code: str):
    """Worker function for a single transcript pulse."""
    source_vtt = VAULT_ROOT / f"{machine_code}.vtt"
    final_json = OUTPUT_ROOT / f"{machine_code}.json"
    
    # 0. Skip Check (Incremental Sync)
    if final_json.exists():
        result = f"[SKIP] {machine_code} already exists in _output."
        persist_disposition(machine_code, result)
        return result

    if not source_vtt.exists():
        result = f"[ERROR] Source {machine_code}.vtt not found."
        persist_disposition(machine_code, result)
        return result
        
    start_time = time.time()
    staging_machine_dir = STAGING_ROOT / machine_code
    
    # Stage 1: INGEST
    ok, out = run_stage("ingest.py", ["--source", str(source_vtt), "--staging", str(STAGING_ROOT)])
    if not ok:
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 1 ({machine_code}): {out}"
        persist_disposition(machine_code, result)
        return result
    if not (staging_machine_dir / "normalized.json").exists():
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 1 ({machine_code}): normalized.json was not produced."
        persist_disposition(machine_code, result)
        return result
        
    # Stage 1.5: PHONETIC TRANSLATOR
    ok, out = run_stage("phonetic_translator.py", ["--staging", str(STAGING_ROOT / machine_code)])
    if not ok:
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 1.5 ({machine_code}): {out}"
        persist_disposition(machine_code, result)
        return result
    if not (staging_machine_dir / "normalized.json").exists():
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 1.5 ({machine_code}): normalized.json is missing after translation."
        persist_disposition(machine_code, result)
        return result
        
    # Stage 2: ROBERTS STATE
    ok, out = run_stage("roberts_state.py", ["--staging", str(STAGING_ROOT / machine_code)])
    if not ok:
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 2 ({machine_code}): {out}"
        persist_disposition(machine_code, result)
        return result
    if not (staging_machine_dir / "attributed.json").exists():
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 2 ({machine_code}): attributed.json was not produced."
        persist_disposition(machine_code, result)
        return result
        
    # Stage 3: QUOTER
    ok, out = run_stage("quoter.py", ["--staging", str(STAGING_ROOT / machine_code)])
    if not ok:
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 3 ({machine_code}): {out}"
        persist_disposition(machine_code, result)
        return result
    if not (staging_machine_dir / "quoted.json").exists():
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 3 ({machine_code}): quoted.json was not produced."
        persist_disposition(machine_code, result)
        return result
        
    # Stage 4: QA
    ok, out = run_stage("qa.py", ["--staging", str(STAGING_ROOT / machine_code)])
    if not ok:
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 4 ({machine_code}): {out}"
        persist_disposition(machine_code, result)
        return result
    if not final_json.exists():
        cleanup_staging(machine_code)
        result = f"[FAIL] Stage 4 ({machine_code}): final output JSON was not delivered."
        persist_disposition(machine_code, result)
        return result
    
    elapsed = time.time() - start_time
    cleanup_staging(machine_code)
    result = f"[SUCCESS] {machine_code} processed in {elapsed:.1f}s"
    persist_disposition(machine_code, result)
    return result

def run_batch(max_workers: int):
    """Processes all VTT files found in the Vault."""
    vtt_files = list(VAULT_ROOT.glob("M1.TS.*.vtt"))
    if not vtt_files:
        print(">>> No VTT files found in Vault.")
        return True

    print(f">>> Found {len(vtt_files)} files. Starting Parallel Batch (Workers: {max_workers})...")
    all_ok = True
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_transcript, f.stem): f.stem for f in vtt_files}
        
        for future in as_completed(futures):
            result = future.result()
            print(result)
            if result.startswith("[FAIL]") or result.startswith("[ERROR]"):
                all_ok = False
    return all_ok

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="Process a single machine code")
    parser.add_argument("--batch", action="store_true", help="Process all VTTs in the Vault")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel workers (default: 4)")
    args = parser.parse_args()
    
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    
    if args.batch:
        ok = run_batch(args.workers)
        raise SystemExit(0 if ok else 1)
    elif args.code:
        result = process_transcript(args.code)
        print(result)
        ok = not (result.startswith("[FAIL]") or result.startswith("[ERROR]"))
        raise SystemExit(0 if ok else 1)
    else:
        parser.print_help()
