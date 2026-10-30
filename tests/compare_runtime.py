"""Run installed upgrade journeys against an exactly restored baseline runtime."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from compatibility_environment import installed_environment
from conda_runtime import (digest, fixture_inventory, identities, installed_inventory,
                           preserved_file, restore, runtime_environment, runtime_files)
from release_artifacts import verify as verify_release


SOURCE = Path(__file__).resolve().parents[1]


def retain_capture(bundle: Path, record: dict, destination: Path) -> None:
    destination.mkdir()
    for field in ("artifact", "manifest"):
        path = preserved_file(bundle, record[field], field)
        shutil.copyfile(path, destination / path.name)
        preserved_file(destination, record[field], field)
    (destination / "runtime.json").write_text(json.dumps(record, indent=2) + "\n")


def normalized(value, roots: dict[str, str]):
    if isinstance(value, dict):
        return {normalized(key, roots): normalized(item, roots) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item, roots) for item in value]
    if isinstance(value, str):
        for path, placeholder in sorted(roots.items(), key=lambda item: len(item[0]), reverse=True):
            value = value.replace(path, placeholder)
        if value.startswith("file:///"):
            value = "file:///<LOCAL_ARTIFACT>/" + Path(unquote(urlsplit(value).path)).name
    return value


def compare(args: argparse.Namespace) -> None:
    bundle = args.baseline_capture.resolve()
    record = json.loads((bundle / "runtime.json").read_text())
    preserved_file(bundle, record["artifact"], "artifact")
    preserved_file(bundle, record["manifest"], "manifest")
    if args.baseline_kind == "preserved-release":
        release = json.loads((bundle / "release.json").read_text())
        checked = verify_release(bundle, release["release"], record["source_commit"])
        if not checked["stable_baseline_eligible"]:
            raise ValueError("Preserved baseline must be a stable release from v1.0.0 onward")
    replacement = None
    if args.replacement_capture:
        replacement = args.replacement_capture.resolve()
        proposed = json.loads((replacement / "runtime.json").read_text())
        for field in ("artifact", "source_commit", "platform", "fixtures", "fixture_versions"):
            if proposed[field] != record[field]:
                raise ValueError(f"Replacement must preserve the original {field}")
        preserved_file(replacement, proposed["artifact"], "artifact")
        preserved_file(replacement, proposed["manifest"], "manifest")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    retain_capture(bundle, record, root / "original-capture")
    selected = root / "original-capture"
    if replacement:
        retain_capture(replacement, proposed, root / "replacement-capture")
        selected = root / "replacement-capture"
    (root / "selection.json").write_text(json.dumps({"status": "unvalidated", "baseline_kind": args.baseline_kind,
        "artifact": record["artifact"], "original_manifest": record["manifest"],
        "effective_manifest": proposed["manifest"] if replacement else record["manifest"],
        "replacement_reason": args.replacement_reason, "exact_baseline_runtime": replacement is None}, indent=2) + "\n")
    restored = root / "baseline"
    restore(args.conda, selected, restored, root / "baseline-cache", tuple(path.resolve() for path in args.recovered))
    baseline = installed_environment(restored / "bin/python", record["source_commit"], True)
    candidate = installed_environment(args.candidate_python.resolve(), args.candidate_source, True)
    candidate_prefix = Path(candidate["prefix"])
    if restored.samefile(candidate_prefix):
        raise ValueError("Baseline and candidate must use distinct environments")
    candidate_packages = installed_inventory(args.conda, candidate_prefix, fixture_inventory(SOURCE))
    if digest(args.candidate_artifact) != candidate_packages["gwflow"]["sha256"]:
        raise ValueError("Candidate artifact SHA-256 differs from the independently installed package")
    baseline_packages = installed_inventory(args.conda, restored, record["fixture_versions"])
    before = {"baseline": runtime_files(restored, baseline_packages),
              "candidate": runtime_files(candidate_prefix, candidate_packages)}
    arguments = [sys.executable, str(SOURCE / "tests/validate_compatibility.py"), "--independent",
                 "--baseline-python", str(restored / "bin/python"),
                 "--candidate-python", str(args.candidate_python.resolve()),
                 "--baseline-source", record["source_commit"], "--candidate-source", args.candidate_source,
                 "--root", str(root / "journeys")]
    for pattern in args.test or []:
        arguments += ["--test", pattern]
    result = subprocess.run(arguments, env=runtime_environment())
    if result.returncode:
        raise ValueError("Installed upgrade journeys failed; inspect the retained journey evidence")
    for side, prefix, packages, fixtures in (("baseline", restored, baseline_packages, record["fixture_versions"]),
                                            ("candidate", candidate_prefix, candidate_packages, fixture_inventory(SOURCE))):
        after = installed_inventory(args.conda, prefix, fixtures)
        if identities(after) != identities(packages) or runtime_files(prefix, after) != before[side]:
            raise ValueError(f"Upgrade journeys modified the {side} runtime")
    observations = [json.loads(line) for line in (root / "journeys/transcript.jsonl").read_text().splitlines()]
    observations = [{key: value for key, value in event.items() if key not in ("stdout", "stderr")} for event in observations]
    evidence = {"status": "passed" if not args.test else "focused-tests-passed",
                "journey_selection": args.test, "baseline_kind": args.baseline_kind, "exact_baseline_runtime": replacement is None,
                "baseline_capture": record, "environments": {"baseline": baseline, "candidate": candidate},
                "native_packages": {"baseline": identities(baseline_packages), "candidate": identities(candidate_packages)},
                "journeys": json.loads((root / "journeys/summary.json").read_text()), "observations": observations,
                "replacement": {"reason": args.replacement_reason, "capture": proposed} if replacement else None,
                "restoration": json.loads((restored / "gwflow-runtime.json").read_text())}
    (root / "comparison.raw.json").write_text(json.dumps(evidence, indent=2) + "\n")
    evidence = normalized(evidence, {str(restored): "<BASELINE>", str(candidate_prefix): "<CANDIDATE>",
                                    str(root): "<COMPARISON>", str(SOURCE): "<REPO>", str(Path(sys.prefix)): "<DRIVER>"})
    evidence["path_placeholders"] = {
        "<BASELINE>": "Restored baseline installation inside the comparison root",
        "<CANDIDATE>": "Independent candidate installation",
        "<COMPARISON>": "New comparison root containing unchanged shared workflow/storage and raw evidence",
        "<REPO>": "Checkout containing the common public-workflow journey definitions",
        "<DRIVER>": "Maintainer tooling environment, separate from the compared installations",
        "<LOCAL_ARTIFACT>": "Original local file-URL directory outside these roots; filename preserved",
    }
    evidence["evidence_selection"] = "Full environment and handoff observations; commands retain arguments, cwd, and exit status. Raw stdout/stderr remain in journeys/transcript.jsonl. Normalization changes paths and adds this metadata only."
    (root / "comparison.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print("Independent restored-baseline upgrade journeys passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conda", default=os.environ.get("CONDA_EXE", "conda"))
    parser.add_argument("--baseline-capture", type=Path, required=True)
    parser.add_argument("--baseline-kind", choices=("preserved-release", "controlled-artifact"), required=True)
    parser.add_argument("--candidate-python", type=Path, required=True)
    parser.add_argument("--candidate-artifact", type=Path, required=True)
    parser.add_argument("--candidate-source", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--recovered", type=Path, action="append", default=[])
    parser.add_argument("--replacement-capture", type=Path)
    parser.add_argument("--replacement-reason")
    parser.add_argument("--test", action="append")
    args = parser.parse_args()
    if bool(args.replacement_capture) != bool(args.replacement_reason and args.replacement_reason.strip()):
        parser.error("Replacement capture and a nonempty reason must be supplied together")
    try:
        compare(args)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Independent comparison failed: {error}\n")


if __name__ == "__main__":
    main()
