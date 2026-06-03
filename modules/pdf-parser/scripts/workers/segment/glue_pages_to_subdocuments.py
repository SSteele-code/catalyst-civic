import sys
import os
import json
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
    
    logger = PipelineLogger(log_dir, "glue_pages_to_subdocuments")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        segments = run_manifest.get("proposed_segments", [])
        
        segment_dir = os.path.join(base_dir, "work", "runs", run_id, "segments")
        os.makedirs(segment_dir, exist_ok=True)
        
        logger.info("SEGMENT_GLUING_START", "SUCCESS", run_id=run_id, message=f"Gluing {len(segments)} segments.")
        
        for i, seg in enumerate(segments):
            segment_id = f"{run_id}_SEG_{i:04d}"
            output_filename = f"{segment_id}_{seg['type']}.pdf"
            output_path = os.path.join(segment_dir, output_filename)
            
            # Spec Section 19.2: Physical Merge
            doc = fitz.open()
            for page_id in seg["pages"]:
                page_manifest = page_handler.load(page_id)
                page_path = os.path.join(base_dir, page_manifest["raw_pdf_path"])
                
                page_doc = fitz.open(page_path)
                doc.insert_pdf(page_doc)
                page_doc.close()
                
            doc.save(output_path)
            doc.close()
            
            seg["segment_id"] = segment_id
            seg["path"] = os.path.join("work", "runs", run_id, "segments", output_filename)
            
            logger.info("SEGMENT_CREATED", "SUCCESS", run_id=run_id, message=f"Created: {output_filename}")
            
        run_handler.update(run_id, {
            "segments": segments,
            "status": "segmentation_glued"
        })
        
        logger.info("SEGMENT_GLUING_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("SEGMENT_GLUING_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python glue_pages_to_subdocuments.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
