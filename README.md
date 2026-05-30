# Catalyst Civic

A production civic intelligence platform for local government transparency.

Catalyst Civic ingests, reconstructs, and structures public municipal records — converting scanned agenda packets and raw meeting transcripts into a searchable, machine-readable civic knowledge base. It is not a prototype. It is a running production system with measurable throughput and quality-gated output.

---

## What It Does

Local government produces two primary record types: **agenda packets** (PDFs distributed before council meetings) and **meeting transcripts** (audio/video recordings of what actually happened). Both are notoriously difficult to work with — scanned, inconsistently formatted, and largely unsearchable.

Catalyst Civic solves both problems with two fully operational Mode 1 pipelines:

- **Agenda / Council Packet Pipeline** — automated PDF intake through structured database delivery
- **Transcript Intelligence Pipeline** — YouTube transcript ingestion through procedurally-aware, speaker-attributed records

---

## Production Scale (Live — May 30, 2026)

This system is running against a live PostgreSQL database with active ingestion across agenda, transcript, and authority lanes.

| Table | Records |
|---|---|
| Agenda meetings processed | 263 |
| Agenda items extracted | 7,747 |
| Transcript meetings processed | 171 |
| Transcript turns attributed | 98,961 |
| Authority registry entities | 558 |
| Identity aliases resolved | 578 |
| Observations recorded | 3,371 |
| **Total indexed records** | **111,912** |

**Quality gate performance:** 171/171 transcript meetings at OK95 threshold. 100% pass rate.  
**Speaker attribution:** 100% of 98,961 transcript turns carry a named speaker.  
**Agenda depth:** 29.46 items per meeting average across 13 years of council history (May 2013 – April 2026).  
**Transcript resolution:** 578.72 attributed turns per meeting average.

This is a functioning end-to-end ingestion-to-database system with measurable throughput and explicit quality controls — not a demo dataset.

---

## Architecture

```
catalyst-civic/
├── _Scripts/        # Operational orchestrators, batch runners, production entry points
└── _Modules/        # Reusable processing engines (parser core, PRATTLE, authority layer)
```

The platform separates orchestration from execution. Scripts drive production runs. Modules contain the logic that runs inside them. Each pipeline module is independently testable and composable.

---

## Pipeline 1 — Agenda / Council Packet Processing

Converts scanned municipal PDFs into structured agenda records.

**Stages:**
1. Automated source discovery and PDF intake
2. Machine-code assignment and manifest tracking
3. Optical parsing and structured page extraction (native text + OCR fallback)
4. Semantic scaffold generation and hierarchy shaping
5. Database loading into structured agenda tables
6. Authority and registry enrichment for civic entities
7. Vaulted output and source archival with ledgered pipeline states

The parser handles the full range of real-world municipal document chaos — handwritten annotations, rotated charts, embedded PowerPoint pages, copied-and-rescanned PDFs, and inconsistent layouts across decades of council history.

Each run is tracked through a manifest-driven lifecycle with explicit per-run state history. Pipeline completion is traceable: 263 pipeline ledger rows match 263 processed meetings exactly.

---

## Pipeline 2 — Transcript Intelligence (PRATTLE Engine)

Converts raw YouTube meeting transcripts into procedurally-aware, speaker-attributed records.

Raw auto-generated transcripts are phonetically garbled, speaker-blind, and procedurally unstructured. PRATTLE reconstructs them through five staged passes:

| Stage | Function |
|---|---|
| **Ingest** | Pull and normalize raw transcript stream |
| **Phonetic Correction** | Resolve municipal proper nouns, names, and domain vocabulary mangled by auto-transcription |
| **Procedural State Modeling** | Apply Roberts Rules state machine to segment transcript by parliamentary phase |
| **Attribution Resolution** | Assign speaker identity to each turn using voice pattern and contextual cues |
| **QA + Delivery** | Gate output at 95% quality threshold before database delivery |

Output: high-fidelity JSON records with full speaker attribution, procedural context, and disposition tagging — loaded into structured transcript and authority schemas.

---

## The Roberts Rules State Machine

The hardest problem in transcript intelligence is knowing *where you are* in a meeting. Without structure, a 3-hour council session is an undifferentiated wall of text.

The insight driving PRATTLE's procedural modeling: **Roberts Rules of Order is already a finite state machine.** Every motion, second, debate period, amendment, and vote is a defined state transition. The presiding officer is the trigger.

By modeling the meeting procedurally — using the presider's language as state change events — the platform segments each transcript into parliamentary phases automatically. This makes attribution, disposition tracking, and agenda item alignment tractable problems instead of impossible ones.

This is not a standard NLP approach. It is a domain insight applied as an architectural decision.

---

## Authority Layer

Alongside the two primary pipelines, Catalyst Civic maintains a growing civic knowledge base:

- **558 registry entities** — named civic actors, organizations, and institutions
- **578 identity aliases** — resolved name variants across years of records
- **3,371 observations** — structured facts linked to entities across meetings and documents

The authority layer provides enrichment context for both pipelines and supports entity-aware search and governance workflows.

---

## Validation Architecture

Output quality is enforced at multiple levels:

- **Manifest-driven run lifecycle** — every pipeline run has explicit state tracking from intake to delivery
- **Per-run state history** — no silent failures; each stage transition is logged
- **Quality gates** — transcript output held at 95% threshold before database write
- **Source traceability** — every extracted record links back to its source document and page
- **Row-count reconciliation** — pipeline ledger counts reconcile against meeting counts at completion

---

## Stack

- **Language:** Python
- **Database:** PostgreSQL
- **OCR:** Tesseract + PyMuPDF
- **Document processing:** OpenCV, pandas, NumPy
- **Transcript source:** YouTube transcript API
- **Output formats:** Structured JSON, PostgreSQL schemas

---

## Purpose

Catalyst Civic exists because local government transparency depends on public records actually being accessible — not just technically public. Scanned PDFs and auto-generated YouTube transcripts are public in name only. This platform makes them searchable, structured, and usable.

The investigation it supports is ongoing. The tool is open. The data stays local.

---

*Built and directed by Simon Steele — Richlands, Virginia*
