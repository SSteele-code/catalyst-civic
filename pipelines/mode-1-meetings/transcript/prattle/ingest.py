import argparse
import json
import re
import shutil
from html import unescape
from pathlib import Path
from datetime import datetime

# PRATTLE Stage 1: INGEST
# Mission: Normalize raw VTT into a uniform JSON intermediate representation.

class Ingestor:
    def __init__(self, source_path: Path, staging_dir: Path):
        self.source_path = source_path
        self.machine_code = source_path.stem
        self.staging_path = staging_dir / self.machine_code
        self.staging_path.mkdir(parents=True, exist_ok=True)
        
        # Internal paths
        self.log_dir = self.staging_path / "logs"
        self.log_dir.mkdir(exist_ok=True)

    def strip_vtt_headers(self, raw_content: str) -> list[str]:
        # Strip VTT headers and timestamps
        text_lines = []
        for line in raw_content.splitlines():
            if "-->" in line or line.startswith("WEBVTT") or not line.strip():
                continue
            if line.startswith("Kind:") or line.startswith("Language:"):
                continue
            clean_line = re.sub(r"<[^>]+>", "", line).strip()
            if clean_line:
                text_lines.append(clean_line)
        return text_lines

    def detect_generation(self, text: str) -> int:
        if ">>" in text or "&gt;&gt;" in text:
            return 3
        
        total_chars = len(text)
        if total_chars == 0:
            return 1
            
        upper_chars = sum(1 for c in text if c.isupper())
        has_periods = "." in text
        
        upper_ratio = upper_chars / total_chars if total_chars > 0 else 0
        
        if has_periods and (upper_ratio > 0.05 or total_chars < 50):
            return 2
        return 1

    def clean_artifacts(self, text: str) -> str:
        # User Mandate: Preserve anything picked up by the mic.
        # We no longer strip [Music], [Applause], etc.
        # We only perform basic whitespace normalization to keep the flow.
        text = re.sub(r"\n+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def reconstruct_sentences(self, text: str) -> str:
        # Dictionary of phrases that likely start a new sentence
        starters = [
            "thank you", "okay", "let us", "we have", "is there", "good evening", 
            "mr mayor", "council", "i make a motion", "so moved", "i second",
            "roll call", "motion carries", "any discussion", "hearing none",
            "my name is", "i live at", "next item", "agenda", "members of council"
        ]
        
        # Phrases that likely end a sentence
        stoppers = ["amended", "carries", "please", "tonight", "agenda"]

        # 1. Protect common abbreviations
        abbreviations = ["Mr", "Ms", "Mrs", "Dr", "St"]
        for abbr in abbreviations:
            text = re.sub(rf"\b{abbr}\b\s+", f"{abbr}__SPACE__", text, flags=re.IGNORECASE)

        # 2. Apply boundary markers before starters
        for starter in starters:
            text = re.sub(rf"(?<![.!?])\s+\b({starter})\b", r". \1", text, flags=re.IGNORECASE)
            
        # 3. Apply boundary markers after stoppers if followed by common starts
        for stopper in stoppers:
            text = re.sub(rf"\b({stopper})\b\s+(i|we|the|this|that|so|my|mr)\b", r"\1. \2", text, flags=re.IGNORECASE)

        # 4. Special case: "Name Yes Name Yes" (Compressed Roll Call)
        # Look for the sequence: [Capitalized Word] [yes/no/aye/abstain]
        # and inject a period if another Capitalized Word follows.
        text = re.sub(rf"\b([A-Z][a-z]+)\s+(yes|no|aye|abstain)\b\s+([A-Z])", r"\1 \2. \3", text, flags=re.IGNORECASE)

        # 5. Restore abbreviations
        text = text.replace("__SPACE__", " ")

        # 6. Capitalize sentences
        def capitalize_match(m):
            return m.group(1) + m.group(2).upper()
            
        # Capitalize start of string
        text = re.sub(r"^([a-z])", lambda m: m.group(1).upper(), text)
        # Capitalize after sentence boundaries
        text = re.sub(r"([.!?]\s+)([a-z])", capitalize_match, text)
        
        # 7. Specialized Proper Noun Capitalization
        proper_nouns = ["Mayor", "Council", "Richlands", "Virginia", "Planning", "Commission"]
        for noun in proper_nouns:
            text = re.sub(rf"\b{noun}\b", noun, text, flags=re.IGNORECASE)

        # 8. Final cleanup
        text = re.sub(r"\.+", ".", text)
        text = re.sub(r"^\. ", "", text)
        text = re.sub(r"\s+\.", ".", text)
        return text

    def parse_vtt_with_timestamps(self, raw_content: str) -> list[dict]:
        entries = []
        current_time = None
        
        lines = raw_content.splitlines()
        for line in lines:
            line_unescaped = unescape(line)
            if "-->" in line_unescaped:
                # Accept both HH:MM:SS.mmm and MM:SS.mmm style timestamps.
                match = re.search(r"(?:(\d{1,2}):)?(\d{2}):(\d{2}\.\d{3})", line_unescaped)
                if match:
                    h_part, m_part, s_part = match.groups()
                    hours = int(h_part) if h_part is not None else 0
                    minutes = int(m_part)
                    seconds = float(s_part)
                    current_time = hours * 3600 + minutes * 60 + seconds
            elif current_time is not None and line.strip() and not line.startswith("WEBVTT") and not line.startswith("Kind:") and not line.startswith("Language:"):
                text = re.sub(r"<[^>]+>", "", line_unescaped).strip()
                if text:
                    entries.append({"ts": current_time, "text": text})
        return entries

    def merge_short_chunks(self, chunks: list[str], min_words: int = 16, hard_cap_words: int = 60) -> list[str]:
        """
        Reduce over-fragmentation by combining short adjacent chunks while
        preserving coarse sentence boundaries.
        """
        merged: list[str] = []
        buffer: list[str] = []

        def flush_buffer():
            nonlocal buffer
            if buffer:
                merged.append(" ".join(buffer).strip())
                buffer = []

        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue

            chunk_words = len(chunk.split())
            if not buffer:
                buffer.append(chunk)
                if chunk_words >= min_words:
                    flush_buffer()
                continue

            prospective = " ".join(buffer + [chunk]).strip()
            prospective_words = len(prospective.split())

            if prospective_words <= hard_cap_words:
                buffer.append(chunk)
            else:
                flush_buffer()
                buffer.append(chunk)

            current_buffer_text = " ".join(buffer).strip()
            buffer_words = len(current_buffer_text.split())
            if buffer_words >= min_words and current_buffer_text.endswith((".", "!", "?")):
                flush_buffer()

        flush_buffer()
        return merged

    def merge_overlapping_text(self, parts: list[str]) -> list[str]:
        if not parts:
            return []
            
        merged = [parts[0]]
        for i in range(1, len(parts)):
            prev = merged[-1]
            curr = parts[i]
            
            prev_words = prev.split()
            curr_words = curr.split()
            
            # Case-insensitive, punctuation-insensitive matching
            prev_lower = [w.lower().strip(".,!?:;") for w in prev_words]
            curr_lower = [w.lower().strip(".,!?:;") for w in curr_words]
            
            max_overlap = 0
            search_limit = min(len(prev_lower), len(curr_lower))
            
            for length in range(1, search_limit + 1):
                if prev_lower[-length:] == curr_lower[:length]:
                    max_overlap = length
            
            if max_overlap > 0:
                new_part_words = curr_words[max_overlap:]
                if new_part_words:
                    merged[-1] += " " + " ".join(new_part_words)
            else:
                # Check for full containment
                curr_str_clean = " ".join(curr_lower)
                prev_str_clean = " ".join(prev_lower)
                if curr_str_clean and curr_str_clean in prev_str_clean:
                    continue
                merged.append(curr)
        return merged

    def process_vtt(self) -> bool:
        print(f">>> [INGEST] Processing {self.machine_code}...")
        
        # Copy source to staging
        shutil.copy2(self.source_path, self.staging_path / "source.vtt")
        
        raw_content = self.source_path.read_text(encoding="utf-8", errors="replace")
        entries = self.parse_vtt_with_timestamps(raw_content)
        
        if not entries:
            print("    Error: No text found in VTT.")
            return False

        # Gap-based sentence reconstruction
        parts = []
        for i in range(len(entries)):
            text = entries[i]["text"]
            if i > 0:
                gap = entries[i]["ts"] - entries[i-1]["ts"]
                if gap > 1.5:
                    if parts and not parts[-1].endswith((".", "!", "?")):
                        parts[-1] += "."
            parts.append(text)
            
        # 1. Deduplicate overlapping text (Industrial Squeeze)
        squeezed_parts = self.merge_overlapping_text(parts)
                
        full_text = " ".join(squeezed_parts)
        full_text = unescape(full_text)
        gen = self.detect_generation(full_text)
        print(f"    Detected Generation: {gen}")
        
        cleaned_text = self.clean_artifacts(full_text)
        
        if gen == 1:
            cleaned_text = self.reconstruct_sentences(cleaned_text)
            
        # 2. Split into preliminary chunks
        if gen == 3:
            raw_chunks = re.split(r">>", cleaned_text)
        else:
            raw_chunks = re.split(r"(?<=[.!?])\s+", cleaned_text)

        # 3. Min-Length Merger (Combine fragments)
        final_chunks = []
        temp_chunk = ""
        for rc in raw_chunks:
            rc = rc.strip()
            if not rc: continue
            
            if temp_chunk:
                combined = temp_chunk + " " + rc
            else:
                combined = rc
                
            if len(combined) < 25 and not combined.endswith((".", "!", "?")):
                temp_chunk = combined
            elif combined.lower().strip(".") in ["mr", "ms", "mrs", "dr", "agenda", "oh", "yes", "no"]:
                temp_chunk = combined
            else:
                final_chunks.append(combined)
                temp_chunk = ""
        
        if temp_chunk:
            final_chunks.append(temp_chunk)

        # 4. Merge micro-fragments into more coherent turns.
        final_chunks = self.merge_short_chunks(final_chunks)

        # Final structured output
        chunks_data = []
        for i, text in enumerate(final_chunks):
            chunks_data.append({
                "chunk_id": i,
                "text": text,
                "has_marker": (gen == 3)
            })

        normalized_data = {
            "machine_code": self.machine_code,
            "generation": gen,
            "ingested_at": datetime.now().isoformat(),
            "chunks": chunks_data
        }
        
        output_file = self.staging_path / "normalized.json"
        output_file.write_text(json.dumps(normalized_data, indent=2), encoding="utf-8")
        print(f"    Saved normalized JSON to staging ({len(chunks_data)} chunks).")
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Path to raw VTT")
    parser.add_argument("--staging", required=True, help="Path to staging root")
    args = parser.parse_args()
    
    ingestor = Ingestor(Path(args.source), Path(args.staging))
    ok = ingestor.process_vtt()
    raise SystemExit(0 if ok else 1)
