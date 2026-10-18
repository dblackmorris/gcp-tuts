#!/usr/bin/env python3
"""Plan or provision GitHub Actions -> Google Cloud Workload Identity Federation."""

import argparse
from dataclasses import dataclass
import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request


@dataclass(frozen=True)
class Config:
    project_id: str
    project_number: str
    repository: str
    repository_id: str
    owner_id: str
    branch: str = "master"
    pool_id: str = "github-oidc-poc"
    provider_id: str = "github"
    service_account_id: str = "github-oidc-poc"
    bucket: str = ""

    @property
    def service_account(self):
        return f"{self.service_account_id}@{self.project_id}.iam.gserviceaccount.com"

    @property
    def provider(self):
        return (
            f"projects/{self.project_number}/locations/global/"
            f"workloadIdentityPools/{self.pool_id}/providers/{self.provider_id}"
        )


def validate(config):
    """Reject malformed identifiers before building shell commands or CEL."""
    for name in ("project_id", "service_account_id"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", getattr(config, name)):
            raise ValueError(f"{name} must contain 6-30 lowercase letters/digits/hyphens")
    for name in ("project_number", "repository_id", "owner_id"):
        if not re.fullmatch(r"[1-9][0-9]*", getattr(config, name)):
            raise ValueError(f"{name} must be a positive numeric ID")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", config.repository):
        raise ValueError("repository must be an owner/repository GitHub name")
    branch = config.branch
    if (
        not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise ValueError("branch must be a simple valid Git branch name")
    for name in ("pool_id", "provider_id"):
        value = getattr(config, name)
        if not re.fullmatch(r"[a-z0-9-]{4,32}", value) or value.startswith("gcp-"):
            raise ValueError(f"{name} must contain 4-32 lowercase letters/digits/hyphens; no gcp- prefix")
    if config.bucket and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", config.bucket):
        raise ValueError("bucket must be a bare Cloud Storage bucket name, without gs:// or a path")


def build_commands(config):
    validate(config)
    common = [f"--project={config.project_id}", "--quiet"]
    pool_flags = ["--location=global", f"--workload-identity-pool={config.pool_id}"]
    ref = f"refs/heads/{config.branch}"
    workflow_ref = f"{config.repository}/.github/workflows/oidc-poc.yml@{ref}"
    condition = " && ".join([
        f"assertion.repository_owner_id == '{config.owner_id}'",
        f"assertion.repository_id == '{config.repository_id}'",
        f"assertion.ref == '{ref}'",
        "assertion.event_name == 'workflow_dispatch'",
        f"assertion.workflow_ref == '{workflow_ref}'",
    ])
    mapping = ",".join([
        "google.subject=assertion.sub",
        "attribute.repository_id=assertion.repository_id",
        "attribute.repository_owner_id=assertion.repository_owner_id",
        "attribute.ref=assertion.ref",
    ])
    member = (
        f"principalSet://iam.googleapis.com/projects/{config.project_number}/"
        f"locations/global/workloadIdentityPools/{config.pool_id}/"
        f"attribute.repository_id/{config.repository_id}"
    )
    services = ["iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com"]
    if config.bucket:
        services.append("storage.googleapis.com")
    commands = [
        ["gcloud", "services", "enable", *services, *common],
        ["gcloud", "iam", "workload-identity-pools", "create", config.pool_id,
         "--location=global", "--display-name=GitHub OIDC POC", *common],
        ["gcloud", "iam", "workload-identity-pools", "providers", "create-oidc", config.provider_id,
         *pool_flags, "--issuer-uri=https://token.actions.githubusercontent.com",
         f"--attribute-mapping={mapping}", f"--attribute-condition={condition}", *common],
        ["gcloud", "iam", "service-accounts", "create", config.service_account_id,
         "--display-name=GitHub OIDC POC", *common],
        ["gcloud", "iam", "service-accounts", "add-iam-policy-binding", config.service_account,
         "--role=roles/iam.workloadIdentityUser", f"--member={member}", "--condition=None", *common],
    ]
    if config.bucket:
        commands.append([
            "gcloud", "storage", "buckets", "add-iam-policy-binding", f"gs://{config.bucket}",
            "--role=roles/storage.objectViewer", f"--member=serviceAccount:{config.service_account}",
            "--condition=None", *common,
        ])
    return commands


def repository_ids(repository):
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be an owner/repository GitHub name")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "gcp-tuts-oidc-poc"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        if data["full_name"].lower() != repository.lower():
            raise ValueError("GitHub redirected to a different repository; check --repository")
        return str(data["id"]), str(data["owner"]["id"])
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as error:
        raise ValueError(
            "Cannot discover GitHub numeric IDs. Supply --repository-id and --owner-id "
            "from trusted GitHub repository metadata (needed for private repositories too)."
        ) from error


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--project-number", help="numeric GCP project number; otherwise queried with gcloud")
    parser.add_argument("--repository", default="dblackmorris/gcp-tuts")
    parser.add_argument("--repository-id", help="stable numeric GitHub repository ID")
    parser.add_argument("--owner-id", help="stable numeric GitHub owner/organization ID")
    parser.add_argument("--branch", default="master")
    parser.add_argument("--pool-id", default="github-oidc-poc")
    parser.add_argument("--provider-id", default="github")
    parser.add_argument("--service-account-id", default="github-oidc-poc")
    parser.add_argument("--bucket", default="", help="optional EXISTING bucket to grant object read/list access")
    parser.add_argument("--apply", action="store_true", help="execute the plan (creates cloud resources and IAM bindings)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        # Validate even unresolved inputs before any discovery network requests.
        unresolved = Config(
            args.project_id, args.project_number or "1", args.repository,
            args.repository_id or "1", args.owner_id or "1", args.branch,
            args.pool_id, args.provider_id, args.service_account_id, args.bucket,
        )
        validate(unresolved)
        repository_id, owner_id = args.repository_id, args.owner_id
        if not repository_id or not owner_id:
            discovered_repository_id, discovered_owner_id = repository_ids(args.repository)
            if repository_id and repository_id != discovered_repository_id:
                raise ValueError("--repository-id disagrees with GitHub metadata")
            if owner_id and owner_id != discovered_owner_id:
                raise ValueError("--owner-id disagrees with GitHub metadata")
            repository_id, owner_id = discovered_repository_id, discovered_owner_id
        project_number = args.project_number
        if not project_number:
            project_number = subprocess.run([
                "gcloud", "projects", "describe", args.project_id,
                "--format=value(projectNumber)", "--quiet",
            ], check=True, capture_output=True, text=True).stdout.strip()
        config = Config(
            args.project_id, project_number, args.repository, repository_id, owner_id,
            args.branch, args.pool_id, args.provider_id, args.service_account_id, args.bucket,
        )
        commands = build_commands(config)
        print("Provisioning plan (create once; existing resource IDs cause an error):", flush=True)
        for command in commands:
            print(shlex.join(command), flush=True)
        if args.apply:
            # Check the ID/number pair before creating a pool with unusable IAM bindings.
            actual_number = subprocess.run([
                "gcloud", "projects", "describe", config.project_id,
                "--format=value(projectNumber)", "--quiet",
            ], check=True, capture_output=True, text=True).stdout.strip()
            if actual_number != config.project_number:
                raise ValueError("--project-number does not belong to --project-id")
            for command in commands:
                subprocess.run(command, check=True)
            print("\nProvisioning completed. Allow several minutes for IAM propagation.")
        else:
            print("\nPlan only. No cloud changes were made. Add --apply to execute.")
        print("\nSet these GitHub Actions repository variables:")
        print(f"GCP_PROJECT_ID={config.project_id}")
        print(f"GCP_WORKLOAD_IDENTITY_PROVIDER={config.provider}")
        print(f"GCP_SERVICE_ACCOUNT={config.service_account}")
        if config.bucket:
            print(f"GCP_POC_BUCKET={config.bucket}")
        return 0
    except (ValueError, OSError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(
            f"gcloud failed (exit {error.returncode}). No further steps were run. "
            "Inspect permissions, API availability, and any partially created resources "
            "before retrying; see oidc/README.md.",
            file=sys.stderr,
        )
        if error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
