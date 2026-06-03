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

def rotate_image(image_path, angle):
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not read image: {image_path}")
        
    if abs(angle) < 0.1:
        return image # No significant skew
        
    (h, w) = image.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    rotated = cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return rotated

def correct_page(image_path, output_path, angle):
    corrected_image = rotate_image(image_path, angle)
    cv2.imwrite(output_path, corrected_image)

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
    
    logger = PipelineLogger(log_dir, "correct_skew")
    run_handler = ManifestHandler(run_manifest_dir)
    page_handler = ManifestHandler(page_manifest_dir)
    
    try:
        run_manifest = run_handler.load(run_id)
        page_ids = run_manifest.get("page_ids", [])
        normalized_dir = os.path.join(base_dir, "work", "runs", run_id, "pages_normalized")
        os.makedirs(normalized_dir, exist_ok=True)
        page_tasks = []
        
        for page_id in page_ids:
            page_manifest = page_handler.load(page_id)
            angle = page_manifest.get("detected_skew_angle", 0.0)
            image_path = os.path.join(base_dir, page_manifest["rendered_image_path"])
            output_filename = f"{page_id}.png"
            output_path = os.path.join(normalized_dir, output_filename)
            page_tasks.append((page_id, angle, image_path, output_filename, output_path))

        logger.info(
            "SKEW_CORRECTION_START",
            "SUCCESS",
            run_id=run_id,
            message=f"Correcting {len(page_tasks)} pages with {min(page_worker_max_workers, max(1, len(page_tasks)))} workers."
        )

        max_workers = min(page_worker_max_workers, len(page_tasks)) or 1
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="correct_skew") as executor:
            futures = {
                executor.submit(correct_page, image_path, output_path, angle): (page_id, angle, output_filename)
                for page_id, angle, image_path, output_filename, output_path in page_tasks
            }

            for future in as_completed(futures):
                page_id, angle, output_filename = futures[future]
                future.result()
                page_handler.update(page_id, {
                    "normalized_image_path": os.path.join("work", "runs", run_id, "pages_normalized", output_filename),
                    "current_state": "normalized"
                })
                logger.info("SKEW_CORRECTED", "SUCCESS", run_id=run_id, page_id=page_id, message=f"Applied rotation: {angle:.2f}")
            
        logger.info("SKEW_CORRECTION_COMPLETE", "SUCCESS", run_id=run_id)
        
    except Exception as e:
        logger.error("SKEW_CORRECTION_FAILED", "FAILURE", run_id=run_id, message=str(e))
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python correct_skew.py <run_id>")
        sys.exit(1)
    main(sys.argv[1])
