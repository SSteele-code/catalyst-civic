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
    
    logger = PipelineLogger(log_dir, "extract_native_text")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("NATIVE_EXTRACTION_START", "SUCCESS", run_id=run_id)
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            route = page_manifest.get("route_type")
            
            if route not in ["native_text_only", "native_text_plus_layout"]:
                continue
                
            native_text_path = os.path.join(base_dir, page_manifest["native_text_path"])
            ocr_text_dir = os.path.join(base_dir, "work", "runs", run_id, "ocr_text")
            os.makedirs(ocr_text_dir, exist_ok=True)
            
            output_filename = f"{page_id}.txt"
            output_path = os.path.join(ocr_text_dir, output_filename)
            
            # Since native text was already "extracted" during detection for scoring, 
            # we just move it to the canonical 'ocr_text' folder for stage consistency.
            shutil.copy(native_text_path, output_path)
            
            page_handler.update(page_id, {
                "ocr_text_path": os.path.join("work", "runs", run_id, "ocr_text", output_filename),
                "extraction_engine_used": "native_pymupdf",
                "current_state": "extraction_complete"
            })
            
            logger.info("NATIVE_EXTRACTED", "SUCCESS", run_id=run_id, page_id=page_id)
            
        logger.info("NATIVE_EXTRACTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("NATIVE_EXTRACTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_native_text.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
