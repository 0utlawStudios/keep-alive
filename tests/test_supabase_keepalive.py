import importlib.util
import json
import pathlib
import unittest
from unittest import mock
import http.client

spec = importlib.util.spec_from_file_location("keepalive", pathlib.Path(__file__).parents[1] / "scripts/supabase_keepalive.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

REF = "a" * 20
OTHER = "b" * 20
EMAIL = "test@example.invalid"


def config():
    return {"accounts": [{"email": EMAIL, "token": "sbp_fixture", "expected_project_refs": [REF]}],
            "expected_accounts": [EMAIL], "excluded_refs": []}


class KeepAliveTests(unittest.TestCase):
    def test_shared_project_recovery_does_not_erase_account_coverage_failure(self):
        cfg = config()
        cfg["accounts"].append({"email": "second@example.invalid", "token": "sbp_second",
                                "expected_project_refs": [REF]})
        cfg["expected_accounts"].append("second@example.invalid")
        def api(token, path, payload=None, **kwargs):
            if path == "/profile":
                return {"primary_email": EMAIL if token == "sbp_fixture" else "second@example.invalid"}
            if path == "/projects" and token == "sbp_fixture":
                return []
            return self.api(token, path, payload, **kwargs)
        summary, rows = self.execute(cfg, api)
        self.assertEqual(summary["accounts_verified"], 2)
        self.assertEqual(summary["projects_healthy"], 1)
        self.assertEqual(summary["failures"], 1)
        self.assertIn({"kind": "project_missing", "ref": REF}, rows)

    def test_missing_known_project_fails_but_new_projects_still_receive_queries(self):
        def api(token, path, payload=None, **kwargs):
            if path == "/projects":
                return [{"id": OTHER, "status": "ACTIVE_HEALTHY"}]
            return self.api(token, path, payload, **kwargs)
        summary, rows = self.execute(config(), api)
        self.assertEqual(summary["projects_healthy"], 1)
        self.assertGreater(summary["failures"], 0)
        self.assertIn({"kind": "project_missing", "ref": REF}, rows)

    def test_expected_project_refs_are_mandatory_and_valid(self):
        for refs in [None, [], ["../../bad"], [REF, REF], "not-a-list"]:
            cfg = config()
            cfg["accounts"][0]["expected_project_refs"] = refs
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                m.config_from_json(json.dumps(cfg))
        cfg = config()
        del cfg["accounts"][0]["expected_project_refs"]
        with self.assertRaises(ValueError):
            m.config_from_json(json.dumps(cfg))

    def execute(self, cfg, api):
        self.logs = []
        return m.run(cfg, api=api, sleep=lambda _: None, emit=self.logs.append)

    def api(self, token, path, payload=None, **kwargs):
        if path == "/profile":
            return {"primary_email": EMAIL}
        if path == "/projects":
            return [{"id": REF, "status": "ACTIVE_HEALTHY"}]
        if path.endswith("/query/read-only"):
            self.assertEqual(payload, {"query": "select 1 as keepalive;"})
            return [{"keepalive": 1}]
        self.fail("Unexpected API operation: " + path)

    def test_healthy_query_and_private_logs(self):
        summary, rows = self.execute(config(), self.api)
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["projects_healthy"], 1)
        self.assertEqual(rows[0]["kind"], "healthy")
        logs = "\n".join(self.logs)
        for private in [REF, EMAIL, "sbp_fixture"]:
            self.assertNotIn(private, logs)

    def test_missing_accounts_fail_coverage(self):
        cfg = config()
        cfg["expected_accounts"].append("missing@example.invalid")
        summary, _ = self.execute(cfg, self.api)
        self.assertEqual(summary["accounts_verified"], 1)
        self.assertEqual(summary["failures"], 1)

    def test_scoped_token_checks_pinned_org_before_query_without_profile(self):
        cfg = config()
        cfg['accounts'][0].update(token_scope='organization', organization_ids=[OTHER])
        calls = []
        def api(token, path, payload=None, **kwargs):
            calls.append(path)
            self.assertNotEqual(path, '/profile')
            if path == '/organizations':
                return [{'id': OTHER}]
            if path == '/projects':
                return [{'id': REF, 'organization_id': OTHER, 'status': 'ACTIVE_HEALTHY'}]
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(cfg, api)
        self.assertEqual(summary['projects_healthy'], 1)
        self.assertEqual(summary['failures'], 0)
        self.assertEqual(calls[:2], ['/organizations', '/projects'])

    def test_scoped_token_rejects_wrong_org_or_cross_org_project(self):
        for bad_org in [True, False]:
            cfg = config()
            cfg['accounts'][0].update(token_scope='organization', organization_ids=[OTHER])
            def api(token, path, payload=None, **kwargs):
                if path == '/organizations':
                    return [{'id': REF if bad_org else OTHER}]
                if path == '/projects':
                    return [{'id': REF, 'organization_id': REF, 'status': 'INACTIVE'}]
                self.fail('Scoped mismatch must prevent every project operation')
            summary, _ = self.execute(cfg, api)
            self.assertEqual(summary['accounts_verified'], 0)
            self.assertGreater(summary['failures'], 0)

    def test_scoped_config_requires_explicit_nonempty_valid_org_binding(self):
        for org_ids in [None, [], ['../../bad'], [OTHER, OTHER]]:
            cfg = config()
            cfg['accounts'][0].update(token_scope='organization', organization_ids=org_ids)
            with self.assertRaises(ValueError):
                m.config_from_json(json.dumps(cfg))
        cfg = config()
        cfg['accounts'][0]['token_scope'] = 'unexpected'
        with self.assertRaises(ValueError):
            m.config_from_json(json.dumps(cfg))

    def test_exact_exclusion_never_queries_or_restores(self):
        cfg = config()
        cfg["excluded_refs"] = [REF]
        def api(token, path, payload=None, **kwargs):
            if path == "/projects":
                return [{"id": REF, "status": "INACTIVE"}]
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(cfg, api)
        self.assertEqual(summary["projects_excluded"], 1)
        self.assertEqual(summary["failures"], 0)

    def test_identity_mismatch_blocks_all_project_operations(self):
        def api(token, path, payload=None, **kwargs):
            self.assertEqual(path, "/profile")
            return {"primary_email": "someone-else@example.invalid"}
        summary, _ = self.execute(config(), api)
        self.assertEqual(summary["accounts_verified"], 0)
        self.assertGreater(summary["failures"], 0)

    def test_restore_only_inactive_then_verify_sql(self):
        calls = []
        def api(token, path, payload=None, **kwargs):
            calls.append(path)
            if path == "/projects":
                return [{"id": REF, "status": "INACTIVE"}]
            if path.endswith("/restore"):
                self.assertEqual(kwargs["attempts"], 1)
                return None
            if path == f"/projects/{REF}":
                return {"id": REF, "status": "ACTIVE_HEALTHY"}
            return self.api(token, path, payload, **kwargs)
        summary, rows = self.execute(config(), api)
        self.assertEqual(summary["projects_healthy"], 1)
        self.assertEqual([r["kind"] for r in rows], ["restore_requested", "healthy"])
        self.assertLess(calls.index(f"/projects/{REF}/restore"), calls.index(f"/projects/{REF}/database/query/read-only"))

    def test_restore_request_alone_does_not_pass(self):
        def api(token, path, payload=None, **kwargs):
            if path == "/projects":
                return [{"id": REF, "status": "INACTIVE"}]
            if path.endswith("/restore"):
                return None
            if path == f"/projects/{REF}":
                return {"id": REF, "status": "RESTORING"}
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(config(), api)
        self.assertEqual(summary["projects_healthy"], 0)
        self.assertEqual(summary["failures"], 1)

    def test_unexpected_state_never_restores(self):
        def api(token, path, payload=None, **kwargs):
            if path == "/projects":
                return [{"id": REF, "status": "ACTIVE_UNHEALTHY"}]
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(config(), api)
        self.assertEqual(summary["failures"], 1)

    def test_malformed_success_does_not_pass(self):
        def api(token, path, payload=None, **kwargs):
            if path.endswith("/query/read-only"):
                return {"error": "failure despite HTTP success"}
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(config(), api)
        self.assertEqual(summary["projects_healthy"], 0)
        self.assertEqual(summary["failures"], 1)

    def test_project_failure_does_not_prevent_other_queries(self):
        def api(token, path, payload=None, **kwargs):
            if path == "/projects":
                return [{"id": r, "status": "ACTIVE_HEALTHY"} for r in [REF, OTHER]]
            if REF in path:
                raise m.ApiError(403)
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(config(), api)
        self.assertEqual(summary["projects_healthy"], 1)
        self.assertEqual(summary["failures"], 1)

    def test_duplicate_projects_receive_one_batch_of_three_queries(self):
        query_count = 0
        def api(token, path, payload=None, **kwargs):
            nonlocal query_count
            if path == "/projects":
                return [{"id": REF, "status": "ACTIVE_HEALTHY"}] * 2
            if path.endswith("/query/read-only"):
                query_count += 1
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(config(), api)
        self.assertEqual(query_count, 3)
        self.assertEqual(summary["projects_healthy"], 1)

    def test_all_three_daily_queries_must_succeed(self):
        calls = 0
        def api(token, path, payload=None, **kwargs):
            nonlocal calls
            if path.endswith('/query/read-only'):
                calls += 1
                if calls == 3:
                    raise m.ApiError(503)
            return self.api(token, path, payload, **kwargs)
        summary, _ = self.execute(config(), api)
        self.assertEqual(calls, 3)
        self.assertEqual(summary['projects_healthy'], 0)
        self.assertEqual(summary['failures'], 1)

    def test_invalid_configs_fail_without_echoing_input(self):
        for raw in ["", "bad sbp_secret", "{}", '{"accounts": []}', json.dumps({**config(), "excluded_refs": ["../../leak"]})]:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "^Invalid or empty keep-alive configuration$"):
                m.config_from_json(raw)
        self.assertEqual(m.config_from_json(json.dumps(config())), config())

    def test_exclusion_field_is_mandatory(self):
        cfg = config()
        del cfg['excluded_refs']
        with self.assertRaises(ValueError):
            m.config_from_json(json.dumps(cfg))

    def test_ambiguous_restore_is_not_retried_through_another_account(self):
        cfg = config()
        cfg['accounts'].append({'email': 'second@example.invalid', 'token': 'sbp_second', 'expected_project_refs': [REF]})
        cfg['expected_accounts'].append('second@example.invalid')
        restores = []
        def api(token, path, payload=None, **kwargs):
            if path == '/profile':
                return {'primary_email': EMAIL if token == 'sbp_fixture' else 'second@example.invalid'}
            if path == '/projects':
                return [{'id': REF, 'status': 'INACTIVE'}]
            if path.endswith('/restore'):
                restores.append(path)
                raise m.ApiError('network')
            self.fail('Unexpected operation')
        summary, _ = self.execute(cfg, api)
        self.assertEqual(len(restores), 1)
        self.assertGreater(summary['failures'], 0)

    def test_api_credentials_not_forwarded_on_redirect(self):
        self.assertIsNone(m.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.invalid"))

    def test_truncated_response_is_normalized_without_retrying_restore(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b'partial')
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(m.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaises(m.ApiError):
                m.request('sbp_fixture', f'/projects/{REF}/restore', {}, attempts=1)
        self.assertEqual(opener.open.call_count, 1)

    def test_many_restores_share_wait_budget_and_healthy_queries_run_first(self):
        refs = [c * 20 for c in 'cdefghi']
        calls = []
        sleeps = []
        def api(token, path, payload=None, **kwargs):
            calls.append(path)
            if path == '/projects':
                return [{'id': r, 'status': 'RESTORING'} for r in refs] + [{'id': REF, 'status': 'ACTIVE_HEALTHY'}]
            for ref in refs:
                if path == f'/projects/{ref}':
                    return {'id': ref, 'status': 'RESTORING'}
            return self.api(token, path, payload, **kwargs)
        summary, _ = m.run(config(), api=api, sleep=sleeps.append, emit=lambda _: None)
        self.assertLessEqual(sum(sleeps), 181)
        self.assertLess(calls.index(f'/projects/{REF}/database/query/read-only'), calls.index(f'/projects/{refs[0]}'))
        self.assertEqual(summary['projects_healthy'], 1)
        self.assertEqual(summary['failures'], len(refs))


if __name__ == "__main__":
    unittest.main()
