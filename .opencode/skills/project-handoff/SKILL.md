---
name: project-handoff
description: Maintain PROGRESS.md as the authoritative project handoff document. Use when the user runs /project-handoff, or says "update progress", "create handoff", "update the progress document", "refresh PROGRESS.md", "prepare handoff for a new session", or asks to record the current Git checkpoint in PROGRESS.md.
---

# Project Handoff

Maintains `PROGRESS.md` as the single authoritative handoff document for the active
repository, so a completely new ChatGPT/OpenCode session can pick up from the exact
current state: completed work, in-progress work, Git checkpoints, architecture,
design decisions, verification status, risks, the next approved workstream, and what
must NOT be started yet.

## Trigger phrases
- "update progress" / "update PROGRESS.md"
- "create handoff" / "handoff document"
- "refresh the progress document"
- "record the current checkpoint"
- "/project-handoff <repo-path>"

## Target repository

The skill is reusable. Determine which repository to operate on, in order:

1. An explicit path passed with the invocation (e.g. `/project-handoff C:\Vijay_GitHub\fno-ai-paper-trading`).
2. The repository whose `PROGRESS.md` is being discussed in the current session.
3. The repository the user calls "this repository" or the current working directory if it is a git repo.

The skill never guesses — if the target is ambiguous, ask the user before writing anything.

## Workflow

1. **Inspect repository state first** (in the target repo):
   - `git status --short`
   - current branch: `git rev-parse --abbrev-ref HEAD`
   - HEAD SHA: `git rev-parse HEAD`
   - remote state: `git rev-parse origin/<branch>` (if remote exists)
   - recent commits: `git log --oneline -10`
   - relevant uncommitted/untracked changes and diffs

2. **Read the existing `PROGRESS.md`** if it exists. Preserve historical checkpoint
   information; do not silently rewrite it.

3. **Derive status only from evidence**, never from guesswork:
   - repository state (git status, log, diff)
   - existing PROGRESS.md content
   - verified test results (actually run, not assumed)
   - explicit user-approved workstream instructions
   - implementation reports available in the current session

4. **Update `PROGRESS.md`** using the required structure below. Update existing
   sections in place rather than creating contradictory duplicates.

## Required PROGRESS.md structure

### 1. Current Status
- Current phase
- Current workstream
- Overall MVP/project status if explicitly known
- Current checkpoint SHA
- Remote synchronization status (HEAD vs origin/<branch>)

### 2. Completed Workstreams
For each completed workstream:
- Workstream ID
- Name
- Status
- Commit SHA
- Commit message
- Tests
- Key implementation points

Keep historical entries concise.

### 3. Current Architecture
Capture only architecture that matters for continuing development:
- major services/components
- important data flow
- authoritative sources of truth
- important runtime boundaries
- important accounting/risk rules
- deterministic vs runtime behavior where relevant

Do not rewrite the entire codebase.

### 4. Important Design Decisions
Record durable decisions future sessions must respect. Examples:
- single authoritative implementation
- accounting authority
- risk-control behavior
- order lifecycle rules
- execution/slippage rules
- completed-candle requirements
- safety constraints

### 5. Verification
- latest full test count
- latest test command
- pass/fail result
- skipped tests and why, if relevant
- Git verification
- HEAD == origin status

Never claim tests passed unless actually verified.

### 6. Known Deviations / Risks
Only verified or explicitly reported items.

### 7. Explicitly Out of Scope
Work that must NOT be accidentally implemented as part of the current checkpoint.
This section prevents scope creep.

### 8. Next Approved Workstream
Only when it has been explicitly approved. If not approved, write exactly:

> Next workstream requires review/approval.

Never begin it automatically.

### 9. New-Session Handoff
A short operational summary a new session can use immediately. Answer:
- Where are we?
- What was last completed?
- What is the latest commit?
- Are tests passing?
- Is the tree clean?
- What should we review next?
- What must not be changed yet?

## Writing rules
- Keep facts precise; prefer concise bullets and tables.
- Use exact commit SHAs and exact test counts.
- Clearly distinguish committed, uncommitted, and untracked files.
- Never fabricate percentages or roadmap status.
- Never claim a workstream is complete without evidence.
- Never silently change historical checkpoint information; preserve prior decisions.
- Update existing sections instead of adding contradictory duplicate ones.

## Safety / scope rules — MUST NOT
- Implement application code
- Modify source code
- Modify tests
- Change architecture
- Start the next workstream
- Commit changes
- Push changes
- Stage or commit `PROGRESS.md`

...unless the user explicitly gives a separate instruction to do so.
This skill ONLY maintains the project handoff document.

## End-of-workstream checklist
At the end of every approved workstream:
1. Verify implementation/checkpoint state.
2. Verify tests.
3. Verify Git state.
4. Update `PROGRESS.md`.
5. Show the exact changes made to `PROGRESS.md`.
6. Confirm `PROGRESS.md` remains untracked/uncommitted unless explicitly instructed otherwise.

## Notes
- `PROGRESS.md` is a living handoff document, NOT part of immutable code checkpoints.
- Unless explicitly instructed otherwise: update `PROGRESS.md`, do **NOT** `git add`
  it, do **NOT** commit it, do **NOT** push it.
- Some skills ship a `scripts/` folder; this skill needs none — it is pure read + document editing.