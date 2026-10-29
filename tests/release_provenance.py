"""Verify a published Conda installation against the artifact tested for release."""

import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sys
from urllib.parse import urlparse

import gwflow
from release_artifacts import stable_tag


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

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
        "workflow_run": "https://github.com/" + os.environ["GITHUB_REPOSITORY"]
                        + "/actions/runs/" + os.environ["GITHUB_RUN_ID"],
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
