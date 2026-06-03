from __future__ import annotations

import re
from pathlib import Path

from src.common.logger import PipelineLogger
from src.common.manifest_handler import ManifestHandler
from src.state_machine.page_types import PAGE_TYPE_PRIORITY, page_type_to_function_type


LIST_ITEM_PATTERN = re.compile(r"^(?:[IVXLC]+[.)]|[A-Z][.)]|[a-z][.)]|\d+[.)])\s*")
BULLET_PATTERN = re.compile(r"^[-*•o]\s+")
ACCOUNT_CODE_PATTERN = re.compile(r"\b\d{2}-\d{4}-\d{5,6}\b")
CURRENCY_PATTERN = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
SECTION_PATTERN = re.compile(r"^(?:article|section)\s+[ivx0-9]+", re.IGNORECASE)
PROCEDURE_PAGE_PATTERN = re.compile(
    r"robert'?s rules|parliamentary procedure|amendments?\s*[\[\(]?\s*illustrated|"
    r"\bmain motion\b|\bprimary amendment\b|\bsecondary amendment\b|"
    r"\bpoint of order\b|\bquestion of privilege\b|\bprevious question\b",
    re.IGNORECASE,
)
CIVIC_SIGNATURE_PATTERN = re.compile(r"\btown clerk\b|\bclerk\b|\bmayor\b|\battest\b", re.IGNORECASE)
CONTRACT_SUPPORT_PATTERNS = {
    r"meeting minutes request",
    r"documentation package",
    r"document checklist",
}
HEADER_FORM_PATTERNS = [
    r"commonwealth of virginia",
    r"internal revenue service",
    r"department of the treasury",
    r"sales and use tax certificate of exemption",
    r"application for",
    r"selection form",
    r"customer information verification",
    r"customer signature",
    r"auto pay information",
    r"meeting minutes request",
    r"request for minutes",
    r"documentation department",
    r"document checklist",
    r"^\s*(?:irs\s+)?form\s+[a-z0-9-]+",
]
SUPPORT_FORM_PATTERNS = [
    r"meeting minutes request",
    r"request for minutes",
    r"documentation department",
    r"document checklist",
    r"customer information verification",
    r"customer signature",
    r"selection form",
    r"auto pay information",
    r"additional insured",
    r"coverage form",
    r"covered person",
    r"named member\s*or\s*entity",
]
BODY_FORM_PATTERNS = [
    r"check box",
    r"employer identification number",
    r"routing number",
    r"account number",
    r"tax exempt",
    r"customer name",
    r"physical address",
    r"mailing address",
    r"equipment location",
    r"business phone",
]
CERTIFICATE_SUPPORT_PATTERNS = [
    r"certificate of coverage",
    r"certificate holder",
    r"issued as a matter of information only",
    r"confers no rights upon the certificate holder",
    r"companies affording coverage",
    r"membership agreement afforded by the policies below",
]
ENDORSEMENT_SUPPORT_PATTERNS = [
    r"this endorsement changes the coverage document",
    r"amendatory endorsement",
    r"additional insured",
    r"this endorsement modifies coverage",
    r"loss payable provisions",
    r"covered person",
    r"named member\s*or\s*entity",
]
EXPLANATION_SHEET_PATTERNS = [
    r"explanation of content",
    r"included in this document package",
    r"brief explanation of the purpose of each form",
    r"thank you for selecting caterpillar products",
    r"if you wish to discuss any of the forms",
]
REQUEST_CORRESPONDENCE_PATTERNS = [
    r"we are requesting a copy",
    r"requesting a copy of the minutes",
    r"request for minutes",
    r"meeting minutes request",
    r"thank you for your assistance",
    r"documentation department",
    r"complete the documentation package",
]
SUPPORT_SUBTYPE_PRIORITY = [
    "certificate_support",
    "endorsement_support",
    "explanation_sheet",
    "request_correspondence",
]
APPENDIX_ROLE_PATTERNS = [
    r"\bappendix\b",
    r"\bappendices\b",
]
EXHIBIT_ROLE_PATTERNS = [
    r"\bexhibit\b",
    r"\battachment\s+[a-z0-9]+\b",
    r"\bschedule\s+[a-z0-9]+\b",
]
ATTACHMENT_ROLE_PATTERNS = [
    r"\battachments?\b",
    r"\battached\b",
    r"\benclosures?\b",
    r"\bsupporting documents?\b",
]

SEMANTIC_TYPES = {
    "agenda",
    "minutes",
    "reference_or_procedure",
    "legislative_prose",
    "government_form",
    "invoice",
    "contract_or_agreement",
    "financial_report",
}
WEAK_TYPES = {"generic_prose", "powerpoint"}
PAGE_FAMILY_BY_TYPE = {
    "agenda": "agenda",
    "minutes": "minutes",
    "reference_or_procedure": "procedure",
    "legislative_prose": "legislative",
    "government_form": "form",
    "invoice": "invoice",
    "contract_or_agreement": "contract",
    "financial_report": "financial",
    "blank_separator": "blank",
    "generic_prose": "generic",
    "powerpoint": "presentation",
    "table_or_mixed_layout": "structural",
}
FUNCTION_TYPE_BY_FAMILY = {
    "agenda": "agenda",
    "minutes": "minutes",
    "procedure": "reference",
    "legislative": "legislative",
    "contract": "contract",
    "financial": "finance",
    "form": "admin",
    "invoice": "admin",
    "blank": "separator",
    "generic": "unknown",
    "presentation": "unknown",
    "structural": "unknown",
}
NON_SUBSTANTIVE_FAMILIES = {"blank", "generic", "presentation", "structural"}
NON_SUBSTANTIVE_FUNCTION_TYPES = {"separator", "unknown"}


def alnum_count(text: str) -> int:
    return sum(1 for ch in (text or "") if ch.isalnum())


def load_text(base_dir: Path, page_manifest: dict) -> str:
    for relative_path in [page_manifest.get("ocr_text_path"), page_manifest.get("native_text_path")]:
        if not relative_path:
            continue
        full_path = base_dir / relative_path
        if full_path.exists():
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read()
    return ""


def count_table_regions(page_manifest: dict) -> int:
    return sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" in region_id)


def count_text_regions(page_manifest: dict) -> int:
    return sum(1 for region_id in page_manifest.get("region_ids", []) if "_TAB_" not in region_id)


def layout_hint_for_result(result: dict) -> str:
    for key in ("layout_type", "page_layout"):
        value = result.get(key)
        if value:
            return str(value)
    page_manifest = result.get("page_manifest", {})
    for key in ("layout_type", "page_layout"):
        value = page_manifest.get(key)
        if value:
            return str(value)
    page_type = result.get("page_type")
    if page_type == "table_or_mixed_layout":
        return "mixed"
    if page_type in {"government_form", "invoice"}:
        return "form"
    if page_type == "agenda":
        return "outline"
    if page_type == "blank_separator":
        return "blank"
    return "prose"


def has_trustworthy_ocr_witness(result: dict, settings: dict) -> bool:
    page_manifest = result.get("page_manifest", {})
    extraction_engine = page_manifest.get("extraction_engine_used")
    if extraction_engine == "native_pymupdf":
        return True
    if extraction_engine == "visual_blank_skip":
        return result.get("page_type") == "blank_separator"
    if str(page_manifest.get("ocr_witness_state") or "").lower() == "weak":
        return False

    quality_score = float(page_manifest.get("ocr_quality_score") or 0.0)
    word_count = int(page_manifest.get("ocr_word_count") or 0)
    numeric_token_count = int(page_manifest.get("ocr_numeric_token_count") or 0)
    selection_margin = float(page_manifest.get("ocr_selection_margin") or 0.0)
    layout_type = layout_hint_for_result(result)
    table_signals = result.get("signals", {}).get("table_signals", {})
    strong_numeric_table = bool(table_signals.get("is_strong_numeric_table", False))

    if quality_score < float(settings.get("context_min_ocr_quality_score", 0.28)):
        return False
    if layout_type in {"table", "mixed", "form"}:
        if word_count < int(settings.get("context_min_ocr_word_count", 4)) and numeric_token_count < int(
            settings.get("context_min_ocr_numeric_token_count", 2)
        ) and not strong_numeric_table:
            return False
    elif word_count < int(settings.get("context_min_ocr_word_count", 4)):
        return False

    if page_manifest.get("ocr_retry_used") and selection_margin < float(settings.get("context_min_ocr_selection_margin", 12.0)):
        return False
    return True


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def header_text(lines: list[str], settings: dict) -> str:
    header_line_limit = int(settings.get("government_form_header_line_window", 8))
    return "\n".join(lines[:header_line_limit])


def keyword_score(text: str, patterns: list[str]) -> tuple[int, list[str]]:
    matches = []
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            matches.append(pattern)
    return len(matches), matches


