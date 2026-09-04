# Changelog

## 0.2.1

- Rejects non-escalating planner responses that omit all work units.
- Rejects bug, feature, and tweak plans that contain no edit unit.
- Classifies unchanged failing validation as a validation failure rather than a generic workflow error.
- Adds `--direct`, `--allow`, and `--read` to bypass planner inference for known bounded changes.
- Direct mode retains worktree isolation, worker fallback, tests, reviewer approval, and integration checks.

## 0.2.0

- Distinguishes integrated, deferred, escalated, and externally resolved work.
- Adds `--frontier-only` / `--escalate-now` to bug, feature, and tweak commands.
- Adds `frontier defer`, `frontier status`, `frontier bundle`, `frontier resolve`, and `frontier reopen`.
- Adds `frontier defer-file` for recording several issues without inference.
- Adds `frontier supersede` for closing duplicate or obsolete escalations.
- Adds `frontier sync` for safely adopting external commits by fast-forward.
- Creates project-visible ignored copies of external-review bundles.
- Combines unresolved issues with the latest integration state for batch frontier review.
- Records the integration commit each local task starts from.
- Includes unresolved external tasks in release-candidate bundles.
- Continues independent planned tasks after escalation by default.
- Detects malformed edit units with empty write allowlists and asks the planner to repair its plan before invoking a worker.
- Prevents unverified no-change claims from satisfying edit units while a bug baseline still fails.
- Adds a concise `guide` command and clearer status/result output.

## 0.1.4

- Runs bug baselines before triage.
- Promotes stalled worker units to an alternate configured local model.
- Prioritizes requested implementation files in worker context.
