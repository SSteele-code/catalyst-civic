import os
import hashlib
import json
from pathlib import Path

SOURCE_DIR = Path(os.getenv("CC_DATA_ROOT", r"C:\CatalystCivic")) / "_Sources" / "M1-Meetings" / "Agendas"
MANIFEST_PATH = SOURCE_DIR / "M1_AGENDAS_MANIFEST.jsonl"

def get_hash(file_path):
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def cleanup_sources():
    print(f"Scanning {SOURCE_DIR} for duplicates...")
    pdf_files = list(SOURCE_DIR.glob("*.pdf"))
    
    hashes = {}
    duplicates = []
    originals = {}

    for pdf in pdf_files:
        h = get_hash(pdf)
        if h in hashes:
            # We found a duplicate. 
            # Prefer the one that follows the M1.AG.NNNNNN naming convention if possible
            existing_pdf = hashes[h]
            if pdf.name.startswith("M1.AG.") and not existing_pdf.name.startswith("M1.AG."):
                duplicates.append(existing_pdf)
                hashes[h] = pdf
            else:
                duplicates.append(pdf)
        else:
            hashes[h] = pdf

    print(f"Found {len(duplicates)} duplicate files.")
    for dup in duplicates:
        print(f"Deleting duplicate: {dup.name}")
        dup.unlink()

    # Clean the manifest
    if MANIFEST_PATH.exists():
        print("Cleaning manifest...")
        valid_hashes = set(hashes.keys())
        cleaned_lines = []
        with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line)
                    # This depends on how the manifest stores info. 
                    # If it has a file_hash or we check the file existence
                    file_name = data.get("file_name") or data.get("agenda_id")
                    if not file_name: continue
                    
                    # Check if the file still exists
                    if (SOURCE_DIR / Path(file_name).name).exists():
                        cleaned_lines.append(line)
                except:
                    continue
        
        with open(MANIFEST_PATH, 'w', encoding='utf-8') as f:
            for line in cleaned_lines:
                f.write(line)
        print(f"Manifest cleaned. {len(cleaned_lines)} records remaining.")

if __name__ == "__main__":
    cleanup_sources()
