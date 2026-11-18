"""Conservative PR change classification and required-check result validation."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


# Deliberately exact: new documentation paths require review before opting in.
PROSE_PATHS = frozenset({"README.md", "CHANGELOG.md", "docs/authoring.md", "docs/operating.md",
                         "docs/reference.md", "docs/history.md"})


def git(source, *arguments):
    return subprocess.check_output(["git", "-C", str(source), *arguments], stderr=subprocess.PIPE)


def classify(source, base, head):
    base = git(source, "merge-base", base, head).decode().strip()
    head = git(source, "rev-parse", "--verify", head + "^{commit}").decode().strip()
    fields = git(source, "diff", "--raw", "--no-abbrev", "--find-renames", "-z", base, head).split(b"\0")
    paths = []
    prose = bool(fields[0])
    while fields[0]:
        header = fields.pop(0).decode("ascii")
        match = re.fullmatch(r":([0-7]{6}) ([0-7]{6}) [0-9a-f]+ [0-9a-f]+ ([A-Z])([0-9]*)", header)
        if match is None:
            raise ValueError("unrecognized git change record")
        old_mode, new_mode, status, score = match.groups()
        count = 2 if status in ("R", "C") else 1
        if len(fields) < count + 1:
            raise ValueError("incomplete git change record")
        changed = [fields.pop(0).decode("utf-8") for _ in range(count)]
        paths.extend(changed)
        prose = prose and status in {"A", "D", "M", "R", "C"} and old_mode in {"000000", "100644"} and new_mode in {"000000", "100644"}
        prose = prose and all(path in PROSE_PATHS for path in changed)
    if fields != [b""]:
        raise ValueError("unexpected trailing git change fields")
    return {"mode": "docs" if prose else "full", "base": base, "head": head, "paths": sorted(set(paths))}


def require_results(mode, needs):
    if mode not in {"docs", "full"}:
        raise ValueError("classification must select docs or full")
    expected = {"classification": "success", "documentation": "success" if mode == "docs" else "skipped",
                "qualification": "success" if mode == "full" else "skipped"}
    actual = {name: job.get("result") for name, job in needs.items()}
    if actual != expected:
        raise ValueError(f"selected CI results differ: expected {expected}, got {actual}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("classify")
    command.add_argument("--source", type=Path, default=Path.cwd())
    command.add_argument("--base", required=True)
    command.add_argument("--head", required=True)
    command.add_argument("--github-output", type=Path)
    command = commands.add_parser("gate")
    command.add_argument("--mode", required=True)
    command.add_argument("--needs", required=True)
    args = parser.parse_args()
    try:
        if args.command == "gate":
            require_results(args.mode, json.loads(args.needs))
            print("All selected checks passed")
            return
        result = classify(args.source, args.base, args.head)
        if args.github_output:
            with args.github_output.open("a") as stream:
                stream.write("mode=" + result["mode"] + "\n")
        print(json.dumps(result, sort_keys=True))
    except (ValueError, OSError, UnicodeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"CI change validation failed: {error}\n")


if __name__ == "__main__":
    main()