def build_metrics(text: str, settings: dict) -> dict:
    lines = nonempty_lines(text)
    short_threshold = int(settings.get("powerpoint_short_line_threshold", 96))
    prose_long_line_threshold = int(settings.get("prose_long_line_threshold", 90))

    agenda_list_lines = sum(1 for line in lines if LIST_ITEM_PATTERN.match(line))
    bullet_lines = sum(1 for line in lines if BULLET_PATTERN.match(line))
    heading_lines = sum(
        1
        for line in lines
        if len(line) <= 70
        and re.search(r"[A-Za-z]", line)
        and not re.search(r"[.!?]", line)
        and not ACCOUNT_CODE_PATTERN.search(line)
    )
    motion_lines = sum(
        1
        for line in lines
        if re.search(r"\bmotion\b|\bsecond(?:ed)?\b|\bmove(?:d)?\b|\bcouncil voted\b", line, re.IGNORECASE)
    )
    vote_lines = sum(
        1
        for line in lines
        if re.search(r"\bvotes?:\b|\broll call\b|\bunanimous\b|\bmembers present\b", line, re.IGNORECASE)
    )
    paragraph_lines = sum(1 for line in lines if len(line) >= prose_long_line_threshold)
    short_lines = sum(1 for line in lines if len(line) <= short_threshold)
    short_ratio = (short_lines / len(lines)) if lines else 0.0
    account_code_count = len(ACCOUNT_CODE_PATTERN.findall(text or ""))
    currency_count = len(CURRENCY_PATTERN.findall(text or ""))
    section_lines = sum(1 for line in lines if SECTION_PATTERN.match(line) or re.match(r"^\d+(?:\.\d+){1,3}$", line))
    numeric_table_lines = sum(
        1
        for line in lines
        if ACCOUNT_CODE_PATTERN.search(line)
        or len(CURRENCY_PATTERN.findall(line)) >= 1
        or (sum(ch.isdigit() for ch in line) >= 8 and len(line) <= 90)
        or (
            len(line) <= 24
            and sum(ch.isdigit() for ch in line) >= 2
            and any(symbol in line for symbol in "$()'.,")
        )
    )
    finance_lines = sum(
        1
        for line in lines
        if re.search(
            r"\bbudget\b|\brevenues?\b|\bexpenditures?\b|\bfund balance\b|\bdepartment\b|\bactual\b|\bmonthly financial\b|\bstaff summary\b|\bpaid checks\b",
            line,
            re.IGNORECASE,
        )
    )
    contract_lines = sum(
        1
        for line in lines
        if re.search(
            r"\bagreement\b|\bcontract\b|\btask order\b|\blessee\b|\blessor\b|\bcontract number\b|\bpurchase agreement\b|\bcertificate of coverage\b|\bendorsement\b",
            line,
            re.IGNORECASE,
        )
    )
    field_label_lines = sum(1 for line in lines if ":" in line and len(line) <= 90)
    compact_field_label_lines = sum(
        1
        for line in lines
        if ":" in line
        and len(line) <= 72
        and 0 < line.find(":") <= 32
    )
    inline_clause_heading_lines = sum(
        1
        for line in lines
        if ":" in line
        and len(line) > 72
        and 0 < line.find(":") <= 40
    )
    signature_form_lines = sum(
        1
        for line in lines
        if re.search(
            r"\bsignature\b|\btitle\b|\bphone\b|\baddress\b|\bcustomer name\b|\baccount number\b|\brouting number\b",
            line,
            re.IGNORECASE,
        )
    )

    return {
        "lines": lines,
        "line_count": len(lines),
        "agenda_list_lines": agenda_list_lines,
        "bullet_lines": bullet_lines,
        "heading_lines": heading_lines,
        "motion_lines": motion_lines,
        "vote_lines": vote_lines,
        "paragraph_lines": paragraph_lines,
        "short_ratio": round(short_ratio, 2),
        "account_code_count": account_code_count,
        "currency_count": currency_count,
        "section_lines": section_lines,
        "numeric_table_lines": numeric_table_lines,
        "finance_lines": finance_lines,
        "contract_lines": contract_lines,
        "field_label_lines": field_label_lines,
        "compact_field_label_lines": compact_field_label_lines,
        "inline_clause_heading_lines": inline_clause_heading_lines,
        "signature_form_lines": signature_form_lines,
    }


def is_dense_false_blank(alnum: int, table_regions: int, text_regions: int, route_type: str, settings: dict) -> bool:
    if route_type != "ocr_handwriting_page" or alnum > 0:
        return False
    dense_region_threshold = int(settings.get("handwriting_dense_region_threshold", 40))
    dense_text_threshold = int(settings.get("handwriting_dense_text_region_threshold", 20))
    return (table_regions + text_regions) >= dense_region_threshold and text_regions >= dense_text_threshold


def has_procedure_markers(text: str) -> bool:
    return bool(PROCEDURE_PAGE_PATTERN.search(text or ""))


def has_civic_signature_markers(text: str) -> bool:
    return bool(CIVIC_SIGNATURE_PATTERN.search(text or ""))


def has_government_form_structure(metrics: dict, settings: dict) -> bool:
    compact_threshold = int(settings.get("government_form_field_label_threshold", 4))
    signature_threshold = int(settings.get("government_form_signature_threshold", 2))
    compact_lines = metrics.get("compact_field_label_lines", 0)
    signature_lines = metrics.get("signature_form_lines", 0)
    return compact_lines >= compact_threshold or (compact_lines >= max(2, compact_threshold - 1) and signature_lines >= signature_threshold)


def has_government_form_header_anchor(metrics: dict, settings: dict) -> tuple[int, list[str]]:
    header_source = ""
    if "lines" in metrics:
        header_source = header_text(metrics["lines"], settings)
    else:
        header_source = str(metrics.get("header_text") or "")
    if not header_source:
        return 0, []
    return keyword_score(header_source.lower(), HEADER_FORM_PATTERNS)


def has_meeting_staff_summary_markers(text: str) -> bool:
    return bool(
        re.search(
            r"\bstaff summary\b|\baction item\b|\bagenda title\b|\bstaff contact(?:\(s\))?\b|\breviewed by\b|\bfunding source\b",
            text or "",
            flags=re.IGNORECASE,
        )
    )


def has_meeting_packet_cover_structure(text: str, metrics: dict) -> bool:
    if metrics.get("compact_field_label_lines", 0) < 4:
        return False

    lower_text = (text or "").lower()
    field_hits, _ = keyword_score(
        lower_text,
        [
            r"\bagenda title\b",
            r"\bstaff contact(?:\(s\))?\b",
            r"\battachments?\b",
            r"\breviewed by\b",
            r"\bagenda date\b",
        ],
    )
    section_hits, _ = keyword_score(
        lower_text,
        [
            r"\bsummary\b",
            r"\brecommendation\b",
            r"\bfunding source\b",
            r"\bfinancial impact\b",
        ],
    )
    meeting_hits, _ = keyword_score(
        lower_text,
        [
            r"\bcouncil meeting\b",
            r"\bboard meeting\b",
            r"\bcommittee meeting\b",
            r"\baction item\b",
            r"\bstaff summary\b",
        ],
    )
    return section_hits >= 1 and (field_hits >= 2 or meeting_hits >= 1)


def has_function_header(function_type: str, text: str, metrics: dict, settings: dict) -> bool:
    lower_text = (text or "").lower()
    header = str(metrics.get("header_text") or "").lower()

    if function_type == "agenda":
        agenda_hits, _ = keyword_score(header, [r"\bagenda\b", r"call meeting to order", r"scheduled public comments"])
        return agenda_hits > 0
    if function_type == "minutes":
        return "minutes" in header or metrics.get("motion_lines", 0) > 0 or metrics.get("vote_lines", 0) > 0
    if function_type == "reference":
        return has_procedure_markers(header) or metrics.get("section_lines", 0) > 0
    if function_type == "legislative":
        hits, _ = keyword_score(header, [r"\bresolution\b", r"\bordinance\b", r"\bwhereas\b", r"be it resolved"])
        return hits > 0
    if function_type == "contract":
        hits, _ = keyword_score(
            header,
            [r"\bagreement\b", r"\bcontract\b", r"\btask order\b", r"\barticle\b", r"\bsection\b"],
        )
        return hits > 0
    if function_type == "finance":
        hits, _ = keyword_score(
            header,
            [r"\bfinancial\b", r"\bbudget\b", r"\brevenues?\b", r"\bexpenditures?\b", r"\bpaid checks\b"],
        )
        return hits > 0 or metrics.get("currency_count", 0) > 0
    if function_type == "admin":
        return (
            has_government_form_header_anchor(metrics, settings)[0] > 0
            or has_meeting_packet_cover_structure(lower_text, metrics)
            or "staff summary" in header
        )
    return False


def add_candidate(candidates: list[dict], page_type: str, score: float, matches: list[str], source: str) -> None:
    if score <= 0:
        return
    candidates.append(
        {
            "page_type": page_type,
            "score": round(min(0.99, score), 2),
            "matched_patterns": matches,
            "source": source,
        }
    )


def page_type_priority_map() -> dict[str, int]:
    return {page_type: index for index, page_type in enumerate(PAGE_TYPE_PRIORITY)}


def support_subtype_priority_map() -> dict[str, int]:
    return {subtype: index for index, subtype in enumerate(SUPPORT_SUBTYPE_PRIORITY)}


def substantive_family(family: str | None) -> bool:
    return bool(family) and family not in NON_SUBSTANTIVE_FAMILIES


def substantive_function(function_type: str | None) -> bool:
    return bool(function_type) and function_type not in NON_SUBSTANTIVE_FUNCTION_TYPES


def top_semantic_candidate(result: dict) -> dict | None:
    semantic_candidates = [
        candidate
        for candidate in result.get("candidates", [])
        if candidate.get("page_type") in SEMANTIC_TYPES and candidate.get("source") in {"semantic", "context"}
    ]
    if not semantic_candidates:
        return None
    priority = page_type_priority_map()
    semantic_candidates.sort(key=lambda item: (-item["score"], priority.get(item["page_type"], 999)))
    return semantic_candidates[0]


def infer_page_layout(result: dict, settings: dict) -> tuple[str, str]:
    page_type = result["page_type"]
    metrics = result["signals"]["metrics"]
    table_signals = result["signals"]["table_signals"]
    support_form_count, _ = keyword_score((result.get("text", "") or "").lower(), SUPPORT_FORM_PATTERNS)

    if page_type == "blank_separator":
        return "blank", "label"
    if page_type == "powerpoint":
        return "slide", "label"
    if page_type in {"government_form", "invoice"}:
        return "form", "structure"
    if (
        support_form_count > 0
        and metrics["contract_lines"] < int(settings.get("contract_dominant_line_threshold", 8))
        and metrics["finance_lines"] == 0
        and metrics["vote_lines"] == 0
    ):
        return "form", "support_anchor"
    if (
        page_type == "contract_or_agreement"
        and table_signals.get("account_code_count", 0) == 0
        and table_signals.get("currency_count", 0) <= 1
    ):
        return "prose", "label_family"
    if (
        table_signals.get("is_strong_numeric_table", False)
    ):
        return "table", "geometry"
    if (
        page_type == "table_or_mixed_layout"
        and table_signals.get("table_regions", 0) >= int(settings.get("sparse_table_region_threshold", 20))
        and (
            table_signals.get("numeric_table_lines", 0) > 0
            or table_signals.get("currency_count", 0) > 0
            or table_signals.get("account_code_count", 0) > 0
        )
    ):
        return "table", "geometry"
    if page_type == "table_or_mixed_layout":
        return "mixed", "label"
    if page_type == "agenda":
        return "outline", "structure"
    return "prose", "default"


