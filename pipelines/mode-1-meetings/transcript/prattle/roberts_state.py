import argparse
import json
import re
from pathlib import Path
from datetime import datetime

# PRATTLE Stage 2: ROBERTS STATE
# Mission: Discover meeting structure and roster entirely from the transcript.
# NO HARDCODING. Pure discovery logic.

class RobertsState:
    def __init__(self, staging_path: Path):
        self.staging_path = staging_path
        self.normalized_file = staging_path / "normalized.json"
        
        # State Management
        self.current_phase = "PRE_GAVEL"
        self.main_phase = "PRE_GAVEL"
        self.phase_history = ["PRE_GAVEL"]
        self.sub_phases = {"MOTION_SEQUENCE", "ROLL_CALL"}
        
        # Discovery Roster
        self.roster = {
            "chair": None,  # Usually Mayor
            "clerk": None,  # The one calling the roll
            "members": {},  # {name: {role: "MEMBER", confidence: 0.0}}
            "others": {}    # {name: {role: "CITIZEN/STAFF", confidence: 0.0}}
        }
        
        # Contextual Memory
        self.last_speaker_name = None
        self.chair_identity_confirmed = False
        
        # Sequence logic: Phases must flow forward in general
        self.phase_order = [
            "PRE_GAVEL", "CALL_TO_ORDER", "INVOCATION", "PLEDGE", 
            "AGENDA_AMENDMENTS", "AGENDA_VOTE", "MINUTES_APPROVAL",
            "PUBLIC_COMMENTS", "AGENDA_ITEMS", "REPORTS", "ADJOURNMENT"
        ]

    def get_phase_index(self, phase: str) -> int:
        try:
            return self.phase_order.index(phase)
        except ValueError:
            return -1

    def detect_phase_transition(self, text: str):
        text_lower = text.lower()
        
        # High-frequency sub-states (can happen anywhere)
        sub_triggers = {
            "MOTION_SEQUENCE": [r"motion to approve", r"motion as amended", r"i make a motion", r"so moved", r"\bi'?ll second\b", r"\bsecond\b"],
            "ROLL_CALL": [r"\broll call vote\b", r"\bplease call the roll\b", r"\bcall the roll\b"]
        }

        # Main Phase Triggers
        main_triggers = {
            "CALL_TO_ORDER": [r"come to order", r"call the meeting to order"],
            "INVOCATION": [r"let us pray", r"invocation", r"heavenly father"],
            "PLEDGE": [r"pledge allegiance", r"to the flag"],
            "AGENDA_AMENDMENTS": [r"additions or deletions", r"amend the agenda"],
            "AGENDA_VOTE": [r"approve the agenda", r"adopt the agenda"],
            "PUBLIC_COMMENTS": [r"public comments", r"come forward", r"state your name"],
            "ADJOURNMENT": [r"adjourned", r"meeting is adjourned", r"motion to adjourn"]
        }

        # 1. Check for forward main-phase transitions.
        current_idx = self.get_phase_index(self.main_phase)
        for phase, patterns in main_triggers.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    new_idx = self.get_phase_index(phase)
                    if new_idx > current_idx: # Strict forward movement for main phases
                        print(f"    [PHASE CHANGE] {self.main_phase} -> {phase}")
                        self.main_phase = phase
                        return phase
        
        # 2. Automatic Exit from PRE_GAVEL
        if self.main_phase == "PRE_GAVEL" and len(text.split()) > 10:
            # If we're seeing long coherent sentences, we've likely started
            # Even if we missed the Call to Order trigger
            self.main_phase = "CALL_TO_ORDER"
            return "CALL_TO_ORDER"

        # 3. Sub-state detection, but do not permanently replace main phase.
        for phase, patterns in sub_triggers.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    return phase

        # 4. If we're currently in a sub-phase and no sub-trigger matched, return to main phase.
        if self.current_phase in self.sub_phases:
            return self.main_phase

        return self.main_phase

    def discover_names(self, text: str, phase: str):
        # Discovery Stop Words (Non-names)
        STOP_WORDS = set(w.lower() for w in [
            "And", "The", "This", "That", "You", "Uh", "Please", "Season", "Work", 
            "Correct", "Yes", "No", "Aye", "Vote", "Abstained", "Five", "Be", 
            "Correct", "Numbering", "It", "Basement", "Say", "Homes", "SE", 
            "Absolutely", "Right", "Here", "They", "Ready", "Says", "Written", 
            "Have", "Hear", "Car", "Reup", "Motion", "Notify", "Can", "Questions", 
            "Get", "Now", "Up", "Else", "Park", "Okay", "Been", "Set", "Us", "Changes", 
            "He", "Time", "Evening", "La", "Valley", "One", "Two", "Three", "Four", "Being", "Said", "Or"
        ])

        # 1. Roll Call Discovery (The Gold Standard)
        if phase == "ROLL_CALL":
            # Pattern: "Name [gap] Yes"
            # Look for capitalized words followed by affirmative
            matches = re.finditer(r"\b([A-Z][a-z]+)\b\.?\s+(Yes|No|Aye|Abstain)\b", text)
            for m in matches:
                name = m.group(1)
                if name.lower() not in STOP_WORDS and len(name) >= 3:
                    if name not in self.roster["members"]:
                        print(f"    [DISCOVERY] New Member found in Roll Call: {name}")
                        self.roster["members"][name] = {"role": "MEMBER", "confidence": 0.90}

        # 2. Self-Identification
        cit_match = re.search(r"my name is ([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text)
        if cit_match:
            name = cit_match.group(1)
            if name not in self.roster["members"]:
                self.roster["others"][name] = {"role": "CITIZEN", "confidence": 0.95}

        # 3. Chair Narration ("X makes the motion")
        mot_match = re.search(r"([A-Z][a-z]+)\s+makes the motion", text)
        if mot_match:
            name = mot_match.group(1)
            if name not in self.roster["members"]:
                print(f"    [DISCOVERY] New Member found via Chair narration: {name}")
                self.roster["members"][name] = {"role": "MEMBER", "confidence": 0.85}

    def attribute_speaker(self, text: str, phase: str):
        text_lower = text.lower()

        # The Chair Frame logic
        if phase == "CALL_TO_ORDER":
            # Whoever calls to order is the CHAIR
            # We don't have their name yet, just their role
            return {"role": "CHAIR", "name": "CHAIR_INCUMBENT", "confidence": 0.95}

        # If it's a procedural command, it's likely the Chair
        chair_patterns = [r"is there a motion", r"any discussion", r"motion carries", r"next item"]
        for p in chair_patterns:
            if re.search(p, text_lower):
                return {"role": "CHAIR", "name": "CHAIR_INCUMBENT", "confidence": 0.85}

        # Check for direct address acknowledgments ("Thank you, X")
        ack_match = re.search(r"thank you\s+([A-Z][a-z]+)", text, re.IGNORECASE)
        if ack_match:
            # The current speaker is likely the CHAIR acknowledging the previous speaker
            return {"role": "CHAIR", "name": "CHAIR_INCUMBENT", "confidence": 0.80}

        # Check roster for known members
        for name in self.roster["members"]:
            if name in text and len(text.split()) < 5: # Short turns like "Yes"
                # This is tricky; usually the clerk calls the name and the member responds.
                # If we're in ROLL_CALL, we can attribute this to the member.
                if phase == "ROLL_CALL":
                    return {"role": "MEMBER", "name": name, "confidence": 0.90}

        # Phase-default fallbacks to reduce UNKNOWN saturation.
        if phase == "INVOCATION":
            return {"role": "CHAIR/CLERGY", "name": "INVOCATION_SPEAKER", "confidence": 0.70}
        if phase == "PLEDGE":
            return {"role": "GROUP", "name": "PLEDGE_GROUP", "confidence": 0.70}
        if phase == "PUBLIC_COMMENTS":
            return {"role": "CITIZEN", "name": "PUBLIC_COMMENTER", "confidence": 0.70}
        if phase == "MOTION_SEQUENCE":
            if re.search(r"\b(second|so moved|motion|amend)\b", text_lower):
                return {"role": "MEMBER", "name": "MEMBER_PROCEDURAL", "confidence": 0.70}
            return {"role": "CHAIR", "name": "CHAIR_INCUMBENT", "confidence": 0.65}
        if phase == "ROLL_CALL":
            if re.search(r"^(yes|no|aye|abstain)\.?$", text.strip(), re.IGNORECASE):
                return {"role": "MEMBER", "name": "VOTER_UNKNOWN", "confidence": 0.80}
            return {"role": "CLERK", "name": "CLERK_INCUMBENT", "confidence": 0.70}
        if phase in {"AGENDA_AMENDMENTS", "AGENDA_VOTE", "ADJOURNMENT"}:
            return {"role": "CHAIR", "name": "CHAIR_INCUMBENT", "confidence": 0.75}

        return {"role": "UNKNOWN", "name": "UNKNOWN", "confidence": 0.10}

    def process(self) -> bool:
        if not self.normalized_file.exists():
            print(f"Error: {self.normalized_file} not found.")
            return False

        data = json.loads(self.normalized_file.read_text(encoding="utf-8"))
        chunks = data.get("chunks", [])
        
        attributed_turns = []
        print(f">>> [ROBERTS_STATE] Discovering roster from {len(chunks)} chunks...")
        
        for chunk in chunks:
            text = chunk["text"]
            
            # 1. Update Phase
            new_phase = self.detect_phase_transition(text)
            self.current_phase = new_phase
            
            # 2. Discover Names (Build Roster)
            self.discover_names(text, self.current_phase)
            
            # 3. Attribute
            speaker_info = self.attribute_speaker(text, self.current_phase)
            
            attributed_turns.append({
                "turn_id": chunk["chunk_id"],
                "phase": self.current_phase,
                "speaker": speaker_info,
                "text": text
            })

        output_data = {
            "machine_code": data["machine_code"],
            "processed_at": datetime.now().isoformat(),
            "roster": self.roster,
            "turns": attributed_turns
        }
        
        output_file = self.staging_path / "attributed.json"
        output_file.write_text(json.dumps(output_data, indent=2), encoding="utf-8")
        print(f"    Saved discovery results to staging.")
        print(f"    Discovered {len(self.roster['members'])} Council Members.")
        return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True, help="Path to staging machine-code folder")
    args = parser.parse_args()
    
    engine = RobertsState(Path(args.staging))
    ok = engine.process()
    raise SystemExit(0 if ok else 1)
