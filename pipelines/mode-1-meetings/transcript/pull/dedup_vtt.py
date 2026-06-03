#!/usr/bin/env python
import argparse
import re
import os
from html import unescape
from pathlib import Path

def strip_timing_tags(text: str) -> str:
    """Remove <00:00:00.000> and <c>...</c> tags, decode HTML entities."""
    text = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', text)
    text = re.sub(r'</?c>', '', text)
    text = unescape(text)
    return text.strip()

def dedup_vtt(vtt_path, output_path):
    """
    Surgical Deduper: Processes a raw VTT file to remove scrolling duplicates.
    Strict Invariant: Stage 02 ONLY. Clean text output only.
    """
    print(f"Deduplicating {vtt_path}...")
    
    with open(vtt_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Split into cue blocks
    raw_blocks = re.split(r'\n\n+', content)
    
    clean_lines = []
    recent_texts = []  # rolling window of last 3 unique lines
    
    for block in raw_blocks:
        lines = block.strip().split('\n')
        if not lines:
            continue
            
        # Find the text lines after the timestamp line
        text_lines = []
        for i, line in enumerate(lines):
            if '-->' in line:
                text_lines = lines[i+1:]
                break
        
        if not text_lines:
            continue
            
        # Clean each text line. The LAST line in a VTT block is always the newest content.
        cleaned_text = strip_timing_tags(text_lines[-1])
        if not cleaned_text or cleaned_text == '\xa0':
            continue
            
        # Deduplication strategy: normalize and check window
        normalized = re.sub(r'[^\w\s]', '', cleaned_text.lower()).strip()
        if normalized in recent_texts:
            continue
            
        clean_lines.append(cleaned_text)
        recent_texts.append(normalized)
        if len(recent_texts) > 5:
            recent_texts.pop(0)

    # Final join and write
    final_text = "\n".join(clean_lines)
    
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(final_text)
        
    print(f"Success: Cleaned text saved to {output_path.name}")
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Richlands VTT Deduplicator (dedup_vtt.py)")
    parser.add_argument("--vtt", required=True, help="Path to raw .vtt file")
    parser.add_argument("--out", required=True, help="Path for clean .txt output")
    args = parser.parse_args()
    
    dedup_vtt(args.vtt, args.out)
