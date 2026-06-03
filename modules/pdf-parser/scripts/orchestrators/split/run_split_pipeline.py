import sys
import os
import subprocess

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    logger = PipelineLogger(log_dir, "run_split_pipeline")
    
    python_exe = sys.executable
    
    logger.info("ORCHESTRATION_START", "SUCCESS", run_id=run_id, message="Starting Split Pipeline")
    
    try:
        # Stage 1: Hash Ingest
        logger.info("STAGE_HASH", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "ingest", "hash_input_pdf.py"), run_id], check=True)
        
        # Stage 2: Split PDF
        logger.info("STAGE_SPLIT", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "split", "split_pdf_to_single_page_pdfs.py"), run_id], check=True)
        
        # Stage 3: Register Pages
        logger.info("STAGE_REGISTER", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "split", "register_split_pages.py"), run_id], check=True)
        
        logger.info("ORCHESTRATION_COMPLETE", "SUCCESS", run_id=run_id, message="Split Pipeline Finished successfully")
        
    except subprocess.CalledProcessError as e:
        logger.error("ORCHESTRATION_FAILED", "FAILURE", run_id=run_id, message=f"Subprocess failed: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error("ORCHESTRATION_ERROR", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_split_pipeline.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
