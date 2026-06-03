import sys
import os
import cv2
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler

def detect_handwriting_confidence(image_path):
    """
    Heuristic: Handwriting usually has high stroke density and high variance in contour size.
    Uses edge density and contour analysis.
    """
    image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return 0.0
        
    # 1. Edge Density
    edges = cv2.Canny(image, 100, 200)
    edge_density = cv2.countNonZero(edges) / (image.shape[0] * image.shape[1])
    
    # 2. Contour Analysis
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not cnts:
        return 0.0
        
    # Handwriting often has many small, disconnected contours compared to machine text
    avg_area = sum(cv2.contourArea(c) for c in cnts) / len(cnts)
    
    # Heuristic combination
    confidence = 0.0
    if edge_density > 0.05: confidence += 0.4
    if avg_area < 500: confidence += 0.4 # Small disconnected components
    
    return min(1.0, confidence)

def main(run_id):
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    log_dir = os.path.join(base_dir, "logs", "runs", run_id)
    manifest_dir = os.path.join(base_dir, "manifests")
    run_manifest_dir = os.path.join(manifest_dir, "runs")
    page_manifest_dir = os.path.join(manifest_dir, "pages")
    
    # Load thresholds
    config_path = os.path.join(base_dir, "config", "thresholds.json")
    with open(config_path, "r") as f:
        thresholds = json.load(f)
    HW_THRESHOLD = thresholds.get("layout", {}).get("handwriting_suspicion_threshold", 0.5)
    service = thresholds.get("service", {})
    page_worker_max_workers = max(1, int(service.get("page_worker_max_workers", 1)))
    cv2.setNumThreads(int(service.get("opencv_num_threads", 1)))
    
    logger = PipelineLogger(log_dir, "detect_handwriting")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        page_tasks = []
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            image_path = os.path.join(base_dir, page_manifest["normalized_image_path"])
            page_tasks.append((page_id, image_path))

        logger.info(
            "HANDWRITING_DETECTION_START",
            "SUCCESS",
            run_id=run_id,
            message=f"Checking {len(page_tasks)} pages with {min(page_worker_max_workers, max(1, len(page_tasks)))} workers."
        )

        max_workers = min(page_worker_max_workers, len(page_tasks)) or 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="detect_handwriting") as executor:
            futures = {
                executor.submit(detect_handwriting_confidence, image_path): page_id
                for page_id, image_path in page_tasks
            }

            for future in as_completed(futures):
                page_id = futures[future]
                confidence = future.result()
                detected = confidence >= HW_THRESHOLD
                page_handler.update(page_id, {
                    "handwriting_detected": detected,
                    "handwriting_confidence": confidence
                })
                logger.info("PAGE_CHECKED_HW", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Handwriting: {detected} ({confidence:.2f})")
            
        logger.info("HANDWRITING_DETECTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("HANDWRITING_DETECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_handwriting.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
