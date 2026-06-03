import sys
import os
import subprocess

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    logger = PipelineLogger(log_dir, "run_layout_pipeline")
    
    python_exe = sys.executable
    
    logger.info("ORCHESTRATION_START", "SUCCESS", run_id=run_id, message="Starting Preprocess & Layout Pipeline")
    
    try:
        # Stage 1: Detect Skew
        logger.info("STAGE_SKEW_DETECT", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "preprocess", "detect_skew.py"), run_id], check=True)
        
        # Stage 2: Correct Skew (Normalize Artifacts)
        logger.info("STAGE_SKEW_CORRECT", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "preprocess", "correct_skew.py"), run_id], check=True)
        
        # --- CALIBRATED INPUT: ALL SUBSEQUENT WORKERS MUST USE NORMALIZED ARTIFACTS ---
        
        # Stage 3: Detect Handwriting (3.2.2)
        logger.info("STAGE_LAYOUT_HANDWRITING", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "layout", "detect_handwriting.py"), run_id], check=True)

        # Stage 4: Detect Text Regions
        logger.info("STAGE_LAYOUT_TEXT", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "layout", "detect_text_regions.py"), run_id], check=True)
        
        # Stage 5: Detect Table Regions
        logger.info("STAGE_LAYOUT_TABLE", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "layout", "detect_table_regions.py"), run_id], check=True)
        
        logger.info("ORCHESTRATION_COMPLETE", "SUCCESS", run_id=run_id, message="Layout Pipeline Finished successfully")
        
    except subprocess.CalledProcessError as e:
        logger.error("ORCHESTRATION_FAILED", "FAILURE", run_id=run_id, message=f"Subprocess failed: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error("ORCHESTRATION_ERROR", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_layout_pipeline.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
