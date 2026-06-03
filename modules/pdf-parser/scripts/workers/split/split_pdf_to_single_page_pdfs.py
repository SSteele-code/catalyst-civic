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
    manifest_dir = os.path.join(base_dir, "manifests", "runs")
    
    logger = PipelineLogger(log_dir, "split_pdf_to_single_page_pdfs")
    handler = ManifestHandler(manifest_dir)
    
    try:
        manifest = handler.load(run_id)
        input_path = os.path.join(base_dir, "work", "runs", run_id, "input", manifest["source_pdf_internal_name"])
        pages_raw_dir = os.path.join(base_dir, "work", "runs", run_id, "pages_raw")
        os.makedirs(pages_raw_dir, exist_ok=True)
        
        logger.info("SPLIT_START", "SUCCESS", run_id=run_id, message=f"Splitting: {input_path}")
        
        doc = fitz.open(input_path)
        page_count = len(doc)
        
        for i in range(page_count):
            page_num = i + 1
            # Spec Section 13.2: Page ID = {RUN_ID}_P####
            page_id = f"{run_id}_P{page_num:04d}"
            output_filename = f"{page_id}.pdf"
            output_path = os.path.join(pages_raw_dir, output_filename)
            
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=i, to_page=i)
            new_doc.save(output_path)
            new_doc.close()
            
            logger.info("PAGE_SPLIT", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Created {output_filename}")
            
        doc.close()
        
        handler.update(run_id, {"page_count": page_count, "status": "split_complete"})
        logger.info("SPLIT_COMPLETE", "SUCCESS", run_id=run_id, message=f"Total pages: {page_count}")
        
    except Exception as e:
        logger.error("SPLIT_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python split_pdf_to_single_page_pdfs.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
