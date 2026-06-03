import sys
import os
import fitz # PyMuPDF

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
    
    logger = PipelineLogger(log_dir, "detect_native_text_presence")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("NATIVE_DETECTION_START", "SUCCESS", run_id=run_id, message=f"Checking {len(page_ids)} pages.")
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            page_pdf_path = os.path.join(base_dir, page_manifest["raw_pdf_path"])
            native_text_dir = os.path.join(base_dir, "work", "runs", run_id, "native_text")
            os.makedirs(native_text_dir, exist_ok=True)
            
            doc = fitz.open(page_pdf_path)
            page = doc[0]
            text = page.get_text("text").strip()
            
            has_native_text = len(text) > 0
            
            if has_native_text:
                text_output_path = os.path.join(native_text_dir, f"{page_id}.txt")
                with open(text_output_path, "w", encoding="utf-8") as f:
                    f.write(text)
                
                page_handler.update(page_id, {
                    "native_text_detected": True,
                    "native_text_path": os.path.join("work", "runs", run_id, "native_text", f"{page_id}.txt"),
                    "current_state": "native_checked"
                })
            else:
                page_handler.update(page_id, {
                    "native_text_detected": False,
                    "current_state": "native_checked"
                })
                
            doc.close()
            logger.info("PAGE_CHECKED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Native text: {has_native_text}")
            
        run_handler.update(run_id, {"status": "native_detection_complete"})
        logger.info("NATIVE_DETECTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("NATIVE_DETECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_native_text_presence.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
