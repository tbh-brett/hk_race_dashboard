# Skill: memory-curator

## Purpose
Keep `/memories/` (user, session, repo) tidy. Distil findings from a long
working session into the right scope, deduplicate against existing notes,
and retire memories that are no longer true.

## Triggers
- "remember this"
- "update memory"
- End of a long session where several insights were uncovered.
- After any post-race run where the going was unusual (rain-affected: Good-to-Yielding, Yielding, Soft, Heavy, Wet) — record the exact going string and any model calibration notes.
- When a memory you read turns out to contradict reality.

## Scope decisions

| Insight type | Scope | Path |
|---|---|---|
| Personal preferences (interpreter path, encoding, OneDrive workaround) | User | `/memories/<topic>.md` |
| In-progress task notes, plans for this conversation only | Session | `/memories/session/<task>.md` |
| Repo conventions, pipeline facts, incident reports, model decisions | Repo | `/memories/repo/<topic>.md` |

## Procedure

1. **List existing memories first** — `memory(command=view, path=/memories/)`
   and the relevant subfolder. NEVER create a duplicate.
2. **Decide scope** using the table above. If the insight is project-specific
   (HKJC, dashboard, model), it is repo memory, not user memory.
3. **Write tersely.** User memory is auto-loaded into every prompt — bullets only.
   Repo memory may be longer (it is loaded on demand).
4. **Update, don't append.** If the existing note is partly wrong, use
   `str_replace` to correct it rather than tacking on a contradiction.
5. **Retire stale notes.** v4.2 removed features (Risk, FormFranking,
   Consistency, PaceMultipliers) — if a memory still references them as
   active, delete that part.

## Cross-check against project-wide facts

Before committing a new memory, verify it does not contradict:
- v4.2 disabled features.
- ESZ = strongest signal (don't write "ESZ is weak").
- Pace prediction floor (~14% label-match).
- Combined strategy > model-only.

If you cannot decide between user vs repo scope, default to **repo** —
it is portable to other workspaces and won't bloat the always-loaded user
context.

## Runtime helper
`agent_skills.curate_session_memory(notes: str, *, scope: str)` proposes a
diff but does not write — the agent (or user, via dashboard button) must
confirm. The dashboard surfaces the proposed diff for a one-click write.
