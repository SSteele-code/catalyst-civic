import sys
import os
import re

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def calculate_quality_score(text):
    if not text:
        return 0.0
    
    # Heuristic: Ratio of alphanumeric + common punctuation to total length
    # This helps detect "mojibake" (corrupted encoding)
    clean_text = re.sub(r'[^a-zA-Z0-9\s.,!?;:()\'"\-]', '', text)
    score = len(clean_text) / len(text)
    return score

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    logger = PipelineLogger(log_dir, "score_native_text_quality")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        
        logger.info("QUALITY_SCORING_START", "SUCCESS", run_id=run_id)
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            if not page_manifest.get("native_text_detected"):
                continue
                
            text_path = os.path.join(base_dir, page_manifest["native_text_path"])
            with open(text_path, "r", encoding="utf-8") as f:
                text = f.read()
                
            score = calculate_quality_score(text)
            
            page_handler.update(page_id, {
                "native_text_quality_score": score
            })
            
            logger.info("PAGE_SCORED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Quality Score: {score:.2f}")
            
        logger.info("QUALITY_SCORING_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("QUALITY_SCORING_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python score_native_text_quality.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
