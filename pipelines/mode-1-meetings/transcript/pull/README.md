# Catalyst Civic: PULL Layer (Source Acquisition)

The PULL layer is responsible for automated ingestion of source meeting transcripts (WebVTT) from YouTube into the flat M1 source store.

## 🛠️ The Ingestion Logic

### 1. YouTube Fetcher (`fetch_yt.py`)
*   **Mission**: Pull raw video metadata and VTT (WebVTT) transcript files from specified YouTube channels.
*   **Target**: Town of Richlands, VA official channel.

### 2. VTT Deduplicator (`dedup_vtt.py`)
*   **Mission**: Optional utility for generating derived text from VTT when needed downstream.
*   **Logic**: Removes sequential duplicate lines and collapses timestamped fragments into clean, contiguous dialogue blocks.

### 3. PULL Orchestrator (`orchestrator.py`)
*   **Mission**: Manage batch discovery, fetch, and machine-code storage.
*   **Output Root**: `_Sources/M1-Meetings/Transcripts/`
*   **File Naming**: `M1-TS-<document_number>-<created_yyyymmdd>-<pulled_yyyymmdd>-<youtube_id>.<ext>`
*   **Assets per video**: one source `.vtt`
*   **Manifest**: `M1_TS_MANIFEST.jsonl`
*   **State**: `yt_state.json`

## 📁 Output Structure

```
_Sources/M1-Meetings/Transcripts/
├── yt_state.json
├── M1_TS_MANIFEST.jsonl
├── M1-TS-000001-20230613-20260411-px_BB-zSPxE.vtt
└── ...
```

## 🧹 Git Policy

- `_Sources/M1-Meetings/Transcripts/` is local data storage and is gitignored.
- Source `.vtt`, `yt_state.json`, and `M1_TS_MANIFEST.jsonl` are not versioned.

## 🤖 Bot/Agent Instructions
*   **Flat Storage**: Do not create year folders under `Transcripts/`.
*   **Verify the ID**: Keep the 11-character YouTube Video ID in every transcript filename.
*   **Source-Only Output**: Do not emit `.txt` transcript derivatives in this folder.
*   **Manifest File**: `M1_TS_MANIFEST.jsonl` is the canonical ingest ledger and machine-code registry.
*   **State File**: `yt_state.json` tracks processed video IDs.
*   **Idempotent**: Re-running the orchestrator skips videos already ingested.
