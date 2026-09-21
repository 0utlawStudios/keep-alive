# keep-alive

Budget-free keep-alive and uptime watchdog.

This repository runs a scheduled endpoint watchdog and a daily Supabase database
keep-alive. The Supabase workflow discovers projects under each enrolled account,
resumes eligible paused projects, and verifies a constant read-only SQL query.
It lives in a **public** repository on purpose: public repositories get unlimited
GitHub Actions minutes, so this watchdog never consumes the account's private
2,000-minute monthly quota.

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

- Endpoint watchdog: every other day at 12:07 UTC.
- Supabase discovery: daily at 11:17 UTC.
- `workflow_dispatch`: manual run from the Actions tab.

There is no `pull_request` trigger, so forks can never run with these secrets.

## Target Secret Shape

Use this same shape for `KEEPALIVE_TARGETS` and optional `KEEPALIVE_EXTRA_TARGETS`.

```json
{ "targets": [ { "name": "app", "method": "GET", "url": "https://...", "headers": { "Authorization": "Bearer ..." }, "body": "" } ] }
```

## Supabase account discovery

The independent `supabase-keepalive.yml` workflow reads the encrypted repository
secret `SUPABASE_KEEPALIVE_ACCOUNTS`. Existing endpoint and legacy Supabase secrets
are preserved. The old unmerged discovery proposal is not needed by this version.

```json
{
  "accounts": [{"email": "owner@example.com", "token": "sbp_example"}],
  "expected_accounts": ["owner@example.com"],
  "excluded_refs": []
}
```

- Store one usable personal access token per named login. Each account's profile
  must match the configured email before any project operation.
- Prefer scoped tokens where available: profile access, account-wide project
  discovery, Database Read, and Project Settings Read/Write for restoration.
  Verify the token against this runner before replacing an existing token.
  Scoped tokens are being rolled out by Supabase and are not available to every
  account. Classic tokens have broad account privileges and must remain secret.
- Missing or inaccessible expected accounts fail coverage explicitly. An empty
  project list is accepted only after identity and discovery succeed.
- A project reference in `excluded_refs` is never queried or resumed.
- Healthy projects receive `select 1 as keepalive;` through the server-enforced
  read-only query endpoint. No application tables are read or changed.
- Only `INACTIVE` projects receive a restore request, once per run. Transitional
  projects share a three-minute polling budget after healthy projects are served, and recovery requires a successful
  query. An accepted restore request alone does not count as healthy.
- Supabase enforces capacity and permission rules. The runner never upgrades,
  transfers, deletes, pauses another project, or retries a restore mutation.
- Logs contain opaque project hashes and aggregate counts, not account emails,
  project names/references, tokens, URLs, or API error bodies. Hashes are the first
  twelve hexadecimal characters of SHA-256(project reference), computed locally
  when an operator needs to map an alert.
- The workflow has read-only repository permissions and no pull-request trigger.
  It does not publish issues or upload artifacts containing inventory data.

The existing monthly heartbeat keeps repository activity current for both
schedules. No third-party dependencies are installed by the Supabase job.

### Local verification

```sh
python3 -m unittest discover -s tests -v
actionlint
# Supply the secret through your secure environment, never a command argument.
python3 scripts/supabase_keepalive.py
```

The runner uses only the Python standard library. Local results and GitHub runtime
results are separate verification steps. A successful query confirms current
database access, not a guarantee that a free-tier project can never pause.

Sources: [Supabase pausing](https://supabase.com/docs/guides/platform/free-project-pausing),
[personal access tokens](https://supabase.com/docs/guides/platform/personal-access-tokens),
[read-only query endpoint](https://supabase.com/docs/reference/api/v1-read-only-query),
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
