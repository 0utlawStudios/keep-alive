# keep-alive

Budget-free keep-alive and uptime watchdog.

This repository runs a single scheduled GitHub Actions workflow that pings a set of
health endpoints once a day to keep free-tier backends warm and to alert on downtime.
It lives in a **public** repository on purpose: public repositories get unlimited
GitHub Actions minutes, so this watchdog never consumes the account's private
2,000-minute monthly quota.

## Design

- **Targets and credentials** are read from the encrypted `KEEPALIVE_TARGETS` Actions
  secret. This repository contains no URLs, keys, or tokens.
- **Hardened**: each target is retried with backoff; a failed ping fails the run,
  opens or updates a tracking issue, and is visible in the Actions log.
- **Self-sustaining**: a monthly heartbeat commit keeps the schedule from being
  auto-disabled after 60 days of repository dormancy.
- **Least privilege**: the workflow can only write repository contents (heartbeat)
  and issues (alerts).

## Triggers

- `schedule`: daily.
- `workflow_dispatch`: manual run from the Actions tab.

There is no `pull_request` trigger, so forks can never run with these secrets.

## KEEPALIVE_TARGETS shape

```json
{ "targets": [ { "name": "app", "method": "GET", "url": "https://...", "headers": { "Authorization": "Bearer ..." }, "body": "" } ] }
```