def infer_provisional_family(result: dict, settings: dict) -> tuple[str, str]:
    page_type = result["page_type"]
    direct_family = PAGE_FAMILY_BY_TYPE.get(page_type, "structural")
    if direct_family not in {"structural", "generic"}:
        return direct_family, "label"

    semantic_candidate = top_semantic_candidate(result)
    if semantic_candidate is not None:
        return PAGE_FAMILY_BY_TYPE.get(semantic_candidate["page_type"], "structural"), "semantic_candidate"

    anchor_type = result["signals"].get("context_anchor_type")
    if anchor_type:
        return PAGE_FAMILY_BY_TYPE.get(anchor_type, "structural"), "context_anchor"

    metrics = result["signals"]["metrics"]
    semantic_match_counts = result["signals"]["semantic_match_counts"]
    table_signals = result["signals"]["table_signals"]
    text = result.get("text", "").lower()
    support_form_count, _ = keyword_score(text, SUPPORT_FORM_PATTERNS)

    if (
        semantic_match_counts.get("financial_report", 0) > 0
        or metrics["finance_lines"] > 0
        or table_signals.get("currency_count", 0) > 0
        or table_signals.get("account_code_count", 0) > 0
    ):
        return "financial", "heuristic"
    if semantic_match_counts.get("contract_or_agreement", 0) > 0 or metrics["contract_lines"] > 0:
        return "contract", "heuristic"
    if (
        semantic_match_counts.get("government_form", 0) > 0
        or has_government_form_structure(metrics, settings)
        or support_form_count > 0
    ):
        return "form", "heuristic"
    if semantic_match_counts.get("minutes", 0) > 0 or metrics["motion_lines"] > 0 or metrics["vote_lines"] > 0:
        return "minutes", "heuristic"
    if semantic_match_counts.get("agenda", 0) > 0 or metrics["agenda_list_lines"] >= int(settings.get("agenda_continuation_list_threshold", 4)):
        return "agenda", "heuristic"
    if semantic_match_counts.get("reference_or_procedure", 0) > 0 or has_procedure_markers(text):
        return "procedure", "heuristic"
    if semantic_match_counts.get("legislative_prose", 0) > 0:
        return "legislative", "heuristic"
    if semantic_match_counts.get("invoice", 0) > 0:
        return "invoice", "heuristic"
    return direct_family, "fallback"


def infer_function_type(result: dict, settings: dict) -> tuple[str, str]:
    page_type = result["page_type"]
    direct_function = page_type_to_function_type(page_type)
    if direct_function != "unknown":
        return direct_function, "label"

    semantic_candidate = top_semantic_candidate(result)
    if semantic_candidate is not None:
        semantic_function = page_type_to_function_type(semantic_candidate["page_type"])
        if semantic_function != "unknown":
            return semantic_function, "semantic_candidate"

    anchor_type = result["signals"].get("context_anchor_type")
    if anchor_type:
        anchor_function = page_type_to_function_type(anchor_type)
        if anchor_function != "unknown":
            return anchor_function, "context_anchor"

    page_family = result.get("page_family")
    family_function = FUNCTION_TYPE_BY_FAMILY.get(page_family or "", "unknown")
    if family_function != "unknown":
        return family_function, "legacy_family"

    metrics = result["signals"]["metrics"]
    table_signals = result["signals"]["table_signals"]
    semantic_match_counts = result["signals"]["semantic_match_counts"]
    lower_text = (result.get("text", "") or "").lower()

    if (
        semantic_match_counts.get("financial_report", 0) > 0
        or metrics["finance_lines"] > 0
        or table_signals.get("currency_count", 0) > 0
        or table_signals.get("account_code_count", 0) > 0
    ):
        return "finance", "heuristic"
    if semantic_match_counts.get("contract_or_agreement", 0) > 0 or metrics["contract_lines"] > 0:
        return "contract", "heuristic"
    if (
        semantic_match_counts.get("government_form", 0) > 0
        or has_government_form_structure(metrics, settings)
        or has_meeting_packet_cover_structure(lower_text, metrics)
    ):
        return "admin", "heuristic"
    if semantic_match_counts.get("agenda", 0) > 0 or metrics["agenda_list_lines"] >= int(settings.get("agenda_continuation_list_threshold", 4)):
        return "agenda", "heuristic"
    if semantic_match_counts.get("minutes", 0) > 0 or metrics["motion_lines"] > 0 or metrics["vote_lines"] > 0:
        return "minutes", "heuristic"
    if semantic_match_counts.get("reference_or_procedure", 0) > 0 or has_procedure_markers(lower_text):
        return "reference", "heuristic"
    if semantic_match_counts.get("legislative_prose", 0) > 0:
        return "legislative", "heuristic"
    if page_type == "blank_separator":
        return "separator", "label"
    return "unknown", "fallback"


def infer_support_subtype(result: dict, settings: dict) -> tuple[str | None, str | None, float, list[str]]:
    page_family = result["page_family"]
    page_type = result["page_type"]
    if page_family not in {"contract", "form"} and page_type not in {"contract_or_agreement", "government_form"}:
        return None, None, 0.0, []

    text = result.get("text", "") or ""
    lower_text = text.lower()
    lines = nonempty_lines(text)
    header = header_text(lines, settings).lower()
    min_score = float(settings.get("support_subtype_min_score", 0.58))
    header_weight = float(settings.get("support_subtype_header_weight", 0.12))
    body_weight = float(settings.get("support_subtype_body_weight", 0.06))
    body_match_threshold = int(settings.get("support_subtype_body_match_threshold", 2))

    candidates: list[dict] = []

    def add_subtype_candidate(subtype: str, patterns: list[str], bonus: float = 0.0) -> None:
        header_count, header_matches = keyword_score(header, patterns)
        body_count, body_matches = keyword_score(lower_text, patterns)
        if header_count == 0 and body_count < body_match_threshold:
            return

        score = min_score + (header_count * header_weight) + (body_count * body_weight) + bonus
        reasons = list(dict.fromkeys(header_matches + body_matches))
        candidates.append(
            {
                "subtype": subtype,
                "score": round(min(0.99, score), 2),
                "reasons": reasons,
                "source": "support_semantic",
            }
        )

    certificate_bonus = 0.08 if "certificate of coverage" in lower_text else 0.0
    add_subtype_candidate("certificate_support", CERTIFICATE_SUPPORT_PATTERNS, certificate_bonus)

    endorsement_bonus = 0.0
    if "amendatory endorsement" in lower_text or "this endorsement changes the coverage document" in lower_text:
        endorsement_bonus += 0.08
    add_subtype_candidate("endorsement_support", ENDORSEMENT_SUPPORT_PATTERNS, endorsement_bonus)

    explanation_bonus = 0.0
    if "explanation of content" in header:
        explanation_bonus += 0.12
    if "document package" in lower_text:
        explanation_bonus += 0.06
    add_subtype_candidate("explanation_sheet", EXPLANATION_SHEET_PATTERNS, explanation_bonus)

    correspondence_bonus = 0.0
    if "we are requesting a copy" in lower_text:
        correspondence_bonus += 0.08
    if "thank you for your assistance" in lower_text:
        correspondence_bonus += 0.06
    add_subtype_candidate("request_correspondence", REQUEST_CORRESPONDENCE_PATTERNS, correspondence_bonus)

    if not candidates:
        return None, None, 0.0, []

    priority = support_subtype_priority_map()
    candidates.sort(key=lambda item: (-item["score"], priority.get(item["subtype"], 999)))
    best = candidates[0]
    return best["subtype"], best["source"], float(best["score"]), best["reasons"]


def nearest_substantive_family(results: list[dict], index: int, direction: int, max_distance: int) -> tuple[str | None, int | None]:
    distance = 0
    cursor = index + direction
    while 0 <= cursor < len(results) and distance < max_distance:
        distance += 1
        family = results[cursor].get("page_family")
        if substantive_family(family):
            return family, cursor
        cursor += direction
    return None, None


def nearest_substantive_function(results: list[dict], index: int, direction: int, max_distance: int) -> tuple[str | None, int | None]:
    distance = 0
    cursor = index + direction
    while 0 <= cursor < len(results) and distance < max_distance:
        distance += 1
        function_type = results[cursor].get("function_type")
        if substantive_function(function_type):
            return function_type, cursor
        cursor += direction
    return None, None


def infer_support_role(results: list[dict], index: int, settings: dict) -> tuple[str, str, float, list[str]]:
    result = results[index]
    text = result.get("text", "") or ""
    lower_text = text.lower()
    metrics = result["signals"]["metrics"]
    function_type = result.get("function_type", "unknown")
    page_support_subtype = result.get("page_support_subtype")
    if result.get("page_type") == "blank_separator" or result.get("layout_type") == "blank":
        return "standalone", "blank_default", 0.9, ["blank_separator"]

    if has_meeting_packet_cover_structure(lower_text, metrics) or has_meeting_staff_summary_markers(lower_text):
        return "cover", "meeting_packet_cover", 0.9, ["meeting_packet_cover_structure"]

    appendix_hits, appendix_matches = keyword_score(lower_text, APPENDIX_ROLE_PATTERNS)
    if appendix_hits > 0:
        return "appendix", "semantic", 0.9, appendix_matches

    exhibit_hits, exhibit_matches = keyword_score(lower_text, EXHIBIT_ROLE_PATTERNS)
    if exhibit_hits > 0 or page_support_subtype in {"certificate_support", "endorsement_support"}:
        reasons = exhibit_matches or [str(page_support_subtype)]
        return "exhibit", "semantic", 0.84, reasons

    if metrics.get("signature_form_lines", 0) >= 2 or (
        function_type in {"contract", "admin"}
        and metrics.get("signature_form_lines", 0) >= 1
        and has_civic_signature_markers(text)
    ):
        return "signature", "structure", 0.8, ["signature_lines"]

    attachment_hits, attachment_matches = keyword_score(lower_text, ATTACHMENT_ROLE_PATTERNS)
    if attachment_hits > 0 or page_support_subtype in {"explanation_sheet", "request_correspondence"}:
        reasons = attachment_matches or [str(page_support_subtype)]
        return "attachment", "semantic", 0.76, reasons

    prev_function, prev_index = nearest_substantive_function(results, index, -1, 2)
    next_function, next_index = nearest_substantive_function(results, index, 1, 2)
    has_adjacent_same_function = (
        (prev_function == function_type and prev_index is not None and index - prev_index <= 1)
        or (next_function == function_type and next_index is not None and next_index - index <= 1)
    )
    if substantive_function(function_type) and (
        result.get("best_source") == "context"
        or (
            has_adjacent_same_function
            and not has_function_header(function_type, text, metrics, settings)
        )
    ):
        if not has_trustworthy_ocr_witness(result, settings):
            return "standalone", "weak_ocr_default", 0.58, ["weak_ocr_witness"]
        reasons = []
        if result.get("best_source") == "context":
            reasons.append("context_carry")
        if has_adjacent_same_function:
            reasons.append("adjacent_same_function")
        return "continuation", "context", 0.74, reasons or ["contextual_continuation"]

    return "standalone", "default", 0.65, ["default_standalone"]


