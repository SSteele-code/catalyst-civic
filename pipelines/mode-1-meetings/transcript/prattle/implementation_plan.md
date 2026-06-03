# PRATTLE — Human Conversation Reconstruction Engine

> **The cornerstone layer between PULL and PARSE in the Catalyst Civic pipeline.**
> Prattle's single mandate: transform raw transcript text into clean, speaker-attributed dialogue turns — chat bubbles — so that downstream layers (PARSE, PROOF) never have to fight conversational noise.

---

## Pipeline Context

```
PULL  →  PRATTLE  →  PARSE  →  PROOF
 │          │          │          │
 │          │          │          └── True ontology layer
 │          │          └── Pre-structures for ontology; flags entities, people
 │          └── Conversation reconstruction (THIS DOCUMENT)
 └── Gets raw transcript from source
```

Prattle does NOT know what a motion means. It does NOT flag ordinance numbers or budget figures. It knows **who said what, in what order**, and it delivers that as clean, attributed turns.

---

## Architecture Overview

Prattle is an **orchestration of four sub-orchestrations**, executed sequentially. Each stage narrows the problem space for the next.

```
┌─────────────────────────────────────────────────┐
│                   PRATTLE                        │
│                                                  │
│  ┌──────────┐   ┌──────────┐   ┌─────────┐   ┌────┐  │
│  │  INGEST  │──▶│ ROBERTS  │──▶│ QUOTER  │──▶│ QA │  │
│  │          │   │  STATE   │   │         │   │    │  │
│  └──────────┘   └──────────┘   └─────────┘   └────┘  │
│                                                  │
│  Raw text in                  Chat bubbles out   │
└─────────────────────────────────────────────────┘
```

| Stage | Job | Input | Output |
|---|---|---|---|
| **INGEST** | Normalize raw text into uniform format | Raw transcript from PULL | Normalized chunks |
| **ROBERTS STATE** | Track meeting phase + attribute speakers | Normalized chunks | Attributed turns + confidence scores |
| **QUOTER** | Resolve low-confidence attributions | Attributed turns with flags | Corrected turns |
| **QA** | Validate, clean text, generate metrics | Corrected turns | Final clean output |

---

## 1. INGEST — The Normalization Orchestration

**Job:** Receive raw transcript, detect its format generation, normalize it into a uniform intermediate representation. Roberts State should never care what year the transcript came from.

### 1.1 Format Detection

The Richlands corpus spans 4+ years. Three distinct transcript generations exist:

| Generation | Era | Characteristics | Example |
|---|---|---|---|
| **Gen 1** | Pre-2023 | All lowercase, no punctuation, no speaker markers | Sep 2022 transcript |
| **Gen 2** | 2023-2024 | Proper casing, punctuation, no speaker markers | Dec 2023, Apr 2024 |
| **Gen 3** | 2025+ | `>>` speaker-change markers, punctuation | Oct 2025, Mar 2026 |

Detection is rule-based:
```
IF text contains ">>" markers     → Gen 3
ELSE IF uppercase_ratio > 0.3 AND has_periods  → Gen 2
ELSE                              → Gen 1
```

### 1.2 Artifact Removal

Strip non-speech content that appears across all generations:
- `[Music]`, `[Applause]`, `[Laughter]` — YouTube auto-caption artifacts
- Duplicate consecutive lines (YouTube captioning glitch)
- Empty lines / whitespace normalization
- Metadata headers if present (timestamps, file paths)

> [!NOTE]
> Artifact removal is **aggressive**. These tokens carry zero speaker or content information. Kill them early.

### 1.3 Sentence Reconstruction (Gen 1)

The hardest normalization step. Gen 1 transcripts are continuous lowercase streams:

```
heavenly father thank you again for the opportunity to be here tonight 
for the members of the council those president and council chambers the 
staff the manager and those who cannot be with us we thank you for their 
thoughts and their wishes for a good meeting
```

Strategy:
- Apply rule-based sentence boundary detection using:
  - Signal phrases that always start sentences ("thank you", "okay", "let us", "we have", "is there")
  - Prosodic patterns (common phrase-final words: "tonight", "meeting", "all", "amen")
  - Maximum sentence length heuristic (cap at ~40 words, split at nearest natural boundary)
- Capitalize sentence starts
- Insert periods at boundaries

