# Changelog

## 0.4.0 (unreleased)

- Replaces the command hierarchy with eight commands and numbered sequential sessions; bug/feature/tweak requests defer by default into one concise handoff, with explicit `--local` execution.
- Combines idea/planning into light briefs and two-pass, provisional full plans; removes public frontier, task-ID, queue, release-candidate, and sample commands.
- Preserves local edit/test/commit evidence and optional patches in the session handoff, including failed and interrupted runs; adds cross-process exclusion, scoped cancellation, and safe session recovery/rotation.
- Initializes Git and its first commit automatically when needed, generates only minimal setup, and retains existing configuration/history. Updates installation and validation scripts for the new CLI.

## 0.3.1

- Runs cancellation regression checks through a separate CLI process, preventing the test harness from intercepting the active task's signal.
- Tracks the authoritative version file in `uv` cache inputs so editable-install metadata refreshes after version changes. Workflow behavior is unchanged from 0.3.0.

## 0.3.0

- Adds durable task phases, bounded requests/attempts, cancellation, and inspectable failure artifacts.
- Requires executable planner authority and operation-specific edit schemas; rejects unverified no-change claims and duplicate retries while preserving current edits for repair.
- Adds required-file context manifests, safe path/hash checks, fresh-worktree preparation, and clearer validation failure categories.
- Hardens managed-process ownership, model-switch locking, worktree cleanup, and fast-forward integration.
- Separates local outcomes from frontier resolution, preserves legacy records, and supports partial batch resolution without rewriting history.
- Adds plan-only, explicit budgets, diagnostics, safe cleanup, real capability probes, and clean-source/installed-wheel release gates. Maintenance tasks no longer write tracked per-task handoffs.

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