def dedupe_reasons(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reason for reason in reasons if reason))


def select_sort_lane(result: dict, settings: dict) -> tuple[str, str, list[str]]:
    page_type = result.get("page_type")
    if page_type == "blank_separator":
        return "default", "blank_default", ["blank_separator"]

    page_manifest = result.get("page_manifest", {})
    layout_type = result.get("layout_type") or result.get("page_layout") or layout_hint_for_result(result)
    table_signals = result.get("signals", {}).get("table_signals", {})

    if not has_trustworthy_ocr_witness(result, settings):
        reasons = ["weak_ocr_witness"]
        reasons.extend(f"ocr_witness:{reason}" for reason in page_manifest.get("ocr_witness_reasons", [])[:3])
        if result.get("best_source") == "context":
            reasons.append("context_override")
        if float(result.get("confidence") or 0.0) < float(settings.get("suspicion_low_confidence_threshold", 0.70)):
            reasons.append("low_confidence")
        return "weak_fallback", "ocr_witness_gate", dedupe_reasons(reasons)

    table_lane_reasons: list[str] = []
    if page_type == "table_or_mixed_layout":
        table_lane_reasons.append("page_type_table_or_mixed_layout")
    if layout_type in {"table", "mixed"}:
        table_lane_reasons.append(f"layout:{layout_type}")
    if table_signals.get("is_strong_numeric_table", False):
        table_lane_reasons.append("strong_numeric_table")

    if table_lane_reasons:
        return "table_specialist", "table_structure", dedupe_reasons(table_lane_reasons)

    return "default", "default", ["default_route"]


def determine_review_state(result: dict, settings: dict) -> tuple[str, str, list[str]]:
    suspicion_score = float(result.get("suspicion_score") or 0.0)
    sort_lane = str(result.get("sort_lane") or "default")
    page_type = str(result.get("page_type") or "generic_prose")
    layout_type = str(result.get("layout_type") or result.get("page_layout") or "prose")
    page_manifest = result.get("page_manifest", {})
    witness_state = str(page_manifest.get("ocr_witness_state") or "").lower()
    route_type = str(page_manifest.get("route_type") or "")
    extraction_engine = str(page_manifest.get("extraction_engine_used") or "")
    provisional_threshold = float(settings.get("review_state_provisional_threshold", 0.18))
    review_required_threshold = float(settings.get("review_state_review_required_threshold", 0.35))
    quarantine_threshold = float(settings.get("review_state_quarantine_threshold", 0.62))

    reasons: list[str] = []
    if witness_state == "weak":
        reasons.append("weak_ocr_witness")
    if sort_lane == "weak_fallback":
        reasons.append("weak_fallback_lane")
    if sort_lane == "table_specialist":
        reasons.append("table_specialist_lane")
    if result.get("best_source") == "context":
        reasons.append("context_override")
    if suspicion_score >= provisional_threshold:
        reasons.append("suspicion_above_provisional")
    if suspicion_score >= review_required_threshold:
        reasons.append("suspicion_above_review_required")
    if layout_type in {"table", "mixed", "form"} and page_type != "blank_separator":
        reasons.append(f"structured_layout:{layout_type}")

    if (
        page_type == "blank_separator"
        and extraction_engine in {"visual_blank_skip", "native_pymupdf"}
        and suspicion_score < provisional_threshold
    ):
        return "auto_pass", "review_state_matrix", ["blank_or_native_clean"]

    if suspicion_score >= quarantine_threshold and (witness_state == "weak" or sort_lane == "weak_fallback"):
        reasons.append("quarantine_gate")
        return "quarantined", "review_state_matrix", dedupe_reasons(reasons)

    if (
        route_type == "ocr_handwriting_page"
        and suspicion_score < review_required_threshold
        and witness_state != "weak"
    ):
        return "provisional_auto_pass", "review_state_matrix", dedupe_reasons(reasons or ["stable_handwriting_provisional"])

    if (
        witness_state == "weak"
        or sort_lane == "weak_fallback"
        or suspicion_score >= review_required_threshold
        or route_type == "manual_review_required"
    ):
        return "review_required", "review_state_matrix", dedupe_reasons(reasons or ["review_gate"])

    if (
        sort_lane == "table_specialist"
        or suspicion_score >= provisional_threshold
        or result.get("best_source") == "context"
        or layout_type in {"table", "mixed", "form"}
    ):
        return "provisional", "review_state_matrix", dedupe_reasons(reasons or ["structured_or_contextual"])

    return "auto_pass", "review_state_matrix", ["clean_signal_auto_pass"]


def enrich_classification_metadata(results: list[dict], settings: dict) -> list[dict]:
    family_neighbor_distance = int(settings.get("family_inference_neighbor_distance", 2))
    for result in results:
        page_family, family_source = infer_provisional_family(result, settings)
        page_layout, layout_source = infer_page_layout(result, settings)
        result["page_family"] = page_family
        result["page_family_source"] = family_source
        result["page_layout"] = page_layout
        result["page_layout_source"] = layout_source
        result["layout_type"] = page_layout
        result["layout_type_source"] = layout_source

    for index, result in enumerate(results):
        if result["page_family"] not in {"structural", "generic"}:
            continue
        if not has_trustworthy_ocr_witness(result, settings):
            continue
        prev_family, _ = nearest_substantive_family(results, index, -1, family_neighbor_distance)
        next_family, _ = nearest_substantive_family(results, index, 1, family_neighbor_distance)
        if prev_family and prev_family == next_family:
            result["page_family"] = prev_family
            result["page_family_source"] = "neighbor_consensus"
            continue
        if prev_family and result["page_layout"] == "table":
            result["page_family"] = prev_family
            result["page_family_source"] = "neighbor_hint"

    for result in results:
        function_type, function_source = infer_function_type(result, settings)
        result["function_type"] = function_type
        result["function_type_source"] = function_source

    for index, result in enumerate(results):
        if result["function_type"] != "unknown":
            continue
        if not has_trustworthy_ocr_witness(result, settings):
            continue
        prev_function, _ = nearest_substantive_function(results, index, -1, family_neighbor_distance)
        next_function, _ = nearest_substantive_function(results, index, 1, family_neighbor_distance)
        if prev_function and prev_function == next_function:
            result["function_type"] = prev_function
            result["function_type_source"] = "neighbor_consensus"
            continue
        if prev_function and result["layout_type"] == "table":
            result["function_type"] = prev_function
            result["function_type_source"] = "neighbor_hint"

    for result in results:
        page_support_subtype, subtype_source, subtype_confidence, subtype_reasons = infer_support_subtype(result, settings)
        result["page_support_subtype"] = page_support_subtype
        result["page_support_subtype_source"] = subtype_source
        result["page_support_subtype_confidence"] = subtype_confidence
        result["page_support_subtype_reasons"] = subtype_reasons

    for index, result in enumerate(results):
        support_role, support_role_source, support_role_confidence, support_role_reasons = infer_support_role(results, index, settings)
        result["support_role"] = support_role
        result["support_role_source"] = support_role_source
        result["support_role_confidence"] = support_role_confidence
        result["support_role_reasons"] = support_role_reasons

    close_margin = float(settings.get("suspicion_close_score_margin", 0.08))
    low_conf_threshold = float(settings.get("suspicion_low_confidence_threshold", 0.70))
    very_low_conf_threshold = float(settings.get("suspicion_very_low_confidence_threshold", 0.55))
    dense_blank_regions = int(settings.get("suspicion_dense_blank_region_threshold", 20))

    for index, result in enumerate(results):
        suspicion_score = 0.0
        suspicion_reasons: list[str] = []
        candidates = sorted(
            result.get("candidates", []),
            key=lambda item: (-item["score"], page_type_priority_map().get(item["page_type"], 999)),
        )

        distinct_runner_up = next(
            (candidate for candidate in candidates[1:] if candidate["page_type"] != candidates[0]["page_type"]),
            None,
        )
        if distinct_runner_up is not None:
            margin = round(candidates[0]["score"] - distinct_runner_up["score"], 2)
            if margin <= close_margin:
                suspicion_score += 0.22
                suspicion_reasons.append(f"close_runner_up:{distinct_runner_up['page_type']}")

        if result.get("best_source") == "context":
            suspicion_score += 0.18
            suspicion_reasons.append("context_override")

        if result["confidence"] < low_conf_threshold:
            suspicion_score += 0.18
            suspicion_reasons.append("low_confidence")
        if result["confidence"] < very_low_conf_threshold:
            suspicion_score += 0.12
            suspicion_reasons.append("very_low_confidence")

        if not has_trustworthy_ocr_witness(result, settings):
            suspicion_score += 0.26
            suspicion_reasons.append("weak_ocr_witness")
            witness_reasons = result["page_manifest"].get("ocr_witness_reasons", [])
            if witness_reasons:
                suspicion_reasons.append(f"weak_ocr_detail:{witness_reasons[0]}")

        if (
            result["page_type"] == "blank_separator"
            and (result["signals"]["table_region_count"] + result["signals"]["text_region_count"]) >= dense_blank_regions
        ):
            suspicion_score += 0.45
            suspicion_reasons.append("dense_blank_separator")

        semantic_match_counts = result["signals"]["semantic_match_counts"]
        if semantic_match_counts.get("government_form", 0) > 0 and semantic_match_counts.get("contract_or_agreement", 0) > 0:
            suspicion_score += 0.18
            suspicion_reasons.append("form_contract_conflict")

        prev_family, prev_index = nearest_substantive_family(results, index, -1, 2)
        next_family, next_index = nearest_substantive_family(results, index, 1, 2)
        if (
            prev_family
            and next_family
            and prev_family == next_family
            and substantive_family(result["page_family"])
            and result["page_family"] != prev_family
        ):
            suspicion_score += 0.22
            suspicion_reasons.append(f"neighbor_family_conflict:{prev_family}")

        prev_function, _ = nearest_substantive_function(results, index, -1, 2)
        next_function, _ = nearest_substantive_function(results, index, 1, 2)
        if (
            prev_function
            and next_function
            and prev_function == next_function
            and substantive_function(result["function_type"])
            and result["function_type"] != prev_function
        ):
            suspicion_score += 0.18
            suspicion_reasons.append(f"neighbor_function_conflict:{prev_function}")

        if prev_index is not None and next_index is not None:
            prev_type = results[prev_index]["page_type"]
            next_type = results[next_index]["page_type"]
            if prev_type == next_type and result["page_type"] != prev_type and result.get("best_source") != "semantic":
                suspicion_score += 0.10
                suspicion_reasons.append(f"neighbor_label_conflict:{prev_type}")

        if (
            result["page_manifest"].get("route_type") == "ocr_handwriting_page"
            and "rescue" in str(result["page_manifest"].get("ocr_variant_used", "")).lower()
            and result["page_type"] != "blank_separator"
        ):
            suspicion_score += 0.08
            suspicion_reasons.append("handwriting_route_rescue")

        sort_lane, sort_lane_source, sort_lane_reasons = select_sort_lane(result, settings)
        result["sort_lane"] = sort_lane
        result["sort_lane_source"] = sort_lane_source
        result["sort_lane_reasons"] = sort_lane_reasons
        result["suspicion_score"] = round(min(0.99, suspicion_score), 2)
        result["suspicion_reasons"] = suspicion_reasons
        review_state, review_state_source, review_state_reasons = determine_review_state(result, settings)
        result["review_state"] = review_state
        result["review_state_source"] = review_state_source
        result["review_state_reasons"] = review_state_reasons
        result["signals"]["classification_metadata"] = {
            "function_type": result["function_type"],
            "function_type_source": result["function_type_source"],
            "layout_type": result["layout_type"],
            "layout_type_source": result["layout_type_source"],
            "support_role": result["support_role"],
            "support_role_source": result["support_role_source"],
            "support_role_confidence": result["support_role_confidence"],
            "support_role_reasons": result["support_role_reasons"],
            "page_family": result["page_family"],
            "page_family_source": result["page_family_source"],
            "page_layout": result["page_layout"],
            "page_layout_source": result["page_layout_source"],
            "page_support_subtype": result["page_support_subtype"],
            "page_support_subtype_source": result["page_support_subtype_source"],
            "page_support_subtype_confidence": result["page_support_subtype_confidence"],
            "page_support_subtype_reasons": result["page_support_subtype_reasons"],
            "sort_lane": result["sort_lane"],
            "sort_lane_source": result["sort_lane_source"],
            "sort_lane_reasons": result["sort_lane_reasons"],
            "suspicion_score": result["suspicion_score"],
            "suspicion_reasons": suspicion_reasons,
            "review_state": result["review_state"],
            "review_state_source": result["review_state_source"],
            "review_state_reasons": result["review_state_reasons"],
        }

    return results


