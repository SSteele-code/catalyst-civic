import sys
import os
import subprocess

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    logger = PipelineLogger(log_dir, "run_classification_pipeline")
    
    python_exe = sys.executable
    
    logger.info("ORCHESTRATION_START", "SUCCESS", run_id=run_id, message="Starting Classification & Sorting Pipeline")
    
    try:
        # Stage 1: Score Agenda
        logger.info("STAGE_SCORE_AGENDA", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "classify", "score_agenda_page.py"), run_id], check=True)
        
        # Stage 2: Score Budget
        logger.info("STAGE_SCORE_BUDGET", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "classify", "score_budget_page.py"), run_id], check=True)
        
        # Stage 3: Resolve Type
        logger.info("STAGE_RESOLVE", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "classify", "resolve_page_type.py"), run_id], check=True)
        
        # Stage 4: Recover Unknowns
        logger.info("STAGE_RECOVER_UNKNOWN", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "classify", "recover_unknown_pages.py"), run_id], check=True)
        
        # Stage 5: Sort into Buckets
        logger.info("STAGE_SORT", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "sort", "move_page_reference_to_bucket.py"), run_id], check=True)
        
        logger.info("ORCHESTRATION_COMPLETE", "SUCCESS", run_id=run_id, message="Classification Pipeline Finished successfully")
        
    except subprocess.CalledProcessError as e:
        logger.error("ORCHESTRATION_FAILED", "FAILURE", run_id=run_id, message=f"Subprocess failed: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error("ORCHESTRATION_ERROR", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_classification_pipeline.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
