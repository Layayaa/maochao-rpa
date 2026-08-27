# Release notes

## v2026.08.27-login-v51

This is the locked production baseline for the Maochao RPA web application.

- Adds separate member and administrator login flows.
- Restricts members to their own runs, files, and supplier assignments.
- Adds operator password initialization, change, and administrator reset support.
- Preserves and migrates existing SQLite schemas at application startup.
- Includes the synchronized supplier-selection workflow (`v51`).
- Includes the production web UI, API, worker, scheduling, and Windows startup scripts.

Production data is intentionally excluded from this repository. Do not commit
`config.local.json`, account databases, encryption keys, browser profiles,
downloads, logs, or generated data.

The canonical production deployment is the locked login/permissions build on
the `.30` server. Older server deployments are retired and must not be used as
data sources.