> [!IMPORTANT]
> Sentence reconstruction does NOT need to be perfect. It needs to be good enough that Roberts State can find signal phrases and detect phase transitions. Minor boundary errors are acceptable — the text content is preserved regardless.

### 1.4 Marker Extraction (Gen 3)

For `>>` marked transcripts:
- Locate all `>>` positions
- Record them as **explicit speaker-change markers** in chunk metadata
- Strip `>>` from the text body — it's metadata now, not content
- These markers become the **leading indicator** of speaker change in Roberts State (highest confidence tier)

### 1.5 Preliminary Chunking

Break the normalized text into candidate turn blocks:
- **Gen 3:** Split at `>>` markers — each block is one speaker's turn
- **Gen 2:** Split at paragraph boundaries, then at sentence boundaries where speaker-change signals appear
- **Gen 1:** Split at reconstructed sentence boundaries (more granular — Roberts State will merge)

### 1.6 INGEST Output Contract

```json
{
  "transcript_id": "string",
  "generation": 1,
  "chunks": [
    {
      "chunk_id": 1,
      "text": "normalized text, artifacts removed",
      "has_marker": true,
      "source_lines": [1, 3]
    }
  ]
}
```

---

## 2. ROBERTS STATE — The Phase + Attribution Engine

**Job:** Walk the normalized chunks sequentially, tracking what phase of the meeting we're in, and for each chunk, determine who is speaking. This is where Prattle reads conversation like a human.

### How a Human Reads a Transcript

A human who picks up a town council transcript and reads it does the following, unconsciously:

1. **Recognizes the opening** — "This is a meeting being called to order"
2. **Identifies the chair** — "This person is running the show — must be the mayor"
3. **Tracks the flow** — "Now the pledge... now agenda amendments... now public comments..."
4. **Hears name mentions** — "'Seth makes the motion' — so Seth just spoke"
5. **Uses social cues** — "'Thank you, Karen' — Karen just finished"
6. **Applies procedural knowledge** — "'Roll call vote please' — now the clerk calls names"
7. **Uses elimination** — "Three members voted, five on council, the next two must be..."

Roberts State replicates this process with two co-operating state machines:

### 2.1 The Phase Machine

Tracks what part of the meeting we're in. A town council meeting under Robert's Rules is NOT free-form — it's a protocol with a predictable sequence.

#### Phase States

```
PRE_GAVEL                        ← informal chatter before meeting starts
CALL_TO_ORDER                    ← "this meeting will now come to order"
INVOCATION                       ← prayer
PLEDGE                           ← pledge of allegiance
AGENDA_AMENDMENTS                ← "additions or deletions to the agenda"
AGENDA_VOTE                      ← "motion to approve the agenda"
├── MOTION_SEQUENCE              ← sub-state: motion/second/vote
│   └── ROLL_CALL                ← sub-state: clerk calls names
CONSENT_AGENDA                   ← "authorization to pay the bills" / consent items
├── MOTION_SEQUENCE
│   └── ROLL_CALL
MINUTES_APPROVAL                 ← "approve the minutes"
├── MOTION_SEQUENCE
│   └── ROLL_CALL
EXECUTIVE_SESSION_ENTRY          ← "motion to go into executive session"
├── MOTION_SEQUENCE
│   └── ROLL_CALL
EXECUTIVE_SESSION                ← closed session (no transcript content)
EXECUTIVE_SESSION_EXIT           ← "motion to come back into open session"
├── MOTION_SEQUENCE
│   └── ROLL_CALL
EXECUTIVE_SESSION_CERTIFICATION  ← "certify nothing was discussed other than..."
├── MOTION_SEQUENCE
│   └── ROLL_CALL
ACTION_FROM_EXECUTIVE            ← "is there any action council wishes to take"
├── MOTION_SEQUENCE
│   └── ROLL_CALL
SCHEDULED_PUBLIC_COMMENTS        ← "scheduled public comments, five minutes"
UNSCHEDULED_PUBLIC_COMMENTS      ← "unscheduled public comments, three minutes"
AGENDA_ITEMS                     ← the body of the meeting
├── ITEM_DISCUSSION              ← discussion on a specific agenda item
├── MOTION_SEQUENCE
│   └── ROLL_CALL
├── PRESENTATION                 ← guest/staff presentations
├── READING                      ← ordinance/resolution readings
REPORTS                          ← end-of-meeting reports
├── MANAGER_REPORT
├── STAFF_REPORT
├── ATTORNEY_REPORT
├── COUNCIL_COMMENTS
ADJOURNMENT                      ← "meeting is adjourned"
```

