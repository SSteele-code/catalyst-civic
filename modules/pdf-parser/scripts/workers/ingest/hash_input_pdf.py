import sys
import os
import hashlib
import json

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def get_file_hash(file_path):
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests", "runs")
    
    logger = PipelineLogger(log_dir, "hash_input_pdf")
    handler = ManifestHandler(manifest_dir)
    
    try:
        manifest = handler.load(run_id)
        input_path = os.path.join(base_dir, "work", "runs", run_id, "input", manifest["source_pdf_internal_name"])
        
        logger.info("HASHING_START", "SUCCESS", run_id=run_id, message=f"Hashing file: {input_path}")
        
        current_hash = get_file_hash(input_path)
        
        # Verify against initial register hash if exists
        if manifest.get("source_pdf_hash") and manifest["source_pdf_hash"] != current_hash:
            logger.error("HASH_MISMATCH", "FAILURE", run_id=run_id, message="Calculated hash does not match registration hash.")
            sys.exit(1)
            
        handler.update(run_id, {"source_pdf_hash": current_hash, "status": "registered"})
        logger.info("HASHING_COMPLETE", "SUCCESS", run_id=run_id, message=f"Hash: {current_hash}")
        
    except Exception as e:
        logger.error("HASHING_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hash_input_pdf.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
