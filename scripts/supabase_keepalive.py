"""Discover projects and issue a constant, server-enforced read-only SQL query.

Credentials are read only from SUPABASE_KEEPALIVE_ACCOUNTS. Never print API
response bodies, account emails, project names/references, or credential values.
Accounts and tokens appear in logs only as ordinals (account-03 slot-2).
"""
import datetime
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.supabase.com/v1"
REF = re.compile(r"^[a-z]{20}$")
QUERIES_PER_PROJECT = 3
EXPIRY_WARNING_DAYS = 14
TOKEN = re.compile(r"sbp_[A-Za-z0-9_]+")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUS = re.compile(r"^[A-Z_]{1,40}$")


class ApiError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(token, path, payload=None, *, attempts=3):
    """Only the fixed Supabase origin receives the bearer credential."""
    if not path.startswith("/") or "?" in path or ".." in path:
        raise ValueError("Invalid API path")
    opener = urllib.request.build_opener(NoRedirect)
    data = None if payload is None else json.dumps(payload).encode()
    for attempt in range(attempts):
        req = urllib.request.Request(API + path, data=data, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "SupabaseKeepAlive/2.0",
        })
        try:
            with opener.open(req, timeout=35) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise ApiError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if attempt + 1 == attempts:
                raise ApiError("network") from None
        except (json.JSONDecodeError, UnicodeError):
            raise ApiError("invalid-json") from None
        time.sleep(3 * (attempt + 1))


def config_from_json(raw):
    try:
        cfg = json.loads(raw)
        accounts = cfg["accounts"]
        expected = cfg["expected_accounts"]
        excluded = cfg["excluded_refs"]
        if not isinstance(accounts, list) or not accounts:
            raise ValueError()
        if not isinstance(expected, list) or not expected or any(not isinstance(x, str) or "@" not in x for x in expected):
            raise ValueError()
        if len(set(expected)) != len(expected):
            raise ValueError()
        if not isinstance(excluded, list) or any(not isinstance(x, str) or not REF.fullmatch(x) for x in excluded):
            raise ValueError()
        emails = set()
        for account in accounts:
            if not isinstance(account, dict) or not isinstance(account.get("email"), str):
                raise ValueError()
            if account["email"] not in expected or account["email"] in emails:
                raise ValueError()
            account_tokens(account)
            refs = account.get("expected_project_refs")
            if (not isinstance(refs, list) or not refs
                    or any(not isinstance(ref, str) or not REF.fullmatch(ref) for ref in refs)
                    or len(set(refs)) != len(refs)):
                raise ValueError()
            scope = account.get("token_scope", "user")
            if scope not in ("user", "organization"):
                raise ValueError()
            if scope == "organization":
                org_ids = account.get("organization_ids")
                if (not isinstance(org_ids, list) or not org_ids
                        or any(not isinstance(x, str) or not REF.fullmatch(x) for x in org_ids)
                        or len(set(org_ids)) != len(org_ids)):
                    raise ValueError()
            emails.add(account["email"])
        return cfg
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid or empty keep-alive configuration") from None


def account_tokens(account):
    """Normalize the legacy single `token` and the ordered `tokens` fallback list."""
    if ("token" in account) == ("tokens" in account):
        raise ValueError()
    entries = [account["token"]] if "token" in account else account["tokens"]
    if not isinstance(entries, list) or not entries:
        raise ValueError()
    normalized = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"token": entry}
        if not isinstance(entry, dict) or set(entry) - {"token", "expires_at"}:
            raise ValueError()
        token = entry.get("token")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError()
        expires = entry.get("expires_at")
        if expires is not None:
            if not isinstance(expires, str) or not DATE.fullmatch(expires):
                raise ValueError()
            expires = datetime.date.fromisoformat(expires)
        normalized.append({"token": token, "expires_at": expires})
    if len({entry["token"] for entry in normalized}) != len(normalized):
        raise ValueError()
    return normalized