#### Phase Transition Triggers

These signal phrases were present in **every single transcript** across the 11 reviewed:

| Trigger Phrase | Transition To |
|---|---|
| `"will now come to order"` | CALL_TO_ORDER |
| `"let us pray"` / `"invocation"` | INVOCATION |
| `"pledge allegiance"` | PLEDGE |
| `"additions or deletions to the agenda"` | AGENDA_AMENDMENTS |
| `"motion to approve"` + `"agenda"` | AGENDA_VOTE |
| `"consent agenda"` / `"pay the bills"` | CONSENT_AGENDA |
| `"minutes"` + `"approve"/"adopt"` | MINUTES_APPROVAL |
| `"executive session"` / `"closed session"` | EXEC_SESSION_ENTRY/EXIT |
| `"certify"` + `"nothing was discussed"` | EXEC_SESSION_CERTIFICATION |
| `"action council wishes to take"` | ACTION_FROM_EXECUTIVE |
| `"public comments"` | PUBLIC_COMMENTS |
| `"roman numeral"` + number / specific item names | AGENDA_ITEMS |
| `"roll call vote"` | ROLL_CALL (sub-state) |
| `"so moved"` / `"make a motion"` / `"I move"` | MOTION_SEQUENCE (sub-state) |
| `"manager's report"` / `"staff report"` | REPORTS |
| `"adjourned"` / `"adjourn"` | ADJOURNMENT |

> [!IMPORTANT]
> These triggers are **not exact string matches**. They're fuzzy keyword patterns — the same phrase might appear as "additions and deletions" vs "additions or deletions" vs "any changes to the agenda." The pattern matcher needs to be tolerant of variation while avoiding false positives.

### 2.2 The Attribution Machine

For each chunk, determines WHO is speaking. The phase constrains the candidates.

#### Phase-Constrained Speaker Rules

| Phase | Who Speaks | Attribution Strategy |
|---|---|---|
| PRE_GAVEL | Anyone | Low confidence, best-effort |
| CALL_TO_ORDER | Mayor | All text → Mayor (0.95) |
| INVOCATION | Mayor or designee | Single speaker, entire block (0.95) |
| PLEDGE | All | Collapse to single turn, speaker = "ALL" |
| AGENDA_AMENDMENTS | Mayor ↔ Council Members | Mayor is the frame; others identified by name mention |
| MOTION_SEQUENCE | Mayor + 2 Council Members | Formulaic: "So moved" = mover, "Second" = seconder, Mayor narrates |
| ROLL_CALL | Clerk + Council Members | Name → response pattern, highly structured |
| PUBLIC_COMMENTS | Mayor + Citizen | Citizen self-identifies; Mayor bookends |
| AGENDA_ITEMS | Anyone | **Hardest phase** — use all signals |
| REPORTS | Staff + Q&A | Long blocks = staff, questions = council |
| ADJOURNMENT | Mayor | All text → Mayor (0.95) |

#### The Mayor Frame Pattern

The single most important pattern for speaker attribution:

**The Mayor is the conversation router.** He opens every phase, calls on speakers, narrates what happened, and closes. His speech wraps around everyone else's like a frame:

```
MAYOR: "Is there a motion to [action]?"
COUNCIL_MEMBER_A: "So moved."  
MAYOR: "[Name] makes the motion. Is there a second?"
COUNCIL_MEMBER_B: "I'll second, Mr. Mayor."
MAYOR: "[Name_B] has the second. Any discussion?"
[silence or discussion]
MAYOR: "Hearing no discussion, roll call vote please."
CLERK: "[Name1]"
MEMBER_1: "Yes."
CLERK: "[Name2]"
MEMBER_2: "Yes."
...
MAYOR: "Motion carries. It is unanimous."
```

**If you can identify the mayor, everything between two mayor turns is someone else.** The mayor's speech has extremely consistent markers:
- "Is there a motion?"
- "Is there a second?"
- "[Name] makes the motion"
- "Any discussion?"
- "Hearing no discussion"
- "Roll call vote, please"
- "Motion carries"
- "Thank you, [Name]"
- "We now have..." / "We are now on..."
- "Roman numeral [N]"

The mayor is identified on first pass — he's the speaker in CALL_TO_ORDER. From then on, his patterns confirm identity throughout.

