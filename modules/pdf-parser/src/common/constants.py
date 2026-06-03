"""
PDF Parser Industrial Constants
Central authority for codes, authorities, and budgets.
"""

# --- BOUNDARY CODES (Spec Section 19.1) ---
BOUNDARY_TYPE_CHANGE = "BOUNDARY_TYPE_CHANGE"      # Page type transition (e.g., Mode 1 -> Mode 2)
BOUNDARY_AGENDA_HEADER = "BOUNDARY_AGENDA_HEADER"  # 'Call to Order' detected
BOUNDARY_BUDGET_START = "BOUNDARY_BUDGET_START"    # 'Appropriation' table detected
BOUNDARY_DOCUMENT_START = "DOCUMENT_START"         # Start of a logical document
BOUNDARY_SECTION_BREAK = "SECTION_BREAK"           # Major section change within a document

# --- QUARANTINE AUTHORITY (Piece 3.1.3) ---
# Formally assigned to 'Integrity' worker class.
QUARANTINE_AUTHORITY = "INTEGRITY_CLASS"

# --- RETRY BUDGETS (Spec Section 31.1) ---
MAX_STAGE_RETRIES = 2
SUBPROCESS_TIMEOUT_SECONDS = 600
EXPONENTIAL_BACKOFF_FACTOR = 5

# --- ESCALATION POLICIES ---
ESCALATION_HUMAN_REVIEW = "HUMAN_REVIEW_REQUIRED"
ESCALATION_QUARANTINE = "QUARANTINE_ISOLATION"

# --- DOMAIN MAPPING ---
MODE_1_AGENDA = "mode_1"
MODE_2_BUDGET = "mode_2"
MODE_0_UNKNOWN = "unknown"

# --- SERVICE STATES ---
SERVICE_STATE_BOOTING = "booting"
SERVICE_STATE_READY = "ready"
SERVICE_STATE_PROCESSING = "processing"
SERVICE_STATE_FATAL = "fatal"

# --- RUN STATES ---
RUN_STATE_HANDSHAKE_RECEIVED = "handshake_received"
RUN_STATE_DROP_VERIFIED = "drop_verified"
RUN_STATE_PREPARED = "prepared"
RUN_STATE_SPLIT = "split"
RUN_STATE_GEOMETRY_NORMALIZED = "geometry_normalized"
RUN_STATE_FEATURES_COMPUTED = "features_computed"
RUN_STATE_TYPED = "typed"
RUN_STATE_EXTRACTED = "extracted"
RUN_STATE_PACKAGED = "packaged"
RUN_STATE_HANDOFF_READY = "handoff_ready"
RUN_STATE_COMPLETED = "completed"
RUN_STATE_FAILED = "failed"

# --- PAGE TYPES ---
PAGE_TYPE_BLANK_SEPARATOR = "blank_separator"
PAGE_TYPE_AGENDA = "agenda"
PAGE_TYPE_MINUTES = "minutes"
PAGE_TYPE_REFERENCE_PROCEDURE = "reference_or_procedure"
PAGE_TYPE_LEGISLATIVE_PROSE = "legislative_prose"
PAGE_TYPE_CONTRACT = "contract_or_agreement"
PAGE_TYPE_FINANCIAL_REPORT = "financial_report"
PAGE_TYPE_GOVERNMENT_FORM = "government_form"
PAGE_TYPE_INVOICE = "invoice"
PAGE_TYPE_TABLE_MIXED = "table_or_mixed_layout"
PAGE_TYPE_GENERIC_PROSE = "generic_prose"
PAGE_TYPE_POWERPOINT = "powerpoint"
