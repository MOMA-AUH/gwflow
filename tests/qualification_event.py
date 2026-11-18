"""Validate release events and exact master/package identities before qualification or publication."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tomllib

from select_ci_release import select_run


def check_source(source, tag):
    if not re.fullmatch(r"[0-9a-f]{40}", source) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.-]+)?", tag):
        raise ValueError("Expected a full source SHA and version tag")
    subprocess.run(["git", "fetch", "origin", "master"], check=True)
    subprocess.run(["git", "merge-base", "--is-ancestor", source, "origin/master"], check=True)
    project = subprocess.check_output(["git", "show", f"{source}:pyproject.toml"], text=True)
    version = tomllib.loads(project)["project"]["version"]
    recipe = subprocess.check_output(["git", "show", f"{source}:conda-recipe/meta.yaml"], text=True)
    if tag != "v" + version or f'{{% set version = "{version}" %}}' not in recipe.splitlines():
        raise ValueError("Release tag and package versions do not agree")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("qualify", "publish"))
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source")
    parser.add_argument("--requested-source")
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_text())
        if event["repository"]["full_name"] != args.repo:
            raise ValueError("Event repository does not match")
        if args.mode == "qualify":
            source, tag = args.source, args.tag
            if source != args.requested_source:
                raise ValueError("Requested source differs from selected workflow ref; dispatch a matching ref")
            if "inputs" in event:
                if event["ref"] != "refs/heads/master" and event["ref"] != "refs/tags/" + tag:
                    raise ValueError("Manual qualification requires master or the matching release tag")
            elif event.get("ref") != "refs/tags/" + tag or event.get("deleted", False):
                raise ValueError("Qualification requires a release tag push or manual dispatch")
        else:
            run = event["workflow_run"]
            source, tag = run["head_sha"], run["head_branch"]
            select_run([run], args.repo, source)
            if run["event"] != "push":
                raise ValueError("Manual qualification never authorizes automatic publication")
            actual = subprocess.check_output(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], text=True).strip()
            if actual != source:
                raise ValueError("Release tag no longer points to the qualified source")
        check_source(source, tag)
        if args.output:
            with args.output.open("a") as output:
                output.write(f"source={source}\ntag={tag}\n")
                if args.mode == "publish":
                    output.write(f"run_id={run['id']}\n")
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release event rejected: {error}\n")


if __name__ == "__main__":
    main()
