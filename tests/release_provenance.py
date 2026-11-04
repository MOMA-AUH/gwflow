"""Verify a published Conda installation against the artifact tested for release."""

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import sys
from urllib.parse import urlparse

from release_artifacts import stable_tag


def identity_arguments(parser):
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source-ci-run", required=True, help="Run ID of the selected source CI bundle")
    parser.add_argument("--local", action="store_true", help="Record a local verification attempt, without an Actions run")


def publication_identity(args):
    """Validate declared provenance before any publication side effects."""
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or not re.fullmatch(r"[0-9a-f]{40}", args.source):
        raise ValueError("Expected repository owner/name and full source commit")
    if not re.fullmatch(r"[1-9][0-9]*", args.source_ci_run):
        raise ValueError("Source CI run must be a positive numeric run ID")
    workflow_run = None
    if not args.local:
        if os.environ.get("GITHUB_REPOSITORY") != args.repo:
            raise ValueError("Actions GITHUB_REPOSITORY must match --repo; use --local for local verification")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not re.fullmatch(r"[1-9][0-9]*", run_id):
            raise ValueError("Actions GITHUB_RUN_ID must be a positive numeric run ID; use --local for local verification")
        workflow_run = f"https://github.com/{args.repo}/actions/runs/{run_id}"
    return {"verification_context": "local" if args.local else "github-actions",
            "workflow_run": workflow_run,
            "source_ci_run": f"https://github.com/{args.repo}/actions/runs/{args.source_ci_run}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    identity_arguments(parser)
    args = parser.parse_args()
    try:
        identity = publication_identity(args)
    except ValueError as error:
        parser.error(str(error))

    import gwflow

    installed_version = version("gwflow")
    if args.tag != "v" + installed_version:
        parser.error("Release tag does not match the installed gwflow version")
    if not Path(gwflow.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        parser.error("gwflow must be imported from the fresh published-package environment")
    records = list((Path(sys.prefix) / "conda-meta").glob(f"gwflow-{installed_version}-*.json"))
    artifacts = list(args.artifacts.glob(f"gwflow-{installed_version}-*.conda"))
    if len(records) != 1 or len(artifacts) != 1:
        parser.error("Expected exactly one installed gwflow record and one tested artifact")
    package = json.loads(records[0].read_text())
    artifact = artifacts[0]
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if package.get("sha256") != digest or package.get("fn") != artifact.name:
        parser.error("Published installation does not match the tested artifact filename and SHA256")
    url = package.get("url", "")
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc.casefold() != "conda.anaconda.org"
            or parsed.path.casefold() != f"/moma-auh/noarch/{artifact.name}".casefold()):
        parser.error("Published installation must come from the MOMA-AUH noarch channel")

    record = {
        "release": args.tag,
        "source_commit": args.source,
        **identity,
        "artifact": {"filename": artifact.name, "sha256": digest,
                     "size_bytes": artifact.stat().st_size, "url": url,
                     "build": package["build"]},
        "versions": {"python": platform.python_version(),
                     **{name: version(name) for name in ("gwflow", "gwf", "rich", "textual")}},
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "verification": "Fresh channel installation matches the tested artifact; release smoke checks passed.",
        "scope": ("Stable package provenance; baseline selection also requires qualified attachments and a published GitHub release."
                  if stable_tag(args.tag) else
                  "Pre-1.0 or prerelease publication provenance; not a preserved stable compatibility baseline."),
    }
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
