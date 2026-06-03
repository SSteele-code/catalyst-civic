import re
import os
import fitz # PyMuPDF
from pathlib import Path

def validate_run_id(run_id):
    """
    Strict whitelist validation for Run ID.
    Format: RUN_YYYY_MM_DD_#### (Hex)
    Prevents path traversal and injection.
    """
    pattern = r'^RUN_\d{4}_\d{2}_\d{2}_[A-F0-9]{4}$'
    if not re.match(pattern, str(run_id)):
        raise ValueError(f"CRITICAL SECURITY: Invalid Run ID format detected: {run_id}")
    return str(run_id)

def validate_filename(filename):
    """
    Whitelist validation for source filenames.
    Only allows alphanumeric, dots, dashes, and underscores.
    Mandates .pdf extension.
    """
    # Remove any potential path components just in case
    clean_name = os.path.basename(filename)
    
    # Whitelist regex
    pattern = r'^[a-zA-Z0-9._-]+\.pdf$'
    if not re.match(pattern, clean_name, re.IGNORECASE):
        raise ValueError(f"CRITICAL SECURITY: Invalid filename detected: {clean_name}")
    
    return clean_name


def validate_provenance_filename(filename):
    """
    Validation for provenance-only PDF names.
    Allows human-readable names with spaces and punctuation, but strips path segments.
    """
    clean_name = os.path.basename(str(filename or "")).strip()
    if not clean_name:
        raise ValueError("CRITICAL SECURITY: Empty provenance filename.")
    if any(ord(ch) < 32 for ch in clean_name):
        raise ValueError(f"CRITICAL SECURITY: Invalid control character in provenance filename: {clean_name!r}")
    if len(clean_name) > 255:
        raise ValueError("CRITICAL SECURITY: Provenance filename too long.")
    if not clean_name.lower().endswith(".pdf"):
        raise ValueError(f"CRITICAL SECURITY: Provenance filename must end with .pdf: {clean_name}")
    return clean_name

def validate_page_id(page_id):
    """
    Strict whitelist for Page ID.
    Format: RUN_ID_P####
    """
    pattern = r'^RUN_\d{4}_\d{2}_\d{2}_[A-F0-9]{4}_P\d{4}$'
    if not re.match(pattern, str(page_id)):
        raise ValueError(f"CRITICAL SECURITY: Invalid Page ID format: {page_id}")
    return str(page_id)

def validate_pdf(pdf_path, max_mb=500, max_pages=2000):
    """
    CRIT-006 & CRIT-008: Validate PDF size and structure.
    Prevents DoS via massive or malformed files.
    """
    pdf_path = Path(pdf_path)
    
    # 1. Size Check
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(f"RESOURCE LIMIT: PDF size ({size_mb:.1f}MB) exceeds limit of {max_mb}MB")
    
    # 2. Structure Check
    try:
        doc = fitz.open(pdf_path)
        page_count = len(doc)
        doc.close()
        
        if page_count == 0:
            raise ValueError("VALIDATION FAILURE: PDF has zero pages.")
        if page_count > max_pages:
            raise ValueError(f"RESOURCE LIMIT: PDF page count ({page_count}) exceeds limit of {max_pages}")
            
    except Exception as e:
        raise ValueError(f"VALIDATION FAILURE: Malformed PDF or parsing error: {str(e)}")
    
    return True

def safe_regex_search(pattern, text, max_chars=100000):
    """
    CRIT-004: Protect against ReDoS by limiting search text length.
    Large OCR blobs can trigger catastrophic backtracking in complex regex.
    """
    if not text:
        return None
    
    # Truncate text for safety if it's an extreme outlier
    safe_text = text[:max_chars]
    
    return re.search(pattern, safe_text, re.IGNORECASE)
