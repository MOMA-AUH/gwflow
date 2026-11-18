"""Select a complete release bundle from successful release qualification at an exact commit."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def api(endpoint: str) -> dict:
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def select_run(runs: list[dict], repository: str, source: str) -> dict:
    matches = [run for run in runs if (
        run["status"] == "completed" and run["conclusion"] == "success"
        and (run["event"] == "workflow_dispatch" and (run["head_branch"] == "master" or run["head_branch"].startswith("v"))
             or run["event"] == "push" and run["head_branch"].startswith("v"))
        and run["head_sha"] == source
        and run["repository"]["full_name"] == repository and run["head_repository"]["full_name"] == repository
        and run["path"] == ".github/workflows/release-qualification.yml")]
    if not matches:
        raise ValueError("No successful release qualification for the tagged commit; qualify that exact commit first")
    return max(matches, key=lambda run: run["id"])


def verify_master_source(comparison: dict, source: str) -> None:
    """Require the GitHub comparison to prove exact source ancestry on master."""
    if (comparison["status"] not in {"ahead", "identical"}
            or comparison["base_commit"]["sha"] != source
            or comparison["merge_base_commit"]["sha"] != source):
        raise ValueError("Qualified source does not belong to master")


def select_artifact(artifacts: list[dict]) -> dict:
    matches = [artifact for artifact in artifacts if artifact["name"] == "gwflow-conda-release"]
    if len(matches) != 1 or matches[0]["expired"] or matches[0]["size_in_bytes"] <= 0:
        raise ValueError("Complete CI release bundle is missing or expired; rerun qualification for the tagged commit")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", type=int, help="Require this immutable completed qualification run")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or not re.fullmatch(r"[0-9a-f]{40}", args.source):
        parser.error("Expected repository owner/name and full source commit")
    try:
        query = f"head_sha={args.source}&status=success&per_page=100"
        runs = ([api(f"repos/{args.repo}/actions/runs/{args.run_id}")] if args.run_id is not None else
                api(f"repos/{args.repo}/actions/workflows/release-qualification.yml/runs?{query}")["workflow_runs"])
        selected = select_run(runs, args.repo, args.source)
        if args.run_id is not None and selected["id"] != args.run_id:
            raise ValueError("Qualification response differs from requested run ID")
        verify_master_source(api(f"repos/{args.repo}/compare/{args.source}...master"), args.source)
        artifacts = api(f"repos/{args.repo}/actions/runs/{selected['id']}/artifacts?per_page=100")["artifacts"]
        artifact = select_artifact(artifacts)
        with args.output.open("a") as stream:
            stream.write(f"run_id={selected['id']}\nartifact_id={artifact['id']}\n")
        print(f"Selected successful release qualification run {selected['id']} and release artifact {artifact['id']}")
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"CI release selection rejected: {error}\n")


if __name__ == "__main__":
    main()
