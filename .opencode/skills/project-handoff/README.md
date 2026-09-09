# project-handoff skill — README

## Skill name
`project-handoff`

## Purpose
Maintain `PROGRESS.md` as the authoritative project handoff document for a target
repository. A completely new ChatGPT/OpenCode session, given the latest
`PROGRESS.md`, must immediately understand: what is completed, what is in progress,
what is committed and pushed, the exact current Git checkpoint, important
architecture/design decisions, tests and verification status, known
deviations/risks, the next approved workstream, and what must NOT be started yet.

## Invocation
- Run `/project-handoff [<repo-path>]` in OpenCode, or
- Ask in natural language, e.g. "update progress", "create handoff", "refresh
  PROGRESS.md", "record the current checkpoint".

The optional `<repo-path>` selects the target repository. If omitted, the skill
resolves the target from session context or the repository under discussion; if
ambiguous it asks the user.

## Expected inputs
- Target repository (path or unambiguous session context).
- Existing `PROGRESS.md` (if any) — preserved, not rewritten.
- Repository state: `git status`, branch, HEAD SHA, remote SHA, recent commits, diffs.
- Verified test results (commands that were actually run).
- Explicit user-approved workstream instructions and any in-session implementation
  reports.

## Outputs
- An updated `PROGRESS.md` following the required 9-section structure:
  1. Current Status
  2. Completed Workstreams
  3. Current Architecture
  4. Important Design Decisions
  5. Verification
  6. Known Deviations / Risks
  7. Explicitly Out of Scope
  8. Next Approved Workstream
  9. New-Session Handoff
- A report to the user showing exactly what changed and confirmation that
  `PROGRESS.md` remains untracked/uncommitted.

## Safety rules
- ONLY maintains the handoff document. Never implements application code, modifies
  source/tests/architecture, starts the next workstream, commits, pushes, or
  stages/commits `PROGRESS.md` — unless the user separately and explicitly asks.
- `PROGRESS.md` is a living document, NOT part of immutable code checkpoints:
  update it, but do not `git add` / commit / push it unless explicitly instructed.
- Status is derived from evidence only (git state, existing PROGRESS.md, verified
  tests, approved instructions, session reports). Never guess or fabricate
  percentages, roadmap status, or completion.

## Example usage
```
/project-handoff C:\Vijay_GitHub\fno-ai-paper-trading
```
Expected behavior:
1. Inspects `git status --short`, branch, `HEAD`, `origin/master`, `git log`,
   uncommitted changes in that repo.
2. Reads the existing `PROGRESS.md`.
3. Updates sections 1–9 with exact SHAs/test counts from verified evidence.
4. Shows the diff of what changed in `PROGRESS.md`.
5. Confirms `PROGRESS.md` was not committed/staged/pushed.
6. Leaves `PROGRESS.md` untracked (or uncommitted) in the target repo.

For the fno-ai-paper-trading repository the target path is
`C:\Vijay_GitHub\fno-ai-paper-trading` and the handoff document lives at
`PROGRESS.md` in that root.

## Files
- `SKILL.md` — the skill definition used by OpenCode (frontmatter + workflow).
- `README.md` — this file.