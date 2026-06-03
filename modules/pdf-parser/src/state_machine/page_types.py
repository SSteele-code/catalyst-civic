from __future__ import annotations

from src.common.constants import (
    PAGE_TYPE_AGENDA,
    PAGE_TYPE_BLANK_SEPARATOR,
    PAGE_TYPE_CONTRACT,
    PAGE_TYPE_FINANCIAL_REPORT,
    PAGE_TYPE_GENERIC_PROSE,
    PAGE_TYPE_GOVERNMENT_FORM,
    PAGE_TYPE_INVOICE,
    PAGE_TYPE_LEGISLATIVE_PROSE,
    PAGE_TYPE_MINUTES,
    PAGE_TYPE_POWERPOINT,
    PAGE_TYPE_REFERENCE_PROCEDURE,
    PAGE_TYPE_TABLE_MIXED,
)


FUNCTION_TYPE_BY_PAGE_TYPE = {
    PAGE_TYPE_AGENDA: "agenda",
    PAGE_TYPE_MINUTES: "minutes",
    PAGE_TYPE_REFERENCE_PROCEDURE: "reference",
    PAGE_TYPE_LEGISLATIVE_PROSE: "legislative",
    PAGE_TYPE_CONTRACT: "contract",
    PAGE_TYPE_FINANCIAL_REPORT: "finance",
    PAGE_TYPE_GOVERNMENT_FORM: "admin",
    PAGE_TYPE_INVOICE: "admin",
    PAGE_TYPE_BLANK_SEPARATOR: "separator",
    PAGE_TYPE_GENERIC_PROSE: "unknown",
    PAGE_TYPE_POWERPOINT: "unknown",
    PAGE_TYPE_TABLE_MIXED: "unknown",
}


PAGE_TYPE_REGISTRY = {
    PAGE_TYPE_BLANK_SEPARATOR: {
        "description": "Intentional blank page or separator page.",
        "extractor": "extract_blank_separator",
    },
    PAGE_TYPE_AGENDA: {
        "description": "Meeting agenda pages and continuations.",
        "extractor": "extract_agenda",
    },
    PAGE_TYPE_MINUTES: {
        "description": "Meeting minutes, vote logs, and parliamentary reference pages.",
        "extractor": "extract_minutes",
    },
    PAGE_TYPE_REFERENCE_PROCEDURE: {
        "description": "Reference, policy, or parliamentary procedure pages.",
        "extractor": "extract_reference_or_procedure",
    },
    PAGE_TYPE_LEGISLATIVE_PROSE: {
        "description": "Resolution, ordinance, and legislative prose pages.",
        "extractor": "extract_legislative_prose",
    },
    PAGE_TYPE_CONTRACT: {
        "description": "Contracts, leases, task orders, and agreements.",
        "extractor": "extract_contract_or_agreement",
    },
    PAGE_TYPE_FINANCIAL_REPORT: {
        "description": "Financial policy pages, budget pages, and finance narrative pages.",
        "extractor": "extract_financial_report",
    },
    PAGE_TYPE_GOVERNMENT_FORM: {
        "description": "Government forms and standardized filing pages.",
        "extractor": "extract_government_form",
    },
    PAGE_TYPE_INVOICE: {
        "description": "Invoices and bill-like pages.",
        "extractor": "extract_invoice",
    },
    PAGE_TYPE_TABLE_MIXED: {
        "description": "Table-heavy or mixed-layout pages requiring geometry-aware extraction.",
        "extractor": "extract_structured_mixed",
    },
    PAGE_TYPE_GENERIC_PROSE: {
        "description": "Fallback prose page type for uncategorized text pages.",
        "extractor": "extract_generic_prose",
    },
    PAGE_TYPE_POWERPOINT: {
        "description": "Slide-style or PowerPoint-export pages.",
        "extractor": "extract_powerpoint",
    },
}


PAGE_TYPE_PRIORITY = [
    PAGE_TYPE_BLANK_SEPARATOR,
    PAGE_TYPE_AGENDA,
    PAGE_TYPE_MINUTES,
    PAGE_TYPE_REFERENCE_PROCEDURE,
    PAGE_TYPE_LEGISLATIVE_PROSE,
    PAGE_TYPE_GOVERNMENT_FORM,
    PAGE_TYPE_INVOICE,
    PAGE_TYPE_CONTRACT,
    PAGE_TYPE_FINANCIAL_REPORT,
    PAGE_TYPE_POWERPOINT,
    PAGE_TYPE_TABLE_MIXED,
    PAGE_TYPE_GENERIC_PROSE,
]


def page_type_to_function_type(page_type: str | None) -> str:
    if not page_type:
        return "unknown"
    return FUNCTION_TYPE_BY_PAGE_TYPE.get(page_type, "unknown")


def resolve_structured_extractor(layout_type: str, function_type: str) -> str:
    if layout_type == "form":
        return "extract_structured_form"
    if layout_type == "mixed":
        return "extract_structured_mixed"
    if layout_type == "table":
        return "extract_structured_table"
    if function_type in {"finance", "admin"}:
        return "extract_structured_mixed"
    return "extract_structured_table"


def resolve_extractor_name(page_manifest: dict) -> tuple[str, str]:
    page_type = page_manifest.get("page_type", PAGE_TYPE_GENERIC_PROSE)
    function_type = page_manifest.get("function_type") or page_type_to_function_type(page_type)
    layout_type = page_manifest.get("layout_type") or page_manifest.get("page_layout") or "prose"
    sort_lane = page_manifest.get("sort_lane") or "default"

    if function_type == "separator" or layout_type == "blank":
        return "extract_blank_separator", "function_layout"
    if sort_lane == "weak_fallback":
        return "extract_weak_page_evidence", "sort_lane_weak_fallback"
    if sort_lane == "table_specialist":
        return resolve_structured_extractor(layout_type, function_type), "sort_lane_table_specialist"
    if page_type == PAGE_TYPE_INVOICE:
        return "extract_invoice", "page_type_override"
    if function_type == "agenda":
        return "extract_agenda", "function_layout"
    if function_type == "minutes":
        return "extract_minutes", "function_layout"
    if function_type == "reference":
        if layout_type == "slide":
            return "extract_powerpoint", "function_layout"
        return "extract_reference_or_procedure", "function_layout"
    if function_type == "legislative":
        return "extract_legislative_prose", "function_layout"
    if function_type == "contract":
        return "extract_contract_or_agreement", "function_layout"
    if function_type == "finance":
        if layout_type in {"table", "mixed"}:
            return resolve_structured_extractor(layout_type, function_type), "function_layout"
        return "extract_financial_report", "function_layout"
    if function_type == "admin":
        if layout_type == "form":
            return "extract_structured_form", "function_layout"
        if layout_type in {"table", "mixed"}:
            return resolve_structured_extractor(layout_type, function_type), "function_layout"
        return "extract_government_form", "function_layout"
    if layout_type == "slide":
        return "extract_powerpoint", "layout_fallback"
    if layout_type == "form":
        return "extract_structured_form", "layout_fallback"
    if layout_type in {"table", "mixed"}:
        return resolve_structured_extractor(layout_type, function_type), "layout_fallback"

    fallback = PAGE_TYPE_REGISTRY.get(page_type, PAGE_TYPE_REGISTRY[PAGE_TYPE_GENERIC_PROSE])["extractor"]
    return fallback, "page_type_fallback"
