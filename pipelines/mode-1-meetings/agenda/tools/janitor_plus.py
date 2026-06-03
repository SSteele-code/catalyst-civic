import os
import shutil
import sys
from pathlib import Path

# BASE_DIR is the project root (Agenda_Councilpacket)
BASE_DIR = Path(__file__).parent.parent.absolute()
PARSER_ENGINE_DIR = BASE_DIR / "PDF_Parser_Engine"

def clean_pycache(root: Path):
    count = 0
    for p in root.rglob("__pycache__"):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            count += 1
    if count > 0: print(f"Removed {count} __pycache__ directories.")

def force_clear_dir(path: Path):
    """Forcefully deletes all contents of a directory without deleting the directory itself."""
    if not path.exists(): return
    count = 0
    for item in path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            count += 1
        except Exception as e:
            print(f"Error removing {item}: {e}")
    if count > 0: print(f"Cleared {count} items from {path.name}")

def janitor_plus():
    print("--- STARTING ABSOLUTE JANITOR PURGE ---")
    
    # 1. Global Python Cleanup
    clean_pycache(BASE_DIR)
    
    # 2. Aggressive Engine Cleanup
    # We explicitly target the known 'buildup' directories
    targets = [
        PARSER_ENGINE_DIR / "work" / "runs",
        PARSER_ENGINE_DIR / "logs" / "runs",
        PARSER_ENGINE_DIR / "manifests" / "pages",
        PARSER_ENGINE_DIR / "manifests" / "regions",
        PARSER_ENGINE_DIR / "manifests" / "runs",
        PARSER_ENGINE_DIR / "manifests" / "segments",
        PARSER_ENGINE_DIR / "outbox",
        PARSER_ENGINE_DIR / "inbox"
    ]
    
    for target_path in targets:
        if target_path.exists():
            force_clear_dir(target_path)

    # 3. Clean root-level engine logs
    engine_pulse_log = PARSER_ENGINE_DIR / "logs" / "pulse.log"
    if engine_pulse_log.exists():
        engine_pulse_log.unlink()
        print("Cleared root engine pulse.log")

    print("--- JANITOR PLUS COMPLETE ---")

if __name__ == "__main__":
    janitor_plus()
