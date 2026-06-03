import argparse
import json
import re
from pathlib import Path
from datetime import datetime

# PRATTLE Stage 3: QUOTER
# Mission: Resolve low-confidence attributions using contextual adjacency and keyword scanning.

class Quoter:
    def __init__(self, staging_path: Path):
        self.staging_path = staging_path
        self.attributed_file = staging_path / "attributed.json"
        
        self.turns = []
        self.roster = {}
        self.resolved_count = 0
        
    def load_data(self):
        if not self.attributed_file.exists():
            return False
        data = json.loads(self.attributed_file.read_text(encoding="utf-8"))
        self.turns = data.get("turns", [])
        self.roster = data.get("roster", {})
        self.machine_code = data.get("machine_code")
        return True

    def scan_for_names_in_turn(self, text: str) -> str | None:
        # If the turn itself contains a name that might be the speaker self-identifying
        # or if the previous turn named them.
        
        # Check for explicit self-id that Roberts might have missed
        match = re.search(r"this is ([A-Z][a-z]+)", text)
        if match:
            return match.group(1)
            
        # Check if the turn starts with a known member's name (sometimes the clerk calls it and they just say it)
        # We don't have members populated well yet, so we rely on general patterns.
        return None

    def analyze_adjacency(self, idx: int) -> dict:
        # Look at the previous and next turns for clues
        prev_turn = self.turns[idx - 1] if idx > 0 else None
        next_turn = self.turns[idx + 1] if idx < len(self.turns) - 1 else None
        
        # 1. Mayor Handoff Pattern: "Thank you, [Name]" in the NEXT turn means this turn was [Name]
        if next_turn and next_turn["speaker"]["role"] in ["CHAIR", "MAYOR"]:
            ack_match = re.search(r"thank you\s+([A-Z][a-z]+)", next_turn["text"], re.IGNORECASE)
            if ack_match:
                name = ack_match.group(1)
                return {"role": "MEMBER/CITIZEN", "name": name, "confidence": 0.85, "reason": "chair_acknowledgment"}

        # 2. Mayor Call-On Pattern: "Any comments from [Name]?" in PREV turn
        if prev_turn and prev_turn["speaker"]["role"] in ["CHAIR", "MAYOR"]:
            # Look for questions directed at someone
            if "?" in prev_turn["text"]:
                call_match = re.search(r"([A-Z][a-z]+)\?", prev_turn["text"])
                if call_match:
                    name = call_match.group(1)
                    return {"role": "MEMBER/STAFF", "name": name, "confidence": 0.75, "reason": "chair_call_on"}
                    
            # Look for "motion by X" followed by "second"
            if re.search(r"second", self.turns[idx]["text"], re.IGNORECASE):
                 return {"role": "MEMBER", "name": "SECONDER_UNKNOWN", "confidence": 0.60, "reason": "procedural_second"}

        return None

    def resolve_turn(self, idx: int, turn: dict):
        # Only try to resolve UNKNOWN or low confidence
        if turn["speaker"]["confidence"] >= 0.70 and turn["speaker"]["role"] != "UNKNOWN":
            return turn["speaker"]
            
        text = turn["text"]
        phase = turn["phase"]
        
        # 1. Attempt Adjacency Analysis
        adj_result = self.analyze_adjacency(idx)
        if adj_result:
            self.resolved_count += 1
            return adj_result
            
        # 2. Procedural Keyword Analysis
        # If someone says "second", they are a MEMBER
        if re.search(r"^\s*(i\'ll\s+)?second(s)?\b", text, re.IGNORECASE):
            self.resolved_count += 1
            return {"role": "MEMBER", "name": "SECONDER_UNKNOWN", "confidence": 0.85, "reason": "procedural_keyword"}
            
        # If someone says "so moved" or "make a motion"
        if re.search(r"so moved|make a motion", text, re.IGNORECASE):
            self.resolved_count += 1
            return {"role": "MEMBER", "name": "MOVER_UNKNOWN", "confidence": 0.85, "reason": "procedural_keyword"}

        # 3. Phase Heuristics
        if phase == "PUBLIC_COMMENTS" and len(text.split()) > 20:
            # Long blocks in public comments are almost certainly the citizen
            self.resolved_count += 1
            return {"role": "CITIZEN", "name": "UNKNOWN_CITIZEN", "confidence": 0.70, "reason": "phase_length_heuristic"}
            
        if phase == "ROLL_CALL":
            # Is it a lone vote?
            if re.search(r"^(yes|no|aye|abstain)\.?$", text.strip(), re.IGNORECASE):
                self.resolved_count += 1
                return {"role": "MEMBER", "name": "VOTER_UNKNOWN", "confidence": 0.75, "reason": "roll_call_vote"}
            
            # Is it the Clerk calling a name? (usually a short phrase ending in question mark, or just a single name)
            if len(text.split()) <= 3 and not re.search(r"yes|no|aye|abstain", text, re.IGNORECASE):
                self.resolved_count += 1
                return {"role": "CLERK", "name": "CLERK_INCUMBENT", "confidence": 0.65, "reason": "roll_call_call"}

        # 4. Long speech in specific phases
        if phase in ["REPORTS", "AGENDA_ITEMS"] and len(text.split()) > 100:
            # Very long speeches are usually staff reports or council member monologues
            self.resolved_count += 1
            return {"role": "MEMBER/STAFF", "name": "SPEAKER_UNKNOWN", "confidence": 0.50, "reason": "length_heuristic"}

        # 5. Deterministic phase defaults to avoid persistent UNKNOWN output.
        if phase == "INVOCATION":
            self.resolved_count += 1
            return {"role": "CHAIR/CLERGY", "name": "INVOCATION_SPEAKER", "confidence": 0.70, "reason": "phase_default"}
        if phase == "PLEDGE":
            self.resolved_count += 1
            return {"role": "GROUP", "name": "PLEDGE_GROUP", "confidence": 0.70, "reason": "phase_default"}
        if phase == "PUBLIC_COMMENTS":
            self.resolved_count += 1
            return {"role": "CITIZEN", "name": "PUBLIC_COMMENTER", "confidence": 0.70, "reason": "phase_default"}
        if phase == "MOTION_SEQUENCE":
            self.resolved_count += 1
            return {"role": "MEMBER", "name": "MEMBER_PROCEDURAL", "confidence": 0.70, "reason": "phase_default"}
        if phase in ["AGENDA_AMENDMENTS", "AGENDA_VOTE", "ADJOURNMENT"]:
            self.resolved_count += 1
            return {"role": "CHAIR", "name": "CHAIR_INCUMBENT", "confidence": 0.70, "reason": "phase_default"}

        # If we still can't resolve it, keep it as is
        return turn["speaker"]

    def process(self) -> bool:
        if not self.load_data():
            print(f"Error: {self.attributed_file} not found.")
            return False

        print(f">>> [QUOTER] Scanning {len(self.turns)} turns for low-confidence resolutions...")
        
        for idx, turn in enumerate(self.turns):
            resolved_speaker = self.resolve_turn(idx, turn)
            if resolved_speaker != turn["speaker"]:
                turn["speaker"] = resolved_speaker
                turn["quoter_pass"] = True

        output_data = {
            "machine_code": self.machine_code,
            "processed_at": datetime.now().isoformat(),
            "roster": self.roster,
            "turns": self.turns,
            "quoter_metrics": {
                "resolved_count": self.resolved_count
            }
        }
        
        output_file = self.staging_path / "quoted.json"
        output_file.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
        print(f"    Saved quoted turns to staging. Resolved: {self.resolved_count}")
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, help="Path to staging machine-code folder")
    args = parser.parse_args()
    
    engine = Quoter(Path(args.staging))
    ok = engine.process()
    raise SystemExit(0 if ok else 1)
