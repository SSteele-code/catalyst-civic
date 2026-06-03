import sys
import os
import cv2
import numpy as np
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def calculate_skew_angle(image_path):
    """Detects the skew angle of a page image."""
    image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
        
    # Binarize
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    
    # Get all non-zero points
    coords = np.column_stack(np.where(thresh > 0))
    
    # Calculate minAreaRect
    angle = cv2.minAreaRect(coords)[-1]
    
    # Handle cv2.minAreaRect angle logic
    # In OpenCV 4.5+, angle is in [0, 90]. 
    # For horizontal text, we expect near 0 or near 90.
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
        
    # Calibration for common skew range
    if abs(angle) > 45:
        if angle > 0:
            angle = angle - 90
        else:
            angle = angle + 90
            
    return float(angle)

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    service = config.get("service", {})
    page_worker_max_workers = max(1, int(service.get("page_worker_max_workers", 1)))
    cv2.setNumThreads(int(service.get("opencv_num_threads", 1)))
    
    logger = PipelineLogger(log_dir, "detect_skew")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        page_tasks = []

        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            image_path = os.path.join(base_dir, page_manifest["rendered_image_path"])
            page_tasks.append((page_id, image_path))
        
        logger.info(
            "SKEW_DETECTION_START",
            "SUCCESS",
            run_id=run_id,
            message=f"Detecting skew on {len(page_tasks)} pages with {min(page_worker_max_workers, max(1, len(page_tasks)))} workers."
        )

        max_workers = min(page_worker_max_workers, len(page_tasks)) or 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="detect_skew") as executor:
            futures = {
                executor.submit(calculate_skew_angle, image_path): page_id
                for page_id, image_path in page_tasks
            }

            for future in as_completed(futures):
                page_id = futures[future]
                angle = future.result()
                page_handler.update(page_id, {
                    "detected_skew_angle": angle
                })
                logger.info("SKEW_DETECTED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Angle: {angle:.2f} degrees")
            
        logger.info("SKEW_DETECTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("SKEW_DETECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_skew.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
