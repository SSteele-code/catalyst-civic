import os
import sys
import time
import json
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from faster_whisper import WhisperModel

# =============================================================================
# THE MASTER DISTILLER (v2.0 CANON)
# Mission: High-fidelity, optimized, parallel auditory transcription.
# =============================================================================

DISTILLERY_ROOT = Path(__file__).parent
WORKSPACE_DIR = DISTILLERY_ROOT / "_workspace"
VAULT_DIR = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Transcripts" / "_Vualt" / "YTT"
SOURCE_DIR = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Transcripts" / "_Auditory" / "_source"

CHUNK_TIME_SEC = 300

# MUNICIPAL INITIAL PROMPT (The "Canon Glossary")
# Mission: Bias the model towards known names and terms to prevent hallucinations.
MUNICIPAL_PROMPT = (
    "Richlands, Virginia, Town Council, Mayor Curry, Seth White, Gary Jackson, "
    "Rick Wood, Jordan Bales, Jan White, Laura Mollo, Town Manager Ron Holt, "
    "Virginia Code Section 2.2-3711, Lexite Corporation, VCEDA, PSA, CART, "
    "motion, second, roll call, ordinance, resolution."
)

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run_ffmpeg_fusion(source_file, machine_code):
    """Stage 1 & 2 Fusion: Normalize and Segment in one pass."""
    work_dir = WORKSPACE_DIR / machine_code
    
    # Resume Check: If chunks already exist, skip FFmpeg
    existing_chunks = sorted(list(work_dir.glob("chunk_*.wav")))
    if existing_chunks:
        log(f">>> [BOTTLING] Found {len(existing_chunks)} existing chunks. Resuming...")
        return existing_chunks, work_dir

    work_dir.mkdir(parents=True, exist_ok=True)
    log(f">>> [BOTTLING] Fused Normalization & Segmentation for {machine_code}...")
    
    # We use a single pass to apply loudnorm and segment to chunks
    cmd = [
        "ffmpeg", "-y", "-i", str(source_file),
        "-ar", "16000", "-ac", "1",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-f", "segment", "-segment_time", str(CHUNK_TIME_SEC),
        "-c:a", "pcm_s16le",
        str(work_dir / "chunk_%03d.wav")
    ]
    
    try:
        # Run with stderr directed to a pipe so we can potentially monitor progress
        # For now, we just wait, but we allow it to take a long time.
        subprocess.run(cmd, check=True, capture_output=True)
        chunks = sorted(list(work_dir.glob("chunk_*.wav")))
        log(f">>> [BOTTLING] Success: {len(chunks)} chunks minted.")
        return chunks, work_dir
    except subprocess.CalledProcessError as e:
        log(f"!!! [BOTTLING] FFmpeg Failed.")
        return None, None

def transcribe_chunk(model, chunk_path):
    """Process a single chunk."""
    try:
        segments, info = model.transcribe(
            str(chunk_path),
            beam_size=1,
            language="en",
            vad_filter=True, # Prevent hallucinations in silence
            word_timestamps=False, # Speed boost
            condition_on_previous_text=False, # Prevent infinite loops
            initial_prompt=MUNICIPAL_PROMPT # BIAS ENGINE
        )
        
        output = []
        for seg in segments:
            output.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
            # Safety Valve: If we get more than 500 segments in 5 minutes, something is wrong
            if len(output) > 500:
                log(f"    ! Safety Valve Triggered for {chunk_path.name}. Truncating.")
                break
        
        json_path = chunk_path.with_suffix('.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(output, f)
            
        return True, chunk_path.name
    except Exception as e:
        return False, f"{chunk_path.name}: {str(e)}"

def format_timestamp(seconds: float) -> str:
    td = datetime.fromtimestamp(seconds) - datetime.fromtimestamp(0)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    millis = int(td.microseconds / 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

def reassemble_vault(machine_code, work_dir, chunk_count):
    """Stage 4: Reassemble into final VTT."""
    log(f">>> [VAULT] Reassembling {chunk_count} fragments...")
    vtt_path = VAULT_DIR / f"{machine_code}.vtt"
    
    with open(vtt_path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        
        for idx in range(chunk_count):
            json_path = work_dir / f"chunk_{idx:03d}.json"
            if not json_path.exists():
                log(f"    ! Warning: Missing {json_path.name}, skipping.")
                continue
                
            # Offset based on index
            offset = idx * CHUNK_TIME_SEC
            
            with open(json_path, "r", encoding="utf-8") as jf:
                segments = json.load(jf)
                
            for seg in segments:
                start_abs = seg['start'] + offset
                end_abs = seg['end'] + offset
                f.write(f"{format_timestamp(start_abs)} --> {format_timestamp(end_abs)}\n")
                f.write(f"{seg['text']}\n\n")
                
    log(f">>> [VAULT] Master VTT minted at {vtt_path.name}")
    return vtt_path

def run_pipeline(machine_code):
    print(f"\n{'='*60}")
    print(f"IGNITING MASTER DISTILLER: {machine_code}")
    print(f"{'='*60}")
    
    source_file = SOURCE_DIR / f"{machine_code}.m4a"
    if not source_file.exists():
        log(f"!!! Error: Source file {source_file} not found.")
        return False
        
    start_time = time.time()
    
    # STAGE 1 & 2: Fusion Bottling
    chunks, work_dir = run_ffmpeg_fusion(source_file, machine_code)
    if not chunks:
        return False
        
    # STAGE 3: Parallel Distillation
    log(f">>> [WARMUP] Loading WhisperModel (medium) with 3 parallel workers...")
    # num_workers=3 allows the CTranslate2 model to process 3 chunks in parallel safely
    model = WhisperModel("medium", device="cpu", compute_type="int8", cpu_threads=2, num_workers=3)
    
    log(f">>> [DISTILL] Eating {len(chunks)} chunks (Parallel: 3)...")
    success_count = 0
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(transcribe_chunk, model, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            ok, msg = future.result()
            if ok:
                log(f"    > Finished {msg}")
                success_count += 1
            else:
                log(f"    !!! ERROR on {msg}")
                
    if success_count == 0:
        log("!!! All chunks failed. Aborting.")
        return False
        
    # STAGE 4: Vault Reassembly
    reassemble_vault(machine_code, work_dir, len(chunks))
    
    # CLEANUP
    shutil.rmtree(work_dir, ignore_errors=True)
    
    elapsed = time.time() - start_time
    log(f">>> [DONE] {machine_code} completed in {elapsed/60:.1f} minutes.")
    return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", help="Process a single machine code (e.g., M1.TS.000163.NA.20260501)")
    parser.add_argument("--batch", action="store_true", help="Process all missing transcripts in source directory")
    args = parser.parse_args()
    
    if not VAULT_DIR.exists():
        VAULT_DIR.mkdir(parents=True)
    
    if args.code:
        run_pipeline(args.code)
    elif args.batch:
        sources = list(SOURCE_DIR.glob("*.m4a"))
        log(f"Found {len(sources)} total source files.")
        for idx, src in enumerate(sources):
            machine_code = src.stem
            target_vtt = VAULT_DIR / f"{machine_code}.vtt"
            if target_vtt.exists():
                log(f"[SKIP {idx+1}/{len(sources)}] {machine_code} already transcribed.")
                continue
            run_pipeline(machine_code)
    else:
        parser.print_help()