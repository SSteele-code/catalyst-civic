import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from html import unescape
from pathlib import Path

# PRATTLE Production Line
# Mission: Orchestrate FIFO Batch Processing (Pull -> Process -> Verify -> Log)

TRANSCRIPT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Scripts\Mode_1_MEETINGS\STATE\VA-Virginia\TRANSCRIPT")
VAULT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts\_Vualt\YTT")
OUTPUT_ROOT = Path(r"C:\Users\simon\CatalystCivic\_Sources\M1-Meetings\Transcripts\_output")
LEDGER_FILE = TRANSCRIPT_ROOT / "PRODUCTION_LEDGER.jsonl"

MIN_SQUEEZED_COVERAGE_RATIO = 0.95
PYTHON_EXECUTABLE = sys.executable or "python"


def run_command(cmd_list, cwd):
    result = subprocess.run(cmd_list, capture_output=True, text=True, cwd=cwd)
    return result.returncode == 0, result.stdout, result.stderr


def parse_vtt_with_timestamps(raw_content: str) -> list[dict]:
    entries = []
    current_time = None

    for line in raw_content.splitlines():
        line_unescaped = unescape(line)
        if "-->" in line_unescaped:
            match = re.search(r"(?:(\d{1,2}):)?(\d{2}):(\d{2}\.\d{3})", line_unescaped)
            if match:
                h_part, m_part, s_part = match.groups()
                hours = int(h_part) if h_part is not None else 0
                minutes = int(m_part)
                seconds = float(s_part)
                current_time = hours * 3600 + minutes * 60 + seconds
        elif (
            current_time is not None
            and line_unescaped.strip()
            and not line_unescaped.startswith("WEBVTT")
            and not line_unescaped.startswith("Kind:")
            and not line_unescaped.startswith("Language:")
        ):
            text = re.sub(r"<[^>]+>", "", line_unescaped).strip()
            if text:
                entries.append({"ts": current_time, "text": text})
    return entries


def merge_overlapping_text(parts: list[str]) -> list[str]:
    if not parts:
        return []

    merged = [parts[0]]
    for i in range(1, len(parts)):
        prev = merged[-1]
        curr = parts[i]

        prev_words = prev.split()
        curr_words = curr.split()
        prev_lower = [w.lower().strip(".,!?:;") for w in prev_words]
        curr_lower = [w.lower().strip(".,!?:;") for w in curr_words]

        max_overlap = 0
        search_limit = min(len(prev_lower), len(curr_lower))
        for length in range(1, search_limit + 1):
            if prev_lower[-length:] == curr_lower[:length]:
                max_overlap = length

        if max_overlap > 0:
            new_part_words = curr_words[max_overlap:]
            if new_part_words:
                merged[-1] += " " + " ".join(new_part_words)
        else:
            curr_str_clean = " ".join(curr_lower)
            prev_str_clean = " ".join(prev_lower)
            if curr_str_clean and curr_str_clean in prev_str_clean:
                continue
            merged.append(curr)
    return merged


def build_squeezed_source_text(vtt_raw: str) -> str:
    entries = parse_vtt_with_timestamps(vtt_raw)
    if not entries:
        return ""

    parts = []
    for i, entry in enumerate(entries):
        text = entry["text"]
        if i > 0:
            gap = entry["ts"] - entries[i - 1]["ts"]
            if gap > 1.5 and parts and not parts[-1].endswith((".", "!", "?")):
                parts[-1] += "."
        parts.append(text)

    squeezed = " ".join(merge_overlapping_text(parts))
    squeezed = unescape(squeezed)
    squeezed = squeezed.replace(">>", " ").replace("&gt;&gt;", " ")
    return re.sub(r"\s+", " ", squeezed).strip()


