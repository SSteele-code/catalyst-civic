# PRATTLE: Transcript Typer & Conversation Reconstruction Engine

**Industrial-grade municipal meeting transcription and speaker attribution pipeline.**

This repository contains the `PRATTLE` suite, designed to transform raw meeting transcripts (YouTube VTT or local Whisper audio) into structured, speaker-attributed JSON dialogue records.

## 🏗️ The 5-Lobe Architecture

1.  **INGEST (`PRATTLE/ingest.py`)**: Normalizes raw VTT, performs the "Industrial Squeeze" to remove YouTube caption redundancy, and detects 1.5s silence gaps for sentence boundaries.
2.  **PHONETIC TRANSLATOR (`PRATTLE/phonetic_translator.py`)**: Corrects common municipal/legal hallucinations (e.g., "Gini coefficient" -> "Virginia Code") using a specialized municipal dictionary.
3.  **ROBERTS STATE (`PRATTLE/roberts_state.py`)**: A discovery-based Phase Machine that tracks meeting progression and dynamically builds the Council Roster from Roll Calls.
4.  **QUOTER (`PRATTLE/quoter.py`)**: An adjacency-based resolver that attributes UNKNOWN turns to speakers based on Chair acknowledgments and procedural context.
5.  **QA (`PRATTLE/qa.py`)**: Final integrity gate + delivery stage. Uses a **squeezed-source coverage gate** (>=95% token preservation vs deduplicated source baseline) and emits transcript glossary-hover candidate artifacts.

## 🏭 Production Components

*   **PULL (`PULL/orchestrator.py`)**: Automated ingestion of transcripts from the Town of Richlands YouTube channel.
*   **AUDITORY_FACTORY**: Local transcription pipeline using OpenAI Whisper (Large-V3) for "dark" meetings lacking auto-captions.
*   **Production Line (`production_line.py`)**: Orchestrates the entire FIFO loop (Pull -> Process -> Verify -> Log) with agentic parallel execution and "Tail Anchor" integrity verification.

## 📂 Data Governance

The engine expects the following external directory structure (ignored by git):
- `_Vualt/YTT/`: Raw source VTT files.
- `_staging/`: Temporary pulse workspace.
- `_output/`: Final high-fidelity JSON records.
- `_Auditory/`: Local audio extraction and Whisper staging.
- `_output/_glossary/`: Transcript glossary-hover candidate artifacts (`*.glossary_hover.json` / `*.glossary_hover.txt`).

Glossary tracking artifacts:
- `M1_TS_GLOSSARY_HOVER_MANIFEST.jsonl`
- `transcript_glossary_hover_state.json`

Disposition tracking artifacts:
- `M1_TS_DISPOSITION_LOG.jsonl`
- `transcript_disposition_state.json`

Disposition code suffixes (appended as `machine_code_with_disposition`):
- `.OK95` - Passed QA with squeezed-source coverage >=95%
- `.EMPTYVTT` - Source VTT has no usable caption payload
- `.LOWSIG` - Low-signal/tiny transcript blocked by QA

---
*Mission: Total domination of the public record.*
