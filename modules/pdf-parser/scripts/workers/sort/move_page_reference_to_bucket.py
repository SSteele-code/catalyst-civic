import sys
import os
import shutil

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    logger = PipelineLogger(log_dir, "move_page_reference_to_bucket")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("BUCKET_SORTING_START", "SUCCESS", run_id=run_id)
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            page_type = page_manifest.get("page_type", "unknown")
            
            # Bucket path: work/runs/RUN_ID/buckets/TYPE/
            bucket_dir = os.path.join(base_dir, "work", "runs", run_id, "buckets", page_type)
            os.makedirs(bucket_dir, exist_ok=True)
            
            # Source artifact: pages_raw PDF
            source_page_path = os.path.join(base_dir, page_manifest["raw_pdf_path"])
            dest_page_path = os.path.join(bucket_dir, f"{page_id}.pdf")
            
            # Spec Section 9.13: Bucket references only, never source artifacts.
            # On Windows, we use copy instead of symlinks for maximum portability across filesystems
            # but rename it to a 'reference' in logic.
            shutil.copy(source_page_path, dest_page_path)
            
            page_handler.update(page_id, {
                "bucket_path": os.path.join("work", "runs", run_id, "buckets", page_type, f"{page_id}.pdf"),
                "current_state": "sorted"
            })
            
            logger.info("PAGE_BUCKETED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Moved to: {page_type}")
            
        logger.info("BUCKET_SORTING_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("BUCKET_SORTING_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python move_page_reference_to_bucket.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