def verify_integrity(machine_code: str):
    """
    Checks if the JSON output preserves source text against the squeezed transcript
    baseline (not raw overlapping caption words).
    """
    vtt_file = VAULT_ROOT / f"{machine_code}.vtt"
    json_file = OUTPUT_ROOT / f"{machine_code}.json"

    if not vtt_file.exists() or not json_file.exists():
        return False, "Missing files for verification."

    vtt_raw = vtt_file.read_text(encoding="utf-8", errors="replace")
    source_squeezed = build_squeezed_source_text(vtt_raw)
    source_words = re.findall(r"\w+", source_squeezed.lower())
    source_word_count = len(source_words)

    if source_word_count == 0:
        return False, "Coverage Warning (Squeezed source empty/unparseable)."

    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        turns = data.get("turns", [])
        json_text_full = " ".join((t or {}).get("text", "") for t in turns).lower()
        json_words_list = re.findall(r"\w+", json_text_full)
        json_total_count = len(json_words_list)

    squeeze_ratio = json_total_count / source_word_count if source_word_count > 0 else 0.0

    source_anchor_words = [w for w in source_words[-30:] if len(w) > 1]
    json_tail = " ".join((t or {}).get("text", "") for t in turns[-30:]).lower()

    coverage_pass = False
    for i in range(len(source_anchor_words) - 1):
        anchor = " ".join(source_anchor_words[i : i + 2])
        if anchor in json_tail:
            coverage_pass = True
            break

    pass_status = squeeze_ratio >= MIN_SQUEEZED_COVERAGE_RATIO and coverage_pass
    if pass_status:
        return True, f"Integrity Pass (Squeezed Ratio: {squeeze_ratio:.3f}, Tail Verified)"
    if not coverage_pass:
        return False, f"Coverage Warning (Squeezed Ratio: {squeeze_ratio:.3f}, Tail Missing)"
    return False, f"Coverage Warning (Squeezed Ratio: {squeeze_ratio:.3f}, Threshold < {MIN_SQUEEZED_COVERAGE_RATIO:.2f})"


def process_batch(batch_size: int, workers: int) -> bool:
    """Processes a single batch. Returns True if ALL passed, False if any failed."""
    print(f"\n>>> Executing Production Batch (size={batch_size})")

    # 1. PULL
    ok, out, err = run_command(
        [PYTHON_EXECUTABLE, "orchestrator.py", "--limit", str(batch_size)],
        TRANSCRIPT_ROOT / "PULL",
    )
    if not ok:
        print(f"Error in PULL: {err}")
        return False

    # Identify what was pulled.
    manifest_file = VAULT_ROOT / "M1_TS_MANIFEST.jsonl"
    all_codes = []
    if manifest_file.exists():
        with open(manifest_file, "r", encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                all_codes.append(row["machine_code"])

    latest_codes = all_codes[-batch_size:]

    batch_success = True
    for code in latest_codes:
        run_ok, run_out, run_err = run_command(
            [PYTHON_EXECUTABLE, "conductor.py", "--code", code],
            TRANSCRIPT_ROOT / "PRATTLE",
        )
        if not run_ok:
            batch_success = False

        pass_status, msg = verify_integrity(code)
        print(f"[{code}] {msg}")

        ledger_entry = {
            "timestamp": datetime.now().isoformat(),
            "machine_code": code,
            "status": "SUCCESS" if (run_ok and pass_status) else "WARNING",
            "conductor_ok": run_ok,
            "conductor_stderr": run_err.strip(),
            "integrity_msg": msg,
        }

        with open(LEDGER_FILE, "a", encoding="utf-8") as lf:
            lf.write(json.dumps(ledger_entry) + "\n")

        if not pass_status:
            batch_success = False

    return batch_success


def run_deep_production(batch_size: int, workers: int):
    print(">>> AGENTIC MODE: Deep Production Loop Engaged.")
    print(">>> Safety Rule: Loop will halt on any Integrity Warning.")

    batch_count = 0
    while True:
        batch_count += 1
        print(f"\n=== BATCH #{batch_count} ===")
        success = process_batch(batch_size, workers)

        if not success:
            print("\n!!! PRODUCTION HALTED: Integrity verification required for last batch.")
            break

        print(">>> Batch Success. Proceeding to next pulse in 5s...")
        time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=4, help="Number of videos to process in this run")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers for the machine")
    parser.add_argument("--continuous", action="store_true", help="Run agentic loop until failure")
    args = parser.parse_args()

    if args.continuous:
        run_deep_production(args.count, args.workers)
    else:
        process_batch(args.count, args.workers)