def build_table_signals(metrics: dict, table_regions: int, route_type: str, settings: dict) -> dict:
    numeric_table_lines = int(metrics["numeric_table_lines"])
    account_code_count = int(metrics["account_code_count"])
    currency_count = int(metrics["currency_count"])
    short_ratio = float(metrics["short_ratio"])
    paragraph_lines = int(metrics["paragraph_lines"])

    is_strong_numeric_table = (
        account_code_count >= int(settings.get("table_account_code_threshold", 4))
        or numeric_table_lines >= int(settings.get("table_numeric_line_threshold", 6))
        or (
            currency_count >= int(settings.get("table_currency_threshold", 4))
            and short_ratio >= float(settings.get("table_short_line_ratio_threshold", 0.55))
        )
    )
    is_layout_supported = (
        table_regions >= int(settings.get("table_region_threshold", 2))
        and numeric_table_lines >= 2
        and paragraph_lines <= int(settings.get("table_max_paragraph_lines", 2))
    )
    return {
        "numeric_table_lines": numeric_table_lines,
        "account_code_count": account_code_count,
        "currency_count": currency_count,
        "paragraph_lines": paragraph_lines,
        "short_line_ratio": short_ratio,
        "table_regions": table_regions,
        "route_type": route_type,
        "is_strong_numeric_table": is_strong_numeric_table,
        "is_layout_supported": is_layout_supported,
    }


def powerpoint_signals(metrics: dict, settings: dict) -> tuple[bool, dict]:
    is_powerpoint_like = (
        metrics["line_count"] > 0
        and metrics["line_count"] <= 18
        and metrics["bullet_lines"] >= int(settings.get("powerpoint_bullet_threshold", 4))
        and metrics["short_ratio"] >= 0.85
        and metrics["paragraph_lines"] <= 1
        and metrics["account_code_count"] == 0
        and metrics["currency_count"] <= int(settings.get("powerpoint_max_currency_hits", 2))
        and metrics["numeric_table_lines"] <= int(settings.get("powerpoint_numeric_line_threshold", 2))
        and metrics["motion_lines"] == 0
    )
    return is_powerpoint_like, {
        "bullet_line_count": metrics["bullet_lines"],
        "short_line_ratio": metrics["short_ratio"],
        "line_count": metrics["line_count"],
    }