#### Speaker Change Signals (Ranked by Strength)

| Rank | Signal | Confidence Boost | Example |
|---|---|---|---|
| 1 | `>>` marker (Gen 3) | +0.30 | `>> Yes, Mr. Mayor.` |
| 2 | Mayor narration naming | +0.25 | `"Seth makes the motion"` |
| 3 | Self-identification | +0.25 | `"My name is Karen Patton"` |
| 4 | Phase constraint (single speaker) | +0.25 | Mayor in CALL_TO_ORDER |
| 5 | Procedural formula | +0.20 | `"So moved"` / `"I'll second"` |
| 6 | Direct address with name | +0.20 | `"Thank you, Laura"` / `"Ron, can you..."` |
| 7 | Question → answer boundary | +0.15 | Content shifts from question to response |
| 8 | `"Mr. Mayor"` / role address | +0.15 | Implies speaker is NOT the mayor |
| 9 | Turn-initial hedges | +0.10 | `"Well,"` / `"Okay,"` / `"So,"` at a boundary |
| 10 | Topic/register shift | +0.05 | Technical language after procedural language |

#### Attribution Algorithm (per chunk)

```
FOR each chunk:
  1. Check phase constraints → if single-speaker phase, assign immediately
  2. Check for >> marker → if present, mark speaker change, boost confidence
  3. Check for mayor-narration naming → "X makes the motion" = previous chunk was X
  4. Check for self-identification → "My name is X" = speaker is X
  5. Check for procedural formulas → "So moved" = council member
  6. Check for direct address → "Thank you, X" = X just finished speaking
  7. Check for role address → "Mr. Mayor" = speaker is NOT mayor
  8. Check question-answer boundary → topic shift = likely speaker change
  9. Apply accumulated confidence score
  10. IF confidence >= 0.70 → assign speaker
  11. ELSE → assign best-guess speaker, flag for Quoter
```

#### The Roll Call Sub-Machine

Roll call is the most structured sub-state. Two patterns observed:

**Pattern A — Interleaved (clerk calls, member responds):**
```
CLERK: "Seth"
SETH: "Yes"
CLERK: "Gary"
GARY: "Yes"
```

**Pattern B — Compressed (comma-separated in transcript):**
```
"Seth, yes. Gary, yes. Jordan, yes. Jan, yes. Laura, yes."
```

Detection: When ROLL_CALL sub-state is active, scan for council member names followed by "yes"/"no"/"abstain"/"absent". Both patterns decompose into the same attributed turns.

#### The Public Comment Sub-Machine

Also highly structured:

```
MAYOR: "[Name], please come forward. State your name and address."
CITIZEN_X: "My name is [Name]. I live at [Address]. [Extended testimony...]"
MAYOR: "Thank you, [Name]."
```

Self-identification is nearly universal in public comments. The citizen's opening line names them. The mayor's closing line confirms the name.

### 2.3 The Council Roster

A critical data structure maintained by Roberts State:

As the meeting progresses, Roberts State discovers and builds a **roster** of known speakers:

```json
{
  "mayor":        {"name": "Mayor Curry", "discovered_at": "turn 1"},
  "clerk":        {"name": "Amanda", "discovered_at": "turn N"},
  "town_manager": {"name": "Ron Holt", "discovered_at": "turn N"},
  "town_attorney":{"name": "Mike Thomas", "discovered_at": "turn N"},
  "council": [
    {"name": "Seth White", "seat": 1, "discovered_at": "turn N"},
    {"name": "Gary", "seat": 2, "discovered_at": "turn N"},
    {"name": "Laura", "seat": 3, "discovered_at": "turn N"},
    {"name": "Jordan", "seat": 4, "discovered_at": "turn N"},
    {"name": "Jan", "seat": 5, "discovered_at": "turn N"}
  ]
}
```

The roster populates progressively:
- Mayor is discovered at CALL_TO_ORDER
- Council members are discovered during the first ROLL_CALL (names are called)
- Staff are discovered when the mayor introduces them ("Ron, can you address that?")
- Clerk is discovered from narration ("roll call vote, please, Amanda/Becca")

Once a speaker is on the roster, their name becomes a high-confidence attribution signal for the rest of the meeting.

### 2.4 ROBERTS STATE Output Contract

