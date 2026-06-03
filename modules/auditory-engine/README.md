# Auditory Engine — Whisper Transcription for Dark Meetings

A parallel audio transcription engine for municipal council meetings that lack auto-generated captions ("dark" meetings). Built on `faster-whisper` (CTranslate2) with municipal domain tuning.

## Problem

YouTube auto-captions cover roughly 60% of the Richlands Town Council archive. The remaining 40% are older or low-quality recordings that never received captions. This engine fills the gap by transcribing the raw `.m4a` audio locally into WebVTT, feeding the same downstream PRATTLE pipeline used for YouTube-sourced transcripts.

## Architecture

A single-file pipeline: `master_distiller.py`

Stages:
1. **Bottling** — FFmpeg normalizes audio (loudnorm) and segments it into 5-minute chunks in one pass
2. **Warmup** — loads `WhisperModel("medium")` with 3 parallel workers
3. **Distillation** — parallel chunk transcription via `ThreadPoolExecutor`, each chunk written to JSON
4. **Vault Assembly** — reassembles chunk JSONs into a single `.vtt` output with corrected timestamps

## Municipal Bias Engine

The transcriber uses an `initial_prompt` loaded with known Richlands-specific names, organizations, and legal terms:

```
Richlands, Virginia, Town Council, Mayor Curry, Seth White, Gary Jackson,
Rick Wood, Jordan Bales, Jan White, Laura Mollo, Town Manager Ron Holt,
Virginia Code Section 2.2-3711, Lexite Corporation, VCEDA, PSA, CART...
```

This biases the model toward correct local proper nouns and reduces hallucinations on civic legal language.

## Performance Settings

| Setting | Value | Reason |
|---|---|---|
| Model | `medium` | Balance of accuracy and speed for civic speech |
| `device` | `cpu` | No GPU required |
| `compute_type` | `int8` | Faster inference with acceptable accuracy |
| `cpu_threads` | `2` | Per-worker thread budget |
| `num_workers` | `3` | Parallel chunk processing |
| `beam_size` | `1` | Speed over beam search |
| `vad_filter` | `True` | Suppresses silence hallucinations |
| `condition_on_previous_text` | `False` | Prevents infinite repetition loops |

## Output

For each source file `<machine_code>.m4a`, the engine writes `<machine_code>.vtt` to the transcript vault. Output is standard WebVTT, compatible with the PRATTLE downstream processor.

## Usage

```bash
# Transcribe a single file
python master_distiller.py --code M1.TS.000163.NA.20260501

# Batch: transcribe all .m4a files not yet in the vault
python master_distiller.py --batch
```

## Path Configuration

The engine uses two hardcoded paths that must be updated before local use:

```python
VAULT_DIR  = Path(r"<your repo root>/_Sources/M1-Meetings/Transcripts/_Vualt/YTT")
SOURCE_DIR = Path(r"<your repo root>/_Sources/M1-Meetings/Transcripts/_Auditory/_source")
```

## Setup

- Python 3.12+
- FFmpeg installed and on PATH
- `pip install -r requirements.txt`

`faster-whisper` will download the `medium` model (~1.5 GB) on first run.

## Relationship to PDF Parser

This module mirrors the structural role of `pdf-parser` for audio input. PDF Parser handles document packets; Auditory Engine handles meeting recordings. Both feed their output into the same downstream PRATTLE transcript processor.
