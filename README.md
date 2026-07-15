# keep-alive

Budget-free keep-alive and uptime watchdog.

This repository runs scheduled GitHub Actions workflows that keep free-tier backends
warm and alert on downtime. It lives in a **public** repository on purpose: public
repositories get unlimited GitHub Actions minutes, so this watchdog never consumes the
account's private 2,000-minute monthly quota.

Two workflows run independently:

- **`keep-alive.yml`** pings a manual static list of health endpoints (`KEEPALIVE_TARGETS`).
  Use it for non-Supabase uptime checks (Vercel apps, external APIs).
- **`supabase-keepalive.yml`** auto-discovers and keeps alive **every** Supabase project
  across **every** account, including future ones, with no per-project setup.

## Design

- **Targets and credentials** are read from encrypted Actions secrets. `KEEPALIVE_TARGETS`
  is the base list, and optional `KEEPALIVE_EXTRA_TARGETS` entries are appended at
  runtime. This repository contains no URLs, keys, or tokens.
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

## Target Secret Shape

Use this same shape for `KEEPALIVE_TARGETS` and optional `KEEPALIVE_EXTRA_TARGETS`.

```json
{ "targets": [ { "name": "app", "method": "GET", "url": "https://...", "headers": { "Authorization": "Bearer ..." }, "body": "" } ] }
```

## Supabase Auto-Discovery

`supabase-keepalive.yml` needs no target list. It reads one secret, `SUPABASE_ACCESS_TOKENS`,
holding one Supabase personal access token per **login** (not per project):

```json
{ "tokens": ["sbp_xxx", "sbp_yyy"] }
```

Each run, for every token, it calls the Supabase Management API to list that account's
projects, then:

- **Active projects** get a real `select 1` database query. That is genuine database
  activity, so it resets Supabase's 7-day inactivity pause timer. It works on any
  project without needing the project's anon key or table names.
- **Paused projects** (`INACTIVE` / `PAUSED`) get an auto-restore request, then are kept
  alive normally on the following run. If restore is rejected (HTTP 403), that is usually
  the free-tier limit of **2 active projects per organization**; the run opens a tracking
  issue naming the project so you can pause/upgrade another project or move it to another
  org. Keep-alive prevents inactivity pausing, but it cannot exceed that per-org active cap.

**Why this covers future builds.** A new project created under any listed account shows up
in the next project list automatically and gets pinged. No secret edit, no code change.
The only manual action ever again is adding a token when you create a brand-new Supabase
**account**, which is rare.

**Token sensitivity.** Supabase personal access tokens are **account-admin scoped** (they
cannot be scoped per project). Generate **dedicated, revocable** tokens named `keep-alive`
at supabase.com/dashboard/account/tokens, and do not reuse a token that also lives in an
at-risk password vault. Secrets are never exposed to forks (no `pull_request` trigger).