```json
{
  "transcript_id": "string",
  "roster": {},
  "turns": [
    {
      "turn_id": 1,
      "speaker": {
        "role": "MAYOR",
        "name": "Mayor Curry",
        "roster_id": "mayor_curry"
      },
      "phase": "CALL_TO_ORDER",
      "text": "original text, not yet cleaned",
      "confidence": 0.95,
      "signals_used": ["phase_constraint", "call_to_order_pattern"],
      "source_lines": [1, 5]
    }
  ]
}
```

---

## 3. QUOTER — The Low-Confidence Resolver

**Job:** Find every turn that Roberts State couldn't confidently attribute, and use algorithmic methods to resolve the speaker. This is the "go back and re-read" pass.

### 3.1 Stage 1: SCAN

Walk the Roberts State output. Build a worklist:

```
FOR each turn:
  IF confidence < 0.70 OR speaker.role == "UNKNOWN":
    ADD to worklist with:
      - The turn itself
      - Context window: N turns before and after (default N=5)
      - The meeting phase at that point
      - The roster as it existed at that point
```

The worklist is sorted by position in the transcript, not by confidence. We resolve in order because earlier resolutions provide context for later ones.

### 3.2 Stage 2: SEEK

For each flagged turn, gather evidence from multiple sources:

#### Keyword Scan
Search the flagged text for:
- Names from the roster (first names, last names, titles)
- Role markers ("mayor", "chief", "Mr./Mrs./Ms.")
- Self-referential language ("I make a motion", "my concern is", "I'd like to ask")
- Technical jargon that implies a specific role (legal language → attorney, budget terms → finance director)

#### Adjacency Analysis
Check the surrounding attributed turns:
- **Previous mayor turn** — Did the mayor call on someone by name? ("Ron, can you address that?" → next speaker is Ron)
- **Next mayor turn** — Does the mayor acknowledge someone? ("Thank you, Laura" → Laura just finished)
- **Previous speaker** — If the same speaker is talking before AND after this turn, this might be an interruption
- **Response patterns** — Does this turn directly respond to a question in the previous turn?

#### Phase Constraint Recheck
- In this meeting phase, who is ALLOWED to speak?
- Who has ALREADY spoken in this sequence?
- Who has NOT YET spoken? (elimination)

#### Content Fingerprinting
- Compare the linguistic register of this turn to known speakers:
  - The mayor uses procedural language and acknowledgments
  - Staff use technical/administrative language
  - Citizens use personal testimony language
  - Council members use deliberative language ("I move", "I'd suggest", "my concern")

### 3.3 Stage 3: RESOLVE

For each flagged turn, score all candidate speakers:

```
FOR each candidate_speaker in roster:
  score = base_score
  score += keyword_evidence_weight
  score += adjacency_evidence_weight
  score += phase_constraint_weight
  score += content_fingerprint_weight
  
  IF score > best_score:
    best_candidate = candidate_speaker
    best_score = score

IF best_score > resolution_threshold:
  RETAG turn with best_candidate
  UPDATE confidence score
ELSE:
  KEEP as UNKNOWN
  ATTACH reason_code: "insufficient_evidence" | "ambiguous_candidates" | "no_context"
```

#### Process of Elimination (Special Case)

When the phase constrains speakers to a small set (e.g., 5 council members during roll call):
- If N-1 of N known speakers are assigned to adjacent turns
- The remaining unassigned turn is the Nth speaker
- This is high-confidence resolution (0.85+)

### 3.4 QUOTER Output Contract

Same schema as Roberts State output, but with:
- Updated confidence scores on resolved turns
- `resolution_method` field on previously-flagged turns
- `quoter_pass: true` flag on turns that were modified
- Remaining UNKNOWN turns with reason codes

---

## 4. QA — Quality Assurance Gate

**Job:** Validate the final output, clean the text, and generate quality metrics.

### 4.1 Structural Validation

Rules that must hold true for valid output:

| Rule | Validation |
|---|---|
| Every MOTION_SEQUENCE has a mover | A turn with "so moved" / "motion to" must have a named speaker |
| Every MOTION_SEQUENCE has a seconder | A turn with "second" must have a named speaker |
| Every ROLL_CALL has correct count | Number of yes/no responses should match council size (plus/minus 1 for absences) |
| Roll call names match roster | Names in roll call should be council members from the roster |
| No back-to-back UNKNOWN | Two consecutive UNKNOWN turns likely means Roberts State lost the thread — flag for review |
| Phase sequence is valid | Phases should follow the general Robert's Rules order |
| Public commenters are identified | Every PUBLIC_COMMENT should have a speaker with a name |