def determine_page_type(text: str, page_manifest: dict, settings: dict) -> tuple[str, float, str, dict, list[dict]]:
    normalized = normalize_whitespace(text)
    lower_text = normalized.lower()
    alnum = alnum_count(text)
    table_regions = count_table_regions(page_manifest)
    text_regions = count_text_regions(page_manifest)
    route_type = page_manifest.get("route_type", "unknown")
    metrics = build_metrics(text, settings)
    signal_metrics = {key: value for key, value in metrics.items() if key != "lines"}
    signal_metrics["header_text"] = header_text(metrics["lines"], settings)
    dense_false_blank = is_dense_false_blank(alnum, table_regions, text_regions, route_type, settings)
    skew_angle = abs(float(page_manifest.get("detected_skew_angle") or 0.0))

    if (not normalized or alnum == 0) and not dense_false_blank:
        return (
            "blank_separator",
            1.0,
            "ZERO_TEXT_BLANK_SEPARATOR",
            {
                "alnum_count": alnum,
                "table_region_count": table_regions,
                "text_region_count": text_regions,
                "route_type": route_type,
                "metrics": signal_metrics,
                "semantic_match_counts": {},
                "table_signals": build_table_signals(metrics, table_regions, route_type, settings),
            },
            [{"page_type": "blank_separator", "score": 1.0, "matched_patterns": [], "source": "semantic"}],
        )

    if not dense_false_blank and alnum <= int(settings.get("blank_separator_alnum_threshold", 24)) and text_regions <= int(
        settings.get("blank_separator_region_threshold", 12)
    ):
        return (
            "blank_separator",
            1.0,
            "LOW_SIGNAL_BLANK_SEPARATOR",
            {
                "alnum_count": alnum,
                "table_region_count": table_regions,
                "text_region_count": text_regions,
                "route_type": route_type,
                "metrics": signal_metrics,
                "semantic_match_counts": {},
                "table_signals": build_table_signals(metrics, table_regions, route_type, settings),
            },
            [{"page_type": "blank_separator", "score": 1.0, "matched_patterns": [], "source": "semantic"}],
        )

    candidates: list[dict] = []
    semantic_match_counts: dict[str, int] = {}
    procedure_markers = has_procedure_markers(lower_text)
    government_form_header_count, government_form_header_matches = has_government_form_header_anchor(metrics, settings)
    government_form_support_count, government_form_support_matches = keyword_score(lower_text, SUPPORT_FORM_PATTERNS)
    government_form_body_count, government_form_body_matches = keyword_score(lower_text, BODY_FORM_PATTERNS)
    government_form_has_structure = has_government_form_structure(metrics, settings)
    government_form_has_anchor = government_form_header_count > 0 or government_form_support_count > 0

    pattern_map = {
        "agenda": [
            r"\bagenda\b",
            r"call meeting to order",
            r"scheduled public comments",
            r"unscheduled public comments",
            r"town council meeting",
            r"adjourn meeting",
            r"next regular meeting",
        ],
        "minutes": [
            r"\bminutes\b",
            r"regular monthly meeting",
            r"upon a motion by",
            r"seconded by",
            r"roll call vote",
            r"council voted",
            r"members present",
        ],
        "reference_or_procedure": [
            r"robert'?s rules",
            r"parliamentary procedure",
            r"question of privilege",
            r"point of order",
            r"previous question",
            r"parliamentary procedure at a glance",
            r"introduce a motion",
            r"amendments? illustrated",
            r"amendments?\s*[\[\(]?\s*illustrated",
            r"\bmain motion\b",
            r"\bprimary amendment\b",
            r"\bsecondary amendment\b",
            r"the chair repeats the motion",
        ],
        "legislative_prose": [
            r"\bresolution\b",
            r"\bordinance\b",
            r"\bwhereas\b",
            r"be it resolved",
            r"now[, ]+therefore",
            r"town charter",
        ],
        "government_form": [],
        "invoice": [
            r"\binvoice\b",
            r"amount due",
            r"bill to",
            r"invoice number",
            r"remit",
        ],
        "contract_or_agreement": [
            r"\bagreement\b",
            r"\bcontract\b",
            r"\btask order\b",
            r"\bcontract number\b",
            r"general terms and conditions",
            r"insurance requirements",
            r"\bindemnification\b",
            r"\bconfidentiality\b",
            r"\bnon-discrimination\b",
            r"purchase agreement",
            r"payment schedule",
            r"certificate of coverage",
            r"additional terms and conditions",
            r"duties and obligations",
            r"commercial general liability",
            r"workers'? compensation insurance",
            r"warrants and represents",
            r"lessor",
            r"lessee",
            r"opinion of counsel",
            r"endorsement changes the coverage document",
            r"general liability",
        ],
        "financial_report": [
            r"monthly financial report",
            r"financial policies",
            r"financial investment policies",
            r"draft financial and investment policies",
            r"\bbudget\b",
            r"budget amendment",
            r"budget ordinance amendments?",
            r"\bappropriation\b",
            r"statement of revenues",
            r"statement of expenditures",
            r"balance sheet",
            r"\bgeneral fund\b",
            r"\bfund balance\b",
            r"\bgrants?\b",
            r"lease-purchase financing",
            r"capital assets?",
            r"depreciation",
            r"revenue bonds?",
            r"inter-?departmental transfers?",
            r"\bpaid checks report\b",
            r"\bstaff summary\b",
            r"\btotal revenues\b",
            r"\btotal expenditures\b",
        ],
    }

    for page_type, patterns in pattern_map.items():
        score_count, matches = keyword_score(lower_text, patterns)
        if page_type == "government_form":
            score_count = government_form_header_count + government_form_support_count
            matches = list(government_form_header_matches) + list(government_form_support_matches)
            if government_form_has_anchor or government_form_has_structure:
                score_count += government_form_body_count
                matches.extend(government_form_body_matches)
        semantic_match_counts[page_type] = score_count

        bonus = 0.0
        if page_type == "agenda":
            strong_agenda_markers = {
                "call meeting to order",
                "scheduled public comments",
                "unscheduled public comments",
                "adjourn meeting",
                "next regular meeting",
            }
            if metrics["agenda_list_lines"] == 0 and not any(marker in matches for marker in strong_agenda_markers) and score_count <= 2:
                continue
            if has_meeting_packet_cover_structure(lower_text, metrics):
                continue
            if has_meeting_staff_summary_markers(lower_text):
                bonus -= 0.14
            if metrics["agenda_list_lines"] >= int(settings.get("agenda_continuation_list_threshold", 4)):
                bonus += 0.18
            bonus += min(0.16, metrics["agenda_list_lines"] * 0.03)
            if "adjourn meeting" in matches and "next regular meeting" in matches:
                bonus += 0.08
        elif page_type == "minutes":
            if procedure_markers and score_count == 0:
                continue
            bonus += min(0.28, (metrics["motion_lines"] * 0.08) + (metrics["vote_lines"] * 0.06))
        elif page_type == "reference_or_procedure":
            bonus += min(0.20, (metrics["section_lines"] * 0.04) + (metrics["paragraph_lines"] * 0.02))
            if procedure_markers:
                bonus += 0.12
        elif page_type == "legislative_prose":
            bonus += min(0.20, metrics["vote_lines"] * 0.04)
        elif page_type == "government_form":
            weak_header_only = government_form_header_count > 0 and government_form_support_count == 0 and government_form_body_count == 0
            if has_meeting_packet_cover_structure(lower_text, metrics) or has_meeting_staff_summary_markers(lower_text):
                continue
            if score_count == 0 and not government_form_has_structure:
                continue
            if weak_header_only and not government_form_has_structure:
                continue
            if weak_header_only and metrics["contract_lines"] > 0 and metrics.get("compact_field_label_lines", 0) < 2:
                continue
            bonus += min(0.18, metrics["heading_lines"] * 0.02)
            bonus += min(
                0.22,
                (metrics.get("compact_field_label_lines", 0) * 0.03) + (metrics["signature_form_lines"] * 0.05),
            )
        elif page_type == "contract_or_agreement":
            if score_count and set(matches).issubset(CONTRACT_SUPPORT_PATTERNS) and metrics["contract_lines"] == 0 and metrics["section_lines"] == 0:
                continue
            bonus += min(0.24, (metrics["section_lines"] * 0.04) + (metrics["contract_lines"] * 0.03))
            bonus += min(0.18, metrics.get("inline_clause_heading_lines", 0) * 0.05)
            if metrics["section_lines"] >= 4 and metrics["contract_lines"] >= 1 and metrics["finance_lines"] == 0:
                bonus += 0.12
            if metrics.get("compact_field_label_lines", 0) == 0 and metrics.get("inline_clause_heading_lines", 0) >= 2:
                bonus += 0.08
        elif page_type == "financial_report":
            bonus += min(0.20, (metrics["finance_lines"] * 0.03) + (metrics["currency_count"] * 0.01))
            if has_meeting_packet_cover_structure(lower_text, metrics) and (metrics["finance_lines"] > 0 or score_count > 0):
                bonus += 0.12
            elif has_meeting_staff_summary_markers(lower_text) and (metrics["finance_lines"] > 0 or score_count > 0):
                bonus += 0.18
            if score_count >= 2 and metrics["numeric_table_lines"] <= 1 and metrics["currency_count"] == 0 and metrics["account_code_count"] == 0:
                bonus += 0.08
            if (
                metrics["finance_lines"] >= int(settings.get("finance_prose_line_threshold", 3))
                and metrics["numeric_table_lines"] <= int(settings.get("finance_prose_max_numeric_lines", 3))
                and metrics["currency_count"] <= int(settings.get("finance_prose_max_currency_hits", 2))
                and metrics["account_code_count"] == 0
            ):
                bonus += 0.10
            if (
                metrics["paragraph_lines"] == 0
                and metrics["short_ratio"] >= 0.95
                and metrics["line_count"] <= int(settings.get("financial_summary_table_line_threshold", 18))
                and metrics["currency_count"] >= int(settings.get("financial_summary_table_currency_threshold", 6))
            ):
                bonus -= 0.14

        if page_type in {"agenda", "government_form", "contract_or_agreement", "reference_or_procedure", "legislative_prose", "invoice"} and score_count == 0:
            continue
        if page_type == "legislative_prose" and not any(
            anchor in matches
            for anchor in [r"\bresolution\b", r"\bwhereas\b", r"be it resolved", r"now[, ]+therefore"]
        ):
            continue
        if page_type == "minutes" and score_count == 0 and (metrics["motion_lines"] + metrics["vote_lines"]) == 0:
            continue
        if page_type == "financial_report" and score_count == 0 and metrics["finance_lines"] == 0 and metrics["currency_count"] == 0:
            continue
        if not score_count and bonus < 0.16:
            continue

        base_score = 0.30 + (score_count * 0.12) + bonus
        add_candidate(candidates, page_type, base_score, matches, "semantic")

    table_signals = build_table_signals(metrics, table_regions, route_type, settings)
    table_matches = []
    if table_signals["account_code_count"]:
        table_matches.append(f"account_codes:{table_signals['account_code_count']}")
    if table_signals["currency_count"]:
        table_matches.append(f"currency_hits:{table_signals['currency_count']}")
    if table_signals["numeric_table_lines"]:
        table_matches.append(f"numeric_lines:{table_signals['numeric_table_lines']}")
    if table_signals["table_regions"]:
        table_matches.append(f"table_regions:{table_signals['table_regions']}")

    if table_signals["is_strong_numeric_table"] or table_signals["is_layout_supported"]:
        table_score = 0.50
        table_score += min(0.18, table_signals["numeric_table_lines"] * 0.02)
        table_score += min(0.16, table_signals["account_code_count"] * 0.03)
        table_score += min(0.12, table_signals["currency_count"] * 0.01)
        if table_signals["is_layout_supported"]:
            table_score += 0.06
        if table_signals["numeric_table_lines"] >= int(settings.get("table_dominant_numeric_line_threshold", 20)):
            table_score += 0.06
        if table_signals["account_code_count"] >= int(settings.get("table_dominant_account_code_threshold", 8)):
            table_score += 0.06
        if table_signals["currency_count"] >= int(settings.get("table_dominant_currency_threshold", 20)):
            table_score += 0.06
        if table_signals["paragraph_lines"] > int(settings.get("table_max_paragraph_lines", 2)):
            table_score -= 0.12
        add_candidate(candidates, "table_or_mixed_layout", table_score, table_matches, "layout")

    if (
        table_regions >= int(settings.get("sparse_table_region_threshold", 20))
        and metrics["line_count"] <= int(settings.get("sparse_table_max_lines", 18))
        and metrics["short_ratio"] >= 0.95
        and metrics["paragraph_lines"] == 0
    ):
        add_candidate(
            candidates,
            "table_or_mixed_layout",
            0.72,
            [f"sparse_table_regions:{table_regions}", f"line_count:{metrics['line_count']}"],
            "layout",
        )

    if (
        skew_angle >= float(settings.get("rotated_table_skew_threshold", 12.0))
        and table_regions >= int(settings.get("rotated_table_region_threshold", 4))
        and metrics["short_ratio"] >= 0.95
        and metrics["paragraph_lines"] == 0
    ):
        add_candidate(
            candidates,
            "table_or_mixed_layout",
            0.7,
            [f"skew_angle:{round(skew_angle, 2)}", f"table_regions:{table_regions}"],
            "layout",
        )

    report_summary_matches = []
    for pattern in [
        r"monthly traffic summary",
        r"council report",
        r"calls for service",
        r"reckless driving",
        r"operators license",
    ]:
        if re.search(pattern, lower_text, flags=re.IGNORECASE | re.MULTILINE):
            report_summary_matches.append(pattern)
    if report_summary_matches:
        add_candidate(candidates, "table_or_mixed_layout", 0.60, report_summary_matches, "semantic")

    is_powerpoint, ppt_signals = powerpoint_signals(metrics, settings)
    if is_powerpoint:
        add_candidate(
            candidates,
            "powerpoint",
            0.62 + min(0.12, metrics["bullet_lines"] * 0.03),
            [f"bullet_lines:{metrics['bullet_lines']}"],
            "layout",
        )

    contract_candidate = next((candidate for candidate in candidates if candidate["page_type"] == "contract_or_agreement"), None)
    form_candidate = next((candidate for candidate in candidates if candidate["page_type"] == "government_form"), None)
    contract_margin = float(settings.get("contract_over_form_margin", 0.02))
    if (
        contract_candidate
        and form_candidate
        and contract_candidate["score"] >= (form_candidate["score"] - contract_margin)
        and metrics["contract_lines"] >= int(settings.get("contract_dominant_line_threshold", 8))
        and metrics["field_label_lines"] < int(settings.get("government_form_field_label_threshold", 4))
        and not government_form_has_anchor
    ):
        contract_candidate["score"] = round(min(0.99, max(contract_candidate["score"], form_candidate["score"] + 0.01)), 2)
        contract_candidate["matched_patterns"] = contract_candidate["matched_patterns"] + ["contract_over_form_tiebreak"]
    if (
        contract_candidate
        and form_candidate
        and government_form_has_anchor
        and semantic_match_counts.get("government_form", 0) >= 2
        and metrics["contract_lines"] < int(settings.get("contract_dominant_line_threshold", 8))
    ):
        form_candidate["score"] = round(min(0.99, max(form_candidate["score"], contract_candidate["score"] + 0.01)), 2)
        form_candidate["matched_patterns"] = form_candidate["matched_patterns"] + ["form_support_tiebreak"]

    if not candidates:
        generic_score = 0.42 if metrics["paragraph_lines"] else 0.28
        add_candidate(candidates, "generic_prose", generic_score, [], "fallback")

    priority = {page_type: index for index, page_type in enumerate(PAGE_TYPE_PRIORITY)}
    candidates.sort(key=lambda item: (-item["score"], priority.get(item["page_type"], 999)))
    best = candidates[0]

    reason_prefix = {
        "semantic": "SEMANTIC_MATCH",
        "layout": "LAYOUT_FALLBACK",
        "fallback": "GENERIC_FALLBACK",
        "context": "CONTEXT_CARRY",
    }[best["source"]]
    reason = f"{reason_prefix}_{best['page_type'].upper()}"

    signals = {
        "alnum_count": alnum,
        "table_region_count": table_regions,
        "text_region_count": text_regions,
        "route_type": route_type,
        "handwriting_detected": page_manifest.get("handwriting_detected", False),
        "native_text_detected": page_manifest.get("native_text_detected", False),
        "powerpoint_signals": ppt_signals,
        "table_signals": table_signals,
        "metrics": signal_metrics,
        "semantic_match_counts": semantic_match_counts,
    }
    return best["page_type"], float(best["score"]), reason, signals, candidates


