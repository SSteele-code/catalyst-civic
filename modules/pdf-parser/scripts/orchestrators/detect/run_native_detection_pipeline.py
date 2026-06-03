import sys
import os
import subprocess

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    logger = PipelineLogger(log_dir, "run_native_detection_pipeline")
    
    python_exe = sys.executable
    
    logger.info("ORCHESTRATION_START", "SUCCESS", run_id=run_id, message="Starting Native Detection & Render Pipeline")
    
    try:
        # Stage 1: Detect Native Text
        logger.info("STAGE_DETECT", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "detect", "detect_native_text_presence.py"), run_id], check=True)
        
        # Stage 2: Score Quality
        logger.info("STAGE_SCORE", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "detect", "score_native_text_quality.py"), run_id], check=True)
        
        # Stage 3: Render Pages
        logger.info("STAGE_RENDER", "START", run_id=run_id)
        subprocess.run([python_exe, os.path.join(base_dir, "scripts", "workers", "render", "render_pdf_page_to_image.py"), run_id], check=True)
        
        logger.info("ORCHESTRATION_COMPLETE", "SUCCESS", run_id=run_id, message="Detection Pipeline Finished successfully")
        
    except subprocess.CalledProcessError as e:
        logger.error("ORCHESTRATION_FAILED", "FAILURE", run_id=run_id, message=f"Subprocess failed: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error("ORCHESTRATION_ERROR", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_native_detection_pipeline.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
