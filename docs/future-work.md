# Future work

Left out of the first version on purpose (decided 2026-11-02). Each item is independent.

- **Rotate the dashboard's own admin password.** Same protocol, but the "device" is the dashboard login itself, so verification must use a second browser session before the first one logs out.
- **Several supermarkets or dashboards.** Add a `site` to device records and lock paths, one browser worker per site, and per-site budgets and rate limits.
- **Login and roles on the Runs page.** Today it is local-only. Add authentication, and require a second person to approve `resolved` decisions.
- **Email or Slack alerts.** Notify on escalation, with a link to the run.
- **Server deployment.** Containerise the dashboard and rotator next to OpenBao, use OpenBao auto-unseal with a KMS, and replace the long-lived rotator token with AppRole.
- **Real process-kill test.** The crash tests raise an exception inside a node. A subprocess test with `SIGKILL` would also cover partial writes to the SQLite checkpoint file.