def is_anchor(result: dict, settings: dict) -> bool:
    return (
        result["page_type"] in SEMANTIC_TYPES
        and result["best_source"] == "semantic"
        and result["confidence"] >= float(settings.get("context_anchor_score_threshold", 0.72))
    )


def is_weak_for_context(result: dict) -> bool:
    if result.get("layout_type") == "blank":
        return False
    if result["page_type"] in WEAK_TYPES:
        return True
    if result["page_type"] == "table_or_mixed_layout":
        return not result["signals"].get("table_signals", {}).get("is_strong_numeric_table", False)
    return False


def meets_context_evidence_floor(anchor_type: str, result: dict, settings: dict) -> bool:
    metrics = result["signals"]["metrics"]
    semantic_match_counts = result["signals"]["semantic_match_counts"]
    table_signals = result["signals"]["table_signals"]
    lower_text = (result.get("text", "") or "").lower()

    if result["page_type"] == "blank_separator":
        return False
    if anchor_type == "agenda":
        strong_marker_hits, _ = keyword_score(
            lower_text,
            [
                "call meeting to order",
                "scheduled public comments",
                "unscheduled public comments",
                "adjourn meeting",
                "next regular meeting",
            ],
        )
        return (
            semantic_match_counts.get("agenda", 0) > 0
            and (
                metrics["agenda_list_lines"] >= int(settings.get("context_agenda_min_list_lines", 3))
                or strong_marker_hits >= int(settings.get("context_agenda_min_marker_hits", 1))
            )
        )
    if anchor_type == "minutes":
        return (
            semantic_match_counts.get("minutes", 0) > 0
            or (metrics["motion_lines"] + metrics["vote_lines"])
            >= int(settings.get("context_minutes_min_motion_vote_lines", 1))
        )
    if anchor_type == "reference_or_procedure":
        return (
            semantic_match_counts.get("reference_or_procedure", 0) > 0
            or has_procedure_markers(lower_text)
            or metrics["section_lines"] >= int(settings.get("context_reference_min_section_lines", 1))
        )
    if anchor_type == "legislative_prose":
        legislative_hits, _ = keyword_score(lower_text, [r"\bresolution\b", r"\bwhereas\b", r"be it resolved", r"now[, ]+therefore"])
        return (
            semantic_match_counts.get("legislative_prose", 0) > 0
            or metrics["vote_lines"] > 0
            or legislative_hits >= int(settings.get("context_legislative_min_marker_hits", 1))
        )
    if anchor_type == "contract_or_agreement":
        return (
            semantic_match_counts.get("contract_or_agreement", 0) >= int(settings.get("context_contract_min_semantic_hits", 2))
            or metrics["contract_lines"] >= int(settings.get("context_contract_min_contract_lines", 1))
            or metrics["section_lines"] >= int(settings.get("context_contract_min_section_lines", 1))
            or metrics.get("inline_clause_heading_lines", 0) >= int(settings.get("context_contract_min_clause_headings", 1))
        )
    if anchor_type == "financial_report":
        if has_meeting_packet_cover_structure(lower_text, metrics) and (
            semantic_match_counts.get("financial_report", 0) > 0
            or metrics["finance_lines"] > 0
        ):
            return True
        return (
            semantic_match_counts.get("financial_report", 0) > 0
            or metrics["finance_lines"] >= int(settings.get("context_finance_min_lexical_lines", 1))
            or table_signals.get("currency_count", 0) >= int(settings.get("context_finance_min_currency_hits", 1))
            or table_signals.get("account_code_count", 0) >= int(settings.get("context_finance_min_account_codes", 1))
            or table_signals.get("numeric_table_lines", 0) >= int(settings.get("context_finance_min_numeric_lines", 4))
            or table_signals.get("is_strong_numeric_table", False)
        )
    if anchor_type == "government_form":
        header_count, _ = has_government_form_header_anchor(metrics, settings)
        support_count, _ = keyword_score(lower_text, SUPPORT_FORM_PATTERNS)
        body_count, _ = keyword_score(lower_text, BODY_FORM_PATTERNS)
        if has_meeting_packet_cover_structure(lower_text, metrics) or has_meeting_staff_summary_markers(lower_text):
            return False
        return (
            has_government_form_structure(metrics, settings)
            or support_count > 0
            or body_count >= int(settings.get("context_form_min_body_hits", 1))
            or (header_count >= 2 and metrics["signature_form_lines"] >= 1)
        )
    if anchor_type == "invoice":
        invoice_hits, _ = keyword_score(lower_text, [r"\binvoice\b", r"\bbill to\b", r"\bremit\b"])
        return semantic_match_counts.get("invoice", 0) > 0 or invoice_hits > 0
    return False


def should_carry(anchor_type: str, result: dict, settings: dict) -> bool:
    metrics = result["signals"]["metrics"]
    semantic_match_counts = result["signals"]["semantic_match_counts"]
    table_signals = result["signals"]["table_signals"]
    top_candidate_type = result["candidates"][0]["page_type"] if result.get("candidates") else result["page_type"]
    skew_angle = abs(float(result["page_manifest"].get("detected_skew_angle") or 0.0))
    if result["page_type"] == "blank_separator":
        return False
    if not has_trustworthy_ocr_witness(result, settings):
        return False
    if not meets_context_evidence_floor(anchor_type, result, settings):
        return False

    if anchor_type == "agenda":
        return (
            metrics["agenda_list_lines"] >= int(settings.get("agenda_continuation_list_threshold", 4))
            and semantic_match_counts.get("agenda", 0) > 0
        )
    if anchor_type == "minutes":
        return (
            metrics["motion_lines"] > 0
            or metrics["vote_lines"] > 0
            or semantic_match_counts.get("minutes", 0) > 0
            or (
                result["signals"]["alnum_count"] <= int(settings.get("low_text_attachment_alnum_threshold", 80))
                and has_civic_signature_markers(result.get("text", ""))
            )
        )
    if anchor_type == "reference_or_procedure":
        return semantic_match_counts.get("reference_or_procedure", 0) > 0 or metrics["section_lines"] > 0
    if anchor_type == "legislative_prose":
        return (
            semantic_match_counts.get("legislative_prose", 0) > 0
            or metrics["vote_lines"] > 0
            or (
                result["signals"]["alnum_count"] <= int(settings.get("low_text_attachment_alnum_threshold", 80))
                and has_civic_signature_markers(result.get("text", ""))
            )
        )
    if anchor_type == "contract_or_agreement":
        return (
            semantic_match_counts.get("contract_or_agreement", 0) > 0
            or metrics["contract_lines"] > 0
            or metrics["section_lines"] > 0
        )
    if anchor_type == "financial_report":
        if (
            semantic_match_counts.get("financial_report", 0) == 0
            and metrics["finance_lines"] == 0
            and (
                top_candidate_type == "table_or_mixed_layout"
                or table_signals.get("is_strong_numeric_table", False)
                or (
                    table_signals.get("table_regions", 0) >= int(settings.get("financial_context_block_table_regions", 4))
                    and metrics["numeric_table_lines"] >= 4
                )
                or (
                    skew_angle >= float(settings.get("financial_context_block_skew_threshold", 12.0))
                    and top_candidate_type == "table_or_mixed_layout"
                )
            )
        ):
            return False
        return (
            semantic_match_counts.get("financial_report", 0) > 0
            or metrics["finance_lines"] > 0
            or result["signals"]["table_signals"]["currency_count"] > 0
            or metrics["heading_lines"] >= 2
        )
    if anchor_type == "government_form":
        lower_text = (result.get("text", "") or "").lower()
        header_count, _ = has_government_form_header_anchor(metrics, settings)
        support_count, _ = keyword_score(lower_text, SUPPORT_FORM_PATTERNS)
        body_count, _ = keyword_score(lower_text, BODY_FORM_PATTERNS)
        if has_meeting_packet_cover_structure(lower_text, metrics) or has_meeting_staff_summary_markers(lower_text):
            return False
        return (
            semantic_match_counts.get("government_form", 0) > 0
            and (
                has_government_form_structure(metrics, settings)
                or support_count > 0
                or body_count > 0
                or (header_count >= 2 and metrics["signature_form_lines"] >= 1)
            )
        )
    if anchor_type == "invoice":
        return semantic_match_counts.get("invoice", 0) > 0
    return False