### 4.2 Consistency Checks

- Speaker names are consistent throughout (no "Seth" / "Set" / "Said" variants — normalize)
- A speaker doesn't appear in two turns simultaneously (no temporal impossibility)
- Council member count is consistent with known council size for that meeting date

### 4.3 Text Cleaning (Final Pass)

**Now** that attribution is done — the disfluencies are no longer needed for speaker identification. Clean the text:

#### Aggressive Cleaning
- Remove pure filler: `"uh"`, `"um"`, `"uh huh"` when they add nothing
- Fix false starts: `"is there are there any"` → `"are there any"`
- Remove repeated fragments: `"so so if you"` → `"so if you"`
- Remove verbal tics: `"you know"`, `"I mean"` when functioning as filler

#### Preserved
- Meaningful hesitations that convey uncertainty or emphasis
- Colloquial speech patterns that carry tone: `"we've got"`, `"that's fine"`
- Regional dialect: `"y'all"`, `"reckon"`
- Emotional content: `"I guarantee"`, `"it's not fair"`
- Direct quotes and proper nouns — never alter

#### Sentence Repair
- Merge fragmented sentences across line breaks
- Fix capitalization
- Normalize punctuation (one period, not three)

### 4.4 Coverage Metrics

Generate a QA report:

```json
{
  "qa_report": {
    "total_turns": 142,
    "attributed": {
      "high_confidence_90_plus": 118,
      "medium_confidence_70_90": 19,
      "low_confidence_below_70": 3,
      "unknown": 2
    },
    "coverage_rate": 0.986,
    "speakers_discovered": 12,
    "phases_detected": 14,
    "structural_violations": [],
    "consistency_warnings": [],
    "cleaning_applied": {
      "fillers_removed": 347,
      "false_starts_fixed": 23,
      "sentences_merged": 56
    }
  }
}
```

---

## 5. PRATTLE Final Output Contract

What Parse receives:

```json
{
  "meeting_id": "richlands_2025_03_25_special",
  "source_file": "NA--_Richlands_...XDvj6nNspNA.txt",
  "meeting_type": "SPECIAL_CALLED",
  "transcript_generation": 3,

  "roster": {
    "mayor": {"id": "mayor_curry", "name": "Mayor Curry"},
    "clerk": {"id": "clerk_amanda", "name": "Amanda Bea"},
    "town_manager": {"id": "mgr_holt", "name": "Ron Holt"},
    "town_attorney": {"id": "atty_thomas", "name": "Mike Thomas"},
    "council": [
      {"id": "cm_seth", "name": "Seth", "seat": 1},
      {"id": "cm_gary", "name": "Gary", "seat": 2},
      {"id": "cm_jordan", "name": "Jordan", "seat": 3},
      {"id": "cm_jan", "name": "Jan", "seat": 4},
      {"id": "cm_laura", "name": "Laura", "seat": 5}
    ],
    "citizens": [
      {"id": "cit_travis", "name": "Travis"}
    ],
    "staff": [
      {"id": "staff_ronnie", "name": "Ronnie", "role": "Finance Director"}
    ]
  },

  "qa": {
    "total_turns": 142,
    "coverage_rate": 0.986,
    "high_confidence": 118,
    "unknown_count": 2
  },

  "turns": [
    {
      "turn_id": 1,
      "speaker_id": "mayor_curry",
      "speaker_role": "MAYOR",
      "speaker_name": "Mayor Curry",
      "phase": "CALL_TO_ORDER",
      "text": "This special call meeting of the Richlands Town Council will now come to order. Let us stand for the invocation and the pledge.",
      "confidence": 0.95,
      "source_lines": [8, 11]
    },
    {
      "turn_id": 2,
      "speaker_id": "mayor_curry",
      "speaker_role": "MAYOR",
      "speaker_name": "Mayor Curry",
      "phase": "INVOCATION",
      "text": "Thank you. Let us pray. Heavenly Father, thank you for the opportunity to gather tonight to conduct the business of the town of Richlands...",
      "confidence": 0.95,
      "source_lines": [12, 28]
    },
    {
      "turn_id": 3,
      "speaker_id": "mayor_curry",
      "speaker_role": "MAYOR",
      "speaker_name": "Mayor Curry",
      "phase": "AGENDA_AMENDMENTS",
      "text": "Council, we have before us an agenda. Are there any additions or deletions to the agenda, or shall we approve it as presented?",
      "confidence": 0.95,
      "source_lines": [29, 33]
    },
    {
      "turn_id": 4,
      "speaker_id": "cm_gary",
      "speaker_role": "COUNCIL_MEMBER",
      "speaker_name": "Gary",
      "phase": "AGENDA_AMENDMENTS",
      "text": "Mayor, do you have the few that we talked about?",
      "confidence": 0.75,
      "source_lines": [34, 35]
    }
  ]
}
```

