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
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    os.makedirs(page_manifest_dir, exist_ok=True)
    
    logger = PipelineLogger(log_dir, "register_split_pages")
    handler = ManifestHandler(run_manifest_dir)
    
    try:
        manifest = handler.load(run_id)
        page_count = manifest.get("page_count", 0)
        
        logger.info("REGISTRATION_START", "SUCCESS", run_id=run_id, message=f"Registering {page_count} pages.")
        
        page_ids = []
        document_machine_code = manifest.get("document_machine_code")
        for i in range(page_count):
            page_num = i + 1
            page_id = f"{run_id}_P{page_num:04d}"
            page_machine_code = f"{document_machine_code}_P{page_num:04d}" if document_machine_code else None
            page_ids.append(page_id)
            
            # Create Page Manifest (Spec Section 12.2)
            page_data = {
                "run_id": run_id,
                "page_id": page_id,
                "run_page_id": page_id,
                "document_machine_code": document_machine_code,
                "page_machine_code": page_machine_code,
                "source_page_number": page_num,
                "source_pdf_name": manifest.get("source_pdf_display_name") or manifest.get("source_pdf_intake_name"),
                "source_pdf_intake_name": manifest.get("source_pdf_intake_name"),
                "source_pdf_original_name": manifest.get("source_pdf_original_name"),
                "source_pdf_alias_name": manifest.get("source_pdf_alias_name"),
                "raw_pdf_path": os.path.join("work", "runs", run_id, "pages_raw", f"{page_id}.pdf"),
                "current_state": "split",
                "page_type": "unknown",
                "page_type_confidence": 0.0,
                "route_type": "unknown",
                "route_confidence": 0.0,
                "native_text_detected": False,
                "native_text_quality_score": 0.0,
                "handwriting_detected": False, # 3.2.2
                "escalation_policy": "none",
                "review_required": False,
                "quarantine_flag": False
            }
            
            page_manifest_path = os.path.join(page_manifest_dir, f"{page_id}.json")
            with open(page_manifest_path, "w") as f:
                json.dump(page_data, f, indent=4)
                
            logger.info("PAGE_REGISTERED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Initialized manifest: {page_id}.json")
            
        handler.update(run_id, {"page_ids": page_ids, "status": "registered"})
        logger.info("REGISTRATION_COMPLETE", "SUCCESS", run_id=run_id, message=f"Registered {page_count} pages.")
        
    except Exception as e:
        logger.error("REGISTRATION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python register_split_pages.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
