import sys
import os
import json

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    segment_manifest_dir = os.path.join(manifest_dir, "segments")
    os.makedirs(segment_manifest_dir, exist_ok=True)
    
    logger = PipelineLogger(log_dir, "generate_segment_manifests")
    run_handler = ManifestHandler(run_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        segments = run_manifest.get("segments", [])
        
        logger.info("SEGMENT_MANIFEST_START", "SUCCESS", run_id=run_id, message=f"Generating {len(segments)} manifests.")
        
        segment_ids = []
        for seg in segments:
            segment_id = seg["segment_id"]
            segment_ids.append(segment_id)
            
            # Segment Manifest (Spec Section 12.4)
            segment_data = {
                "run_id": run_id,
                "segment_id": segment_id,
                "segment_type": seg["type"],
                "page_ids": seg["pages"],
                "merged_pdf_path": seg["path"],
                "created_at": run_manifest["created_at"],
                "status": "minted"
            }
            
            manifest_path = os.path.join(segment_manifest_dir, f"{segment_id}.json")
            with open(manifest_path, "w") as f:
                json.dump(segment_data, f, indent=4)
                
            logger.info("SEGMENT_MANIFEST_CREATED", "SUCCESS", run_id=run_id, message=f"Minted: {segment_id}.json")
            
        run_handler.update(run_id, {
            "segment_ids": segment_ids,
            "status": "segmentation_complete"
        })
        
        logger.info("SEGMENT_MANIFEST_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("SEGMENT_MANIFEST_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python generate_segment_manifests.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
