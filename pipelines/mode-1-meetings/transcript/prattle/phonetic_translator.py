import argparse
import json
import re
from pathlib import Path
from datetime import datetime

# PRATTLE Lobe 5: PHONETIC TRANSLATOR
# Mission: Correct common municipal and legal phonetic hallucinations from auto-captions.
# Designed for high-volume, long-form speech preservation.

class PhoneticTranslator:
    def __init__(self, staging_path: Path):
        self.staging_path = staging_path
        self.normalized_file = staging_path / "normalized.json"
        
        # The Industrial Translation Dictionary
        # Maps [Pattern] -> [Replacement]
        self.dictionary = {
            r"Gini coefficient": "Virginia Code",
            r"Gini": "Virginia",
            r"Kaneko": "Section",
            r"Rich SS": "Richlands",
            r"Akane": "I can",
            r"Saed": "Seth",
            r"Danan": "Dana",
            r"Rony": "Ronnie",
            r"moticn": "motion",
            r"moti n": "motion",
            r"vceda": "VCEDA",
            r"psa": "PSA",
            r"avwap": "AVWAP",
            r"pca": "PCA",
            r"ordinanc\b": "ordinance",
            r"councilmember": "Council Member"
        }
        
        self.translation_count = 0

    def translate_text(self, text: str) -> str:
        original = text
        for pattern, replacement in self.dictionary.items():
            # Use regex for word boundaries and case-insensitivity where appropriate
            # We use a lambda for replacement to preserve case if possible (simple version)
            new_text, count = re.subn(rf"\b{pattern}\b", replacement, text, flags=re.IGNORECASE)
            text = new_text
            self.translation_count += count
        return text

    def process(self) -> bool:
        if not self.normalized_file.exists():
            print(f"    Error: {self.normalized_file} not found.")
            return False

        data = json.loads(self.normalized_file.read_text(encoding="utf-8"))
        chunks = data.get("chunks", [])
        
        print(f">>> [PHONETIC] Translating {len(chunks)} chunks...")
        
        for chunk in chunks:
            # Process the entire text block, regardless of length (supports soliloquies)
            chunk["text"] = self.translate_text(chunk["text"])

        # Update metadata
        data["phonetic_pass_at"] = datetime.now().isoformat()
        data["translations_performed"] = self.translation_count
        
        # Overwrite normalized.json in staging so Roberts State sees the clean text
        self.normalized_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"    Translation complete. Corrected {self.translation_count} hallucinations.")
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, help="Path to staging machine-code folder")
    args = parser.parse_args()
    
    translator = PhoneticTranslator(Path(args.staging))
    ok = translator.process()
    raise SystemExit(0 if ok else 1)
