# Changelog

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