def apply_contextual_page_typing(results: list[dict], settings: dict) -> list[dict]:
    max_anchor_distance = int(settings.get("context_max_anchor_distance", 6))
    sandwich_distance = int(settings.get("context_sandwich_distance", 2))

    anchors = [index for index, result in enumerate(results) if is_anchor(result, settings)]

    def assign_context(result: dict, page_type: str, anchor_index: int) -> None:
        if page_type == result["page_type"]:
            return
        result["page_type"] = page_type
        result["confidence"] = max(result["confidence"], 0.78)
        result["reason"] = f"CONTEXT_CARRY_{page_type.upper()}"
        result["best_source"] = "context"
        result["signals"]["context_anchor_page"] = results[anchor_index]["page_manifest"].get("source_page_number")
        result["signals"]["context_anchor_type"] = page_type
        result["candidates"].append(
            {
                "page_type": page_type,
                "score": round(result["confidence"], 2),
                "matched_patterns": [f"context_anchor_page:{results[anchor_index]['page_manifest'].get('source_page_number')}"],
                "source": "context",
            }
        )

    for index, result in enumerate(results):
        if not is_weak_for_context(result):
            continue
        if not has_trustworthy_ocr_witness(result, settings):
            continue
        prev_anchor = next((anchor for anchor in reversed(anchors) if anchor < index and index - anchor <= sandwich_distance), None)
        next_anchor = next((anchor for anchor in anchors if anchor > index and anchor - index <= sandwich_distance), None)
        if prev_anchor is None or next_anchor is None:
            continue
        prev_type = results[prev_anchor]["page_type"]
        next_type = results[next_anchor]["page_type"]
        if prev_type == next_type and meets_context_evidence_floor(prev_type, result, settings):
            assign_context(result, prev_type, prev_anchor)

    last_anchor_index = None
    for index, result in enumerate(results):
        if result["page_type"] == "blank_separator":
            last_anchor_index = None
            continue
        if is_anchor(result, settings):
            last_anchor_index = index
            continue
        if last_anchor_index is None or not is_weak_for_context(result) or not has_trustworthy_ocr_witness(result, settings):
            continue
        anchor_type = results[last_anchor_index]["page_type"]
        if index - last_anchor_index <= max_anchor_distance and should_carry(anchor_type, result, settings):
            assign_context(result, anchor_type, last_anchor_index)

    next_anchor_index = None
    for index in range(len(results) - 1, -1, -1):
        result = results[index]
        if result["page_type"] == "blank_separator":
            next_anchor_index = None
            continue
        if is_anchor(result, settings):
            next_anchor_index = index
            continue
        if next_anchor_index is None or not is_weak_for_context(result) or not has_trustworthy_ocr_witness(result, settings):
            continue
        anchor_type = results[next_anchor_index]["page_type"]
        if next_anchor_index - index <= max_anchor_distance and should_carry(anchor_type, result, settings):
            assign_context(result, anchor_type, next_anchor_index)

    return results


def run_page_typing(base_dir: Path, run_id: str, thresholds: dict) -> dict:
    log_dir = base_dir / "logs" / "runs" / run_id
    manifest_dir = base_dir / "manifests"
    run_handler = ManifestHandler(manifest_dir / "runs")
    page_handler = ManifestHandler(manifest_dir / "pages")
    logger = PipelineLogger(log_dir, "state_machine_page_typer")

    settings = thresholds.get("state_machine", {})
    run_manifest = run_handler.load(run_id)
    page_ids = run_manifest.get("page_ids", [])
    page_type_counts: dict[str, int] = {}
    function_type_counts: dict[str, int] = {}
    layout_type_counts: dict[str, int] = {}
    support_role_counts: dict[str, int] = {}
    page_family_counts: dict[str, int] = {}
    page_layout_counts: dict[str, int] = {}
    page_support_subtype_counts: dict[str, int] = {}
    sort_lane_counts: dict[str, int] = {}
    review_state_counts: dict[str, int] = {}
    suspicious_pages: list[dict] = []
    provisional_results: list[dict] = []

    logger.info("PAGE_TYPING_START", "SUCCESS", run_id=run_id, message=f"Typing {len(page_ids)} pages.")

    for page_id in page_ids:
        page_manifest = page_handler.load(page_id)
        text = load_text(base_dir, page_manifest)
        page_type, confidence, reason, signals, candidates = determine_page_type(text, page_manifest, settings)
        provisional_results.append(
            {
                "page_id": page_id,
                "page_manifest": page_manifest,
                "page_type": page_type,
                "confidence": confidence,
                "reason": reason,
                "signals": signals,
                "candidates": candidates,
                "text": text,
                "best_source": candidates[0]["source"],
            }
        )

    provisional_results.sort(key=lambda item: item["page_manifest"].get("source_page_number", 0))
    finalized_results = apply_contextual_page_typing(provisional_results, settings)
    finalized_results = enrich_classification_metadata(finalized_results, settings)
    suspicion_report_threshold = float(settings.get("suspicion_report_threshold", 0.35))

    for result in finalized_results:
        page_id = result["page_id"]
        page_type = result["page_type"]
        function_type = result["function_type"]
        layout_type = result["layout_type"]
        support_role = result["support_role"]
        page_family = result["page_family"]
        page_layout = result["page_layout"]
        page_support_subtype = result.get("page_support_subtype")
        sort_lane = result["sort_lane"]
        review_state = result["review_state"]
        page_type_counts[page_type] = page_type_counts.get(page_type, 0) + 1
        function_type_counts[function_type] = function_type_counts.get(function_type, 0) + 1
        layout_type_counts[layout_type] = layout_type_counts.get(layout_type, 0) + 1
        support_role_counts[support_role] = support_role_counts.get(support_role, 0) + 1
        page_family_counts[page_family] = page_family_counts.get(page_family, 0) + 1
        page_layout_counts[page_layout] = page_layout_counts.get(page_layout, 0) + 1
        sort_lane_counts[sort_lane] = sort_lane_counts.get(sort_lane, 0) + 1
        review_state_counts[review_state] = review_state_counts.get(review_state, 0) + 1
        if page_support_subtype:
            page_support_subtype_counts[page_support_subtype] = page_support_subtype_counts.get(page_support_subtype, 0) + 1
        if result["suspicion_score"] >= suspicion_report_threshold:
            suspicious_pages.append(
                {
                    "page_id": page_id,
                    "run_page_id": result["page_manifest"].get("run_page_id", page_id),
                    "page_machine_code": result["page_manifest"].get("page_machine_code"),
                    "source_page_number": result["page_manifest"].get("source_page_number"),
                    "page_type": page_type,
                    "function_type": function_type,
                    "layout_type": layout_type,
                    "support_role": support_role,
                    "page_family": page_family,
                    "page_layout": page_layout,
                    "page_support_subtype": page_support_subtype,
                    "sort_lane": sort_lane,
                    "sort_lane_source": result.get("sort_lane_source"),
                    "sort_lane_reasons": result.get("sort_lane_reasons", []),
                    "review_state": review_state,
                    "review_state_source": result.get("review_state_source"),
                    "review_state_reasons": result.get("review_state_reasons", []),
                    "suspicion_score": result["suspicion_score"],
                    "suspicion_reasons": result["suspicion_reasons"],
                }
            )
        page_handler.update(
            page_id,
            {
                "page_type": page_type,
                "page_type_confidence": result["confidence"],
                "function_type": function_type,
                "function_type_source": result["function_type_source"],
                "layout_type": layout_type,
                "layout_type_source": result["layout_type_source"],
                "support_role": support_role,
                "support_role_source": result.get("support_role_source"),
                "support_role_confidence": result.get("support_role_confidence", 0.0),
                "support_role_reasons": result.get("support_role_reasons", []),
                "page_family": page_family,
                "page_layout": page_layout,
                "page_family_source": result["page_family_source"],
                "page_layout_source": result["page_layout_source"],
                "page_support_subtype": page_support_subtype,
                "page_support_subtype_source": result.get("page_support_subtype_source"),
                "page_support_subtype_confidence": result.get("page_support_subtype_confidence", 0.0),
                "page_support_subtype_reasons": result.get("page_support_subtype_reasons", []),
                "sort_lane": sort_lane,
                "sort_lane_source": result.get("sort_lane_source"),
                "sort_lane_reasons": result.get("sort_lane_reasons", []),
                "review_state": review_state,
                "review_state_source": result.get("review_state_source"),
                "review_state_reasons": result.get("review_state_reasons", []),
                "suspicion_score": result["suspicion_score"],
                "suspicion_reasons": result["suspicion_reasons"],
                "decision_reason": result["reason"],
                "page_type_signals": result["signals"],
                "page_type_candidates": result["candidates"],
                "current_state": "typed",
            },
        )
        logger.info(
            "PAGE_TYPED",
            "SUCCESS",
            run_id=run_id,
            page_id=page_id,
            message=f"{page_type} ({result['confidence']:.2f})",
            extra={"page_type": page_type, "decision_reason": result["reason"]},
        )

    run_handler.update(
        run_id,
        {
            "page_type_counts": page_type_counts,
            "function_type_counts": function_type_counts,
            "layout_type_counts": layout_type_counts,
            "support_role_counts": support_role_counts,
            "page_family_counts": page_family_counts,
            "page_layout_counts": page_layout_counts,
            "page_support_subtype_counts": page_support_subtype_counts,
            "sort_lane_counts": sort_lane_counts,
            "review_state_counts": review_state_counts,
            "suspicious_pages": suspicious_pages,
            "suspicious_page_count": len(suspicious_pages),
            "status": "typed",
        },
    )
    logger.info("PAGE_TYPING_COMPLETE", "SUCCESS", run_id=run_id)
    return page_type_counts