---

## 6. Design Principles

### 6.1 How Prattle Reads Like a Human

| Human Reading Process | Prattle Equivalent |
|---|---|
| Pick up the document, orient yourself | **INGEST** — detect format, normalize |
| "Oh, this is a meeting being called to order" | **Phase Machine** — detect CALL_TO_ORDER |
| "This person is running the show — the mayor" | **Attribution Machine** — mayor identified at meeting open |
| "Now they're doing the pledge... now public comments..." | **Phase transitions** — signal phrases trigger state changes |
| "'Seth makes the motion' — Seth just spoke" | **Mayor narration naming** — retroactive attribution |
| "'Thank you, Karen' — Karen just finished" | **Acknowledgment pattern** — name confirms previous speaker |
| "'Roll call vote please' — now the clerk calls names" | **ROLL_CALL sub-state** — constrained speaker set |
| "Three voted, five on council, next two must be..." | **Process of elimination** — mathematical resolution |
| "Wait, who said that? Let me re-read..." | **QUOTER** — second pass on low-confidence turns |
| "Does this make sense?" | **QA** — structural validation |

### 6.2 Modularity

Every component is a discrete, testable unit:
- Phase Machine can be tested independently with just signal phrases
- Attribution Machine can be tested per-phase
- Quoter stages can be tested with synthetic flagged turns
- QA rules can be tested as individual assertions

### 6.3 Progressive Discovery

Prattle doesn't need to know the full roster upfront. It discovers speakers as the meeting unfolds — just like a human listener who walks into a meeting late and gradually figures out who everyone is.

### 6.4 Graceful Degradation

When Prattle can't determine a speaker, it says so honestly:
- Tags the turn as UNKNOWN
- Attaches a reason code
- Does NOT guess randomly

Parse can decide what to do with UNKNOWN turns. Prattle's job is to be truthful about what it knows and doesn't know.

---

## 7. Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Gen 1 transcripts (no punctuation, no markers) will have high UNKNOWN rates | Medium | INGEST sentence reconstruction + QUOTER resolution; accept that oldest transcripts may have 10-15% UNKNOWN |
| AGENDA_ITEMS phase is free-form — hardest for attribution | High | Mayor Frame Pattern is strongest signal here; Quoter handles the rest |
| Signal phrase false positives (e.g., someone says "motion" conversationally) | Medium | Phase Machine uses phrase + context, not phrase alone |
| Council member roster changes across years | Low | Roster is built per-meeting from first roll call |
| Long executive sessions produce gap in transcript | None | EXECUTIVE_SESSION phase simply has no content — Phase Machine bridges it |
| Heated discussions with cross-talk / interruptions | Medium | Quoter adjacency analysis; worst case, tag as UNKNOWN |

---

## 8. Implementation Sequence

> [!NOTE]
> This is the build order when the time comes. Not action items now — just showing the logical dependency chain.

1. **INGEST** — Format detection, Artifact removal, Sentence reconstruction, Marker extraction, Chunking
2. **Phase Machine** — Signal phrase catalog, State transitions, Phase assignment per chunk
3. **Attribution Machine** — Mayor identification, Phase-constrained rules, Confidence scoring
4. **Roll Call Sub-Machine** — Name-response pattern matching
5. **Motion Sub-Machine** — Procedural formula detection
6. **Public Comment Sub-Machine** — Self-identification pattern
7. **Council Roster Builder** — Progressive speaker discovery
8. **Quoter Stage 1: SCAN** — Flag low-confidence turns
9. **Quoter Stage 2: SEEK** — Evidence gathering
10. **Quoter Stage 3: RESOLVE** — Mathematical scoring and re-tagging
11. **QA Structural Validation** — Rule-based assertions
12. **QA Text Cleaning** — Disfluency removal and sentence repair
13. **QA Coverage Metrics** — Report generation
14. **Output Serialization** — Final JSON contract
