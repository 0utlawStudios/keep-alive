"""Execute the actual workflow shell, replacing only curl with a deterministic stub."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/keep-alive.yml"
SCRIPT = textwrap.dedent(WORKFLOW.read_text().split("        run: |\n", 1)[1]
                        .split("\n      - name:", 1)[0])
PRIVATE = "private-fixture-must-not-appear"


class EndpointWorkflowTests(unittest.TestCase):
    def run_workflow(self, targets, code="200", exit_code=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curl = root / "curl"
            curl.write_text('#!/bin/bash\n'
                            'printf "%s\\n" "$@" > "$RUNNER_TEMP/curl-arguments"\n'
                            'printf "%s" "$FIXTURE_CODE"\n'
                            'printf "%s" "$FIXTURE_PRIVATE" >&2\n'
                            'exit "$FIXTURE_EXIT"\n')
            curl.chmod(0o700)
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                   "TARGETS": json.dumps({"targets": targets}), "EXTRA_TARGETS": "",
                   "RUNNER_TEMP": directory, "GITHUB_OUTPUT": str(root / "output"),
                   "FIXTURE_CODE": code, "FIXTURE_EXIT": str(exit_code), "FIXTURE_PRIVATE": PRIVATE}
            result = subprocess.run(["bash", "-c", SCRIPT], env=env, capture_output=True, text=True)
            files = {name: (root / name).read_text() if (root / name).exists() else ""
                     for name in ["output", "keepalive-failures.md", "curl-arguments"]}
            self.assertNotIn(PRIVATE, result.stdout + result.stderr + files["keepalive-failures.md"])
            return result, files

    def target(self):
        return {"name": PRIVATE, "url": "https://example.invalid/" + PRIVATE,
                "headers": {"apikey": PRIVATE}}

    def test_success_keeps_target_details_private_and_disables_redirects(self):
        result, files = self.run_workflow([self.target()])
        self.assertEqual(result.returncode, 0)
        self.assertIn("had_failures=false", files["output"])
        self.assertIn("--proto\n=https\n", files["curl-arguments"])
        self.assertNotIn("--location", files["curl-arguments"])
        self.assertIn("OK   target-1 -> HTTP 200", result.stdout)

    def test_redirects_errors_and_malformed_statuses_fail_closed(self):
        for code, exit_code, expected in [("302", 0, "302"), ("503", 0, "503"),
                                          ("000", 6, "000"), (PRIVATE, 0, "000")]:
            with self.subTest(code=code):
                _, files = self.run_workflow([self.target()], code, exit_code)
                self.assertIn("had_failures=true", files["output"])
                self.assertEqual(files["keepalive-failures.md"], f"- **target-1**: HTTP {expected}\n")

    def test_malformed_secret_values_are_rejected_without_parser_disclosure(self):
        for change in [{"headers": PRIVATE}, {"headers": {"apikey": [PRIVATE]}},
                       {"headers": {"apikey": PRIVATE + "\r\nInjected: yes"}},
                       {"url": "http://example.invalid/" + PRIVATE}, {"method": PRIVATE},
                       {"body": {"secret": PRIVATE}}]:
            with self.subTest(field=next(iter(change))):
                result, files = self.run_workflow([{**self.target(), **change}])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid keep-alive target configuration", result.stdout)
                self.assertEqual(files["curl-arguments"], "")


if __name__ == "__main__":
    unittest.main()
