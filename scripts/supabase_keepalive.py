"""Discover projects and issue a constant, server-enforced read-only SQL query.

Credentials are read only from SUPABASE_KEEPALIVE_ACCOUNTS. Never print API
response bodies, account emails, project names/references, or credential values.
"""
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
            if not isinstance(account.get("token"), str) or not re.fullmatch(r"sbp_[A-Za-z0-9_]+", account["token"]):
                raise ValueError()
            emails.add(account["email"])
        return cfg
    except (ValueError, KeyError, TypeError):
        raise ValueError("Invalid or empty keep-alive configuration") from None


def label(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def run(cfg, api=request, sleep=time.sleep, emit=print):
    results = []
    verified_accounts = set()
    seen = set()
    restore_attempted = set()
    pending = {}
    excluded = set(cfg.get("excluded_refs", []))

    def record(kind, ref="", **details):
        results.append({"kind": kind, "ref": ref, **details})
        # Only bounded program-owned enums and opaque hashes enter public logs.
        suffix = " " + label(ref) if ref else ""
        emit(kind.upper() + suffix)

    def query(token, ref):
        response = api(token, f"/projects/{ref}/database/query/read-only",
                       {"query": "select 1 as keepalive;"})
        if response != [{"keepalive": 1}]:
            record("query_invalid", ref)
            return
        seen.add(ref)
        pending.pop(ref, None)
        record("healthy", ref)

    for account in cfg["accounts"]:
        token = account["token"]
        email = account["email"]
        try:
            profile = api(token, "/profile")
            if not isinstance(profile, dict) or profile.get("primary_email") != email:
                record("identity_failed")
                continue
            projects = api(token, "/projects")
            if not isinstance(projects, list):
                record("discovery_failed")
                continue
            if any(not isinstance(p, dict) or not isinstance(p.get("id"), str)
                   or not REF.fullmatch(p["id"]) or not isinstance(p.get("status"), str)
                   for p in projects):
                record("discovery_failed")
                continue
            verified_accounts.add(email)
        except (ApiError, ValueError, TypeError):
            record("account_failed")
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
                record("project_failed", ref, http=error.status if isinstance(error, ApiError) else "invalid-response")
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
                record("project_failed", ref, http=error.status if isinstance(error, ApiError) else "invalid-response")
                pending.pop(ref, None)
    for ref in pending:
        record("unhealthy", ref, status="restore-verification-timeout")
    missing = set(cfg["expected_accounts"]) - verified_accounts
    if missing:
        record("coverage_incomplete", count=len(missing))
    # A later valid credential may recover a project visible to multiple accounts.
    recovered = {r["ref"] for r in results if r["kind"] == "healthy"}
    failures = [r for r in results if r["kind"] not in ("healthy", "excluded", "restore_requested")
                and not (r["ref"] and r["ref"] in recovered)]
    summary = {"accounts_verified": len(verified_accounts), "accounts_expected": len(cfg["expected_accounts"]),
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
