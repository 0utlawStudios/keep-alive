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
  opens or updates a tracking issue, and is visible in the Actions log. The issue
  closes itself on the next fully healthy run.
- **Self-sustaining**: the daily Supabase workflow commits a heartbeat after 25 days
  without repository activity, so GitHub never auto-disables either schedule under
  its 60-day dormancy rule. It is the only job that commits.
- **Least privilege**: workflows default to no permissions. The jobs that read
  secrets get none (endpoint) or read-only contents (Supabase). Separate jobs that
  never see a secret get `issues: write` (alerts) or `contents: write` (heartbeat).

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
  "accounts": [{
    "email": "owner@example.com",
    "tokens": ["sbp_primary", {"token": "sbp_fallback", "expires_at": "2026-12-20"}],
    "expected_project_refs": ["abcdefghijklmnopqrst"]
  }],
  "expected_accounts": ["owner@example.com"],
  "excluded_refs": []
}
```

- Each account holds an ordered `tokens` list (the legacy single `"token"` field is
  still accepted, never both). Every token is verified on every run: user-scoped
  tokens must return a profile matching the configured email, and organization
  tokens must return the exact pinned organization set. The first verified token
  does the account's work, so a dead primary falls back without losing coverage.
- A token that fails logs `TOKEN_DEAD` (HTTP 401/403) or `TOKEN_FAILED`, and an
  optional `expires_at` (UTC date) within 14 days logs `TOKEN_EXPIRING`. Each still
  lets every project receive its queries, then fails the run so the alert fires.
- For an organization-scoped token, first verify the account email and its
  organizations in the authenticated dashboard. Add `"token_scope":"organization"`
  and `"organization_ids":["<verified organization ID>"]` to that account entry.
  These tokens cannot call `/profile`; the runner instead checks the exact
  configured organization set and rejects projects outside it. This verifies the
  resource scope at runtime, not the token owner's email. New organizations need
  explicit enrollment and an updated token; new projects in enrolled organizations
  are discovered automatically.
- Scoped permissions: Organizations Read, Projects (account-wide) Read,
  Organization Settings Read, Database Read, and Project Settings Read/Write
  for restoration. All other capabilities can remain None. Record the expiration
  privately and renew before it expires. Verify against this runner before replacing
  an existing token. Classic tokens have broad account privileges and must remain secret.
- Missing or inaccessible expected accounts fail coverage explicitly. Each account
  pins its known projects in `expected_project_refs`; disappearing projects fail
  coverage rather than silently passing. Newly discovered projects still receive
  queries. Update the baseline deliberately after verified additions or deletions.
- A project reference in `excluded_refs` is never queried or resumed.
- Healthy projects receive three `select 1 as keepalive;` requests per daily run through the server-enforced
  read-only query endpoint. No application tables are read or changed.
- Only `INACTIVE` projects receive a restore request, once per run. Transitional
  projects share a three-minute polling budget after healthy projects are served, and recovery requires a successful
  query. An accepted restore request alone does not count as healthy.
- Supabase enforces capacity and permission rules. The runner never upgrades,
  transfers, deletes, pauses another project, or retries a restore mutation.
- Logs contain opaque project hashes, account and token ordinals in configuration
  order (`account-03 slot-2`), HTTP status codes and aggregate counts, not account
  emails, project names/references, tokens, URLs, or API error bodies. Hashes are the first
  twelve hexadecimal characters of SHA-256(project reference), computed locally
  when an operator needs to map an alert.
- The database job has read-only repository permissions and no pull-request trigger.
  It does not publish issues or upload artifacts containing inventory data.
- A separate alert job opens or comments on the issue "Supabase keep-alive failed"
  with the run link only, and closes it after the next passing run.

An isolated heartbeat job runs even when database checks fail and writes only a
dated heartbeat commit after 25 days without repository activity. Only this
maintenance job receives repository write permission. Actions are pinned to full
commit SHAs and jobs run on `ubuntu-24.04`. No third-party dependencies are
installed by the Supabase job.

The endpoint watchdog accepts only HTTPS 2xx responses and never follows
redirects with credential-bearing headers. Its public logs and failure issues
contain ordinal target labels and HTTP status codes, not configured names, URLs,
headers, response bodies or curl error text.

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
