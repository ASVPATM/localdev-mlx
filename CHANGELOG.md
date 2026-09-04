# Changelog

## 0.1.4

- Run bug baseline validation before planner triage and include the actual failures in the planner prompt.
- Require planner coverage of every explicit defect and distinct baseline failure.
- Prioritize requested implementation files over stable documentation in worker and reviewer context budgets.
- Promote a stalled or invalid primary worker response to the alternate configured planner/reviewer model before external escalation.
- Avoid repeating identical no-edit attempts with the same weaker worker when a local promotion path exists.

## 0.1.3

- Keep read-only or no-change worker units from aborting a task before later edit units run.
- Guide planners to place reproduction and inspection steps in the reproduction plan, or mark them as analysis units.
- Allow cumulative multi-unit bug repairs when the baseline suite already fails.
- Run a final quick validation after all work units before local review.

## 0.1.2

- Make the bundled sample and mock pipeline independent of the caller's `PYTHONPATH`.
- Add repeatable `--test-env KEY=VALUE` support to project initialization.
- Fix import formatting reported by Ruff.

## 0.1.1

- Fix MLX server cleanup on macOS by signaling the managed server PID instead of assuming it is a process-group ID.
- Treat a closed server socket as successful shutdown even when the child briefly remains as a zombie process.
- Avoid signaling stale PIDs when the managed port is already closed.
- Prevent automatic cleanup errors from masking a successful model probe or workflow result.

## 0.1.0

- Initial public release.
