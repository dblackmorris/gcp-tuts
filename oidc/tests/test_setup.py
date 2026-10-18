"""Offline provisioning tests: no GitHub or Google credentials are used."""

import contextlib
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "setup.py"
SPEC = importlib.util.spec_from_file_location("oidc_setup", SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = setup
SPEC.loader.exec_module(setup)

CONFIG = setup.Config(
    project_id="my-gcp-project",
    project_number="123456789012",
    repository="dblackmorris/gcp-tuts",
    repository_id="204901306",
    owner_id="12345678",
)
ARGS = [
    "--project-id", CONFIG.project_id,
    "--project-number", CONFIG.project_number,
    "--repository", CONFIG.repository,
    "--repository-id", CONFIG.repository_id,
    "--owner-id", CONFIG.owner_id,
]


class TrustPolicyTests(unittest.TestCase):
    def test_provider_locks_all_identity_and_workflow_claims(self):
        command = next(c for c in setup.build_commands(CONFIG) if "create-oidc" in c)
        condition = next(a.split("=", 1)[1] for a in command if a.startswith("--attribute-condition="))
        expected = {
            "repository_owner_id": CONFIG.owner_id,
            "repository_id": CONFIG.repository_id,
            "ref": "refs/heads/master",
            "event_name": "workflow_dispatch",
            "workflow_ref": "dblackmorris/gcp-tuts/.github/workflows/oidc-poc.yml@refs/heads/master",
        }
        # Every claim must be required with AND; no disjunctive bypasses.
        self.assertEqual(set(condition.split(" && ")), {
            f"assertion.{claim} == '{value}'" for claim, value in expected.items()
        })
        self.assertIn("--issuer-uri=https://token.actions.githubusercontent.com", command)
        self.assertFalse(any(a.startswith("--allowed-audiences=") for a in command))

    def test_impersonation_is_scoped_to_numeric_repo_in_dedicated_pool(self):
        commands = setup.build_commands(CONFIG)
        iam = next(c for c in commands if "add-iam-policy-binding" in c)
        self.assertIn(CONFIG.service_account, iam)
        self.assertIn("--role=roles/iam.workloadIdentityUser", iam)
        self.assertIn(
            "--member=principalSet://iam.googleapis.com/projects/123456789012/locations/global/"
            "workloadIdentityPools/github-oidc-poc/attribute.repository_id/204901306", iam,
        )
        flattened = " ".join(a for c in commands for a in c)
        self.assertNotIn("roles/owner", flattened)
        self.assertNotIn("roles/editor", flattened)
        self.assertNotIn("roles/iam.serviceAccountTokenCreator", flattened)
        self.assertNotIn("keys", flattened)
        self.assertFalse(any("storage" in c for c in commands))

    def test_optional_bucket_grant_is_bucket_scoped(self):
        command = setup.build_commands(replace(CONFIG, bucket="oidc-test-bucket"))[-1]
        self.assertEqual(command[:6], [
            "gcloud", "storage", "buckets", "add-iam-policy-binding",
            "gs://oidc-test-bucket", "--role=roles/storage.objectViewer",
        ])
        self.assertIn(f"--member=serviceAccount:{CONFIG.service_account}", command)
        self.assertIn("--condition=None", command)

    def test_unsafe_or_invalid_identifiers_are_rejected(self):
        for field, value in [
            ("repository", "owner/repo' || true"),
            ("branch", "master' || true"),
            ("branch", "feature/../master"),
            ("branch", "feature//master"),
            ("branch", "feature.lock/master"),
            ("repository_id", "123/attribute.other/*"),
            ("owner_id", "0"),
            ("project_number", "abc"),
            ("project_id", "bad;echo injected"),
            ("service_account_id", "--malicious-option"),
            ("pool_id", "gcp-reserved"),
            ("provider_id", "bad/path"),
            ("bucket", "gs://bucket"),
        ]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                setup.build_commands(replace(CONFIG, **{field: value}))

    def test_branch_and_repository_changes_update_provider_condition(self):
        config = replace(CONFIG, branch="release/oidc", repository="my-org/my-repo")
        command = next(c for c in setup.build_commands(config) if "create-oidc" in c)
        condition = next(a for a in command if a.startswith("--attribute-condition="))
        self.assertIn("assertion.ref == 'refs/heads/release/oidc'", condition)
        self.assertIn("my-org/my-repo/.github/workflows/oidc-poc.yml@refs/heads/release/oidc", condition)


class ProvisioningIntegrationTests(unittest.TestCase):
    """Run the actual CLI against a recording gcloud stand-in."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fake = self.root / "gcloud"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "with open(os.environ['OIDC_TEST_LOG'], 'a') as out:\n"
            "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('OIDC_TEST_FAIL') in sys.argv[1:]:\n"
            "    print('simulated gcloud failure', file=sys.stderr)\n"
            "    sys.exit(17)\n"
            "if sys.argv[1:3] == ['projects', 'describe']:\n"
            "    print(os.environ.get('OIDC_TEST_PROJECT_NUMBER', '123456789012'))\n"
        )
        fake.chmod(0o755)
        self.log = self.root / "calls.jsonl"
        self.env = dict(os.environ, PATH=f"{self.root}{os.pathsep}{os.environ['PATH']}",
                        OIDC_TEST_LOG=str(self.log))

    def run_script(self, *extra, args=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(ARGS if args is None else args), *extra],
            env=self.env, text=True, capture_output=True, check=False,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_default_plan_makes_no_gcloud_calls_and_preserves_quoting(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        printed_commands = [shlex.split(line) for line in result.stdout.splitlines() if line.startswith("gcloud ")]
        self.assertEqual(printed_commands, setup.build_commands(CONFIG))
        self.assertIn(f"GCP_WORKLOAD_IDENTITY_PROVIDER={CONFIG.provider}", result.stdout)
        self.assertIn("Plan only. No cloud changes were made.", result.stdout)

    def test_apply_checks_project_then_executes_plan_in_order(self):
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0][:3], ["projects", "describe", CONFIG.project_id])
        self.assertEqual(calls[1:], [command[1:] for command in setup.build_commands(CONFIG)])
        self.assertIn("Provisioning completed", result.stdout)

    def test_project_number_mismatch_prevents_mutations(self):
        self.env["OIDC_TEST_PROJECT_NUMBER"] = "999999999999"
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not belong", result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0][:2], ["projects", "describe"])

    def test_failed_creation_stops_before_later_iam_changes(self):
        self.env["OIDC_TEST_FAIL"] = "create-oidc"
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 1)
        self.assertIn("exit 17", result.stderr)
        self.assertEqual(len(self.calls()), 4)  # preflight, API enable, pool, provider
        self.assertFalse(any("service-accounts" in command for command in self.calls()))
        self.assertNotIn("Provisioning completed", result.stdout)

    def test_optional_bucket_executes_only_narrow_storage_grant(self):
        result = self.run_script("--apply", "--bucket", "oidc-test-bucket")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = setup.build_commands(replace(CONFIG, bucket="oidc-test-bucket"))
        self.assertEqual(self.calls()[1:], [command[1:] for command in expected])
        self.assertIn("GCP_POC_BUCKET=oidc-test-bucket", result.stdout)

    def test_missing_project_number_is_discovered_read_only(self):
        arguments = ARGS.copy()
        index = arguments.index("--project-number")
        del arguments[index:index + 2]
        result = self.run_script(args=arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0][:2], ["projects", "describe"])
        self.assertIn(CONFIG.provider, result.stdout)

    def test_bad_input_rejected_before_discovery(self):
        result = self.run_script(args=["--project-id", "invalid;project"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.calls(), [])
        self.assertIn("project_id must", result.stderr)


class DiscoveryTests(unittest.TestCase):
    def test_github_failure_suggests_explicit_ids(self):
        with patch.object(setup.urllib.request, "urlopen", side_effect=setup.urllib.error.URLError("403")):
            with self.assertRaisesRegex(ValueError, "Supply --repository-id and --owner-id"):
                setup.repository_ids("dblackmorris/gcp-tuts")

    def test_redirected_repository_is_rejected(self):
        data = {"full_name": "different/repo", "id": 1, "owner": {"id": 2}}
        with patch.object(setup.urllib.request, "urlopen", return_value=io.StringIO(json.dumps(data))):
            with self.assertRaisesRegex(ValueError, "different repository"):
                setup.repository_ids("dblackmorris/gcp-tuts")

    def test_partial_id_input_must_agree_with_discovery(self):
        with patch.object(setup, "repository_ids", return_value=("999", CONFIG.owner_id)):
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                result = setup.main(["--project-id", CONFIG.project_id,
                                     "--repository-id", CONFIG.repository_id])
        self.assertEqual(result, 1)
        self.assertIn("disagrees", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