def label(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def run(cfg, api=request, sleep=time.sleep, emit=print, today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    results = []
    tokens_expected = 0
    tokens_verified = 0
    verified_accounts = set()
    seen = set()
    restore_attempted = set()
    pending = {}
    excluded = set(cfg.get("excluded_refs", []))

    def record(kind, ref="", **details):
        results.append({"kind": kind, "ref": ref, **details})
        # Only bounded program-owned enums, ordinals, HTTP codes and opaque
        # hashes enter public logs.
        parts = [kind.upper()]
        if ref:
            parts.append(label(ref))
        if "account" in details:
            parts.append(f"account-{details['account']:02d}")
        if "slot" in details:
            parts.append(f"slot-{details['slot']}")
        if "http" in details:
            parts.append(f"http={details['http']}")
        if "days" in details:
            parts.append(f"days={details['days']}")
        if isinstance(details.get("status"), str) and STATUS.fullmatch(details["status"]):
            parts.append(f"status={details['status']}")
        emit(" ".join(parts))

    def failure_code(error):
        return error.status if isinstance(error, ApiError) else "invalid-response"

    def query(token, ref):
        # Supabase recommends a few user database requests each day. These
        # constant queries create activity without touching application tables.
        for _ in range(QUERIES_PER_PROJECT):
            response = api(token, f"/projects/{ref}/database/query/read-only",
                           {"query": "select 1 as keepalive;"})
            if response != [{"keepalive": 1}]:
                record("query_invalid", ref)
                return
        seen.add(ref)
        pending.pop(ref, None)
        record("healthy", ref)

    for number, account in enumerate(cfg["accounts"], start=1):
        email = account["email"]
        scoped = account.get("token_scope", "user") == "organization"
        expected_orgs = set(account.get("organization_ids") or [])
        token = None
        # Every enrolled token is checked each run, so a dead fallback is
        # reported long before it is needed. The first verified token works.
        for slot, entry in enumerate(account_tokens(account), start=1):
            tokens_expected += 1
            where = {"account": number, "slot": slot}
            if entry["expires_at"] is not None:
                days = (entry["expires_at"] - today).days
                if days <= EXPIRY_WARNING_DAYS:
                    record("token_expiring", days=days, **where)
            try:
                if scoped:
                    # Scoped tokens cannot call /profile. Their email-to-org binding
                    # must be verified in the authenticated dashboard at enrollment.
                    # Revalidate the exact organization scope before any project work.
                    orgs = api(entry["token"], "/organizations")
                    if (not isinstance(orgs, list)
                            or any(not isinstance(o, dict) or not isinstance(o.get("id"), str) for o in orgs)
                            or {o["id"] for o in orgs} != expected_orgs):
                        record("scope_failed", **where)
                        continue
                else:
                    profile = api(entry["token"], "/profile")
                    if not isinstance(profile, dict) or profile.get("primary_email") != email:
                        record("identity_failed", **where)
                        continue
            except ApiError as error:
                record("token_dead" if error.status in (401, 403) else "token_failed",
                       http=error.status, **where)
                continue
            except (ValueError, TypeError):
                record("token_failed", http="invalid-response", **where)
                continue
            tokens_verified += 1
            if token is None:
                token = entry["token"]
        if token is None:
            record("account_failed", account=number)
            continue
        try:
            projects = api(token, "/projects")
            if not isinstance(projects, list):
                record("discovery_failed", account=number)
                continue
            if any(not isinstance(p, dict) or not isinstance(p.get("id"), str)
                   or not REF.fullmatch(p["id"]) or not isinstance(p.get("status"), str)
                   for p in projects):
                record("discovery_failed", account=number)
                continue
            if scoped and any(p.get("organization_id") not in expected_orgs for p in projects):
                record("scope_failed", account=number)
                continue
            verified_accounts.add(email)
            discovered_refs = {project["id"] for project in projects}
            for ref in sorted(set(account["expected_project_refs"]) - discovered_refs):
                record("project_missing", ref, account=number)
        except ApiError as error:
            record("discovery_failed", account=number, http=error.status)
            continue
        except (ValueError, TypeError):
            record("discovery_failed", account=number, http="invalid-response")
            continue
        for project in projects:
            ref = project["id"]
            if ref in seen:
                continue
            if ref in excluded:
                seen.add(ref)
                record("excluded", ref)
                continue
            status = project["status"]
            try:
                if status == "INACTIVE":
                    if ref in restore_attempted:
                        record("restore_pending", ref)
                        continue
                    # Restoration is a mutation: never blindly retry this POST.
                    # Mark before sending so ambiguous network failures cannot
                    # trigger another mutation through a second account.
                    restore_attempted.add(ref)
                    api(token, f"/projects/{ref}/restore", {}, attempts=1)
                    record("restore_requested", ref)
                    status = "COMING_UP"
                if status in ("COMING_UP", "RESTORING"):
                    pending.setdefault(ref, token)
                    continue
                if status != "ACTIVE_HEALTHY":
                    record("unhealthy", ref, status=status)
                    continue
                query(token, ref)
            except (ApiError, ValueError, TypeError) as error:
                record("project_failed", ref, http=failure_code(error))
            sleep(0.5)
    # Every already-healthy project is serviced before restoration polling.
    # One shared deadline bounds all transitional projects, not three minutes each.
    deadline = time.monotonic() + 180
    for _ in range(12):
        if not pending or time.monotonic() + 15 > deadline:
            break
        sleep(15)
        for ref, token in list(pending.items()):
            if time.monotonic() >= deadline:
                break
            try:
                detail = api(token, f"/projects/{ref}", attempts=1)
                if not isinstance(detail, dict) or detail.get("id") != ref:
                    raise ValueError("Invalid project detail")
                status = detail.get("status")
                if status == "ACTIVE_HEALTHY":
                    query(token, ref)
                    pending.pop(ref, None)
                elif status not in ("COMING_UP", "RESTORING"):
                    record("unhealthy", ref, status=status)
                    pending.pop(ref, None)
            except (ApiError, ValueError, TypeError) as error:
                record("project_failed", ref, http=failure_code(error))
                pending.pop(ref, None)
    for ref in pending:
        record("unhealthy", ref, status="restore-verification-timeout")
    missing = set(cfg["expected_accounts"]) - verified_accounts
    if missing:
        record("coverage_incomplete", count=len(missing))
    # A later valid credential may recover a project visible to multiple accounts.
    recovered = {r["ref"] for r in results if r["kind"] == "healthy"}
    failures = [r for r in results if r["kind"] not in ("healthy", "excluded", "restore_requested")
                and not (r["kind"] != "project_missing" and r["ref"] and r["ref"] in recovered)]
    summary = {"accounts_verified": len(verified_accounts), "accounts_expected": len(cfg["expected_accounts"]),
               "tokens_verified": tokens_verified, "tokens_expected": tokens_expected,
               "projects_healthy": len(recovered),
               "projects_excluded": len({r["ref"] for r in results if r["kind"] == "excluded"}),
               "failures": len(failures)}
    emit("SUMMARY " + json.dumps(summary, sort_keys=True))
    return summary, results


def main():
    try:
        cfg = config_from_json(os.environ.get("SUPABASE_KEEPALIVE_ACCOUNTS", ""))
    except ValueError:
        print("CONFIGURATION_FAILED")
        return 1
    summary, _ = run(cfg)
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
