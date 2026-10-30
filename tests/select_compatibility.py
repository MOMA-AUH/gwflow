"""Select immutable compatibility baselines from the checked-out descriptor."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.request import urlopen
from urllib.parse import unquote, urlsplit

from compare_runtime import normalized
from release_artifacts import stable_tag, verify as verify_release, verify_publication

from conda_runtime import digest


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version[1:].split("."))


def reference(value: dict) -> dict:
    version, source, assets = value["version"], value["source_commit"], value["assets"]
    if not stable_tag(version) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        raise ValueError("Stable baseline requires a final release from v1.0.0 and an exact source commit")
    metadata = {"runtime.explicit.txt", "runtime.json", "qualification.json", "release.json", "SHA256SUMS", "publication.json"}
    packages = set(assets) - metadata
    if len(packages) != 1 or not metadata <= set(assets):
        raise ValueError("Pin the exact package and complete qualified release attachments")
    package = next(iter(packages))
    if not package.startswith("gwflow-" + version[1:] + "-") or not package.endswith((".conda", ".tar.bz2")):
        raise ValueError("Pinned package filename differs from the release version")
    for filename, asset in assets.items():
        url = urlsplit(asset["url"])
        if (Path(filename).name != filename or filename in (".", "..") or "\\" in filename
                or re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]) is None
                or url.scheme not in ("https", "http", "file") or url.fragment or url.username or url.password
                or (url.scheme != "file" and not url.netloc)):
            raise ValueError("Every pinned asset requires a safe filename, exact URL, and SHA-256")
    return {"version": version, "source_commit": source, "assets": assets}


def select(descriptor: Path) -> dict:
    value = json.loads(descriptor.read_text())
    if value.get("schema_version") != 2 or "stable" not in value:
        raise ValueError("Expected compatibility descriptor schema_version 2 with explicit stable selection")
    plan = {"schema_version": 1, "descriptor_sha256": digest(descriptor), "status": "selected", "baselines": []}
    if value["stable"] is None:
        source = value["source"]
        if re.fullmatch(r"[0-9a-f]{40}", source["commit"]) is None:
            raise ValueError("Provisional source must name an exact 40-character commit")
        return {**plan, "mode": "provisional-source", "source": source}
    stable = value["stable"]
    original, latest = reference(stable["original"]), reference(stable["latest"])
    if version_key(original["version"]) > version_key(latest["version"]):
        raise ValueError("Original baseline must not be newer than latest")
    entries = [{**original, "roles": ["original"]}]
    if original["version"] == latest["version"]:
        if original != latest:
            raise ValueError("The same release has conflicting pinned identities")
        entries[0]["roles"].append("latest")
    else:
        entries.append({**latest, "roles": ["latest"]})
    coverages = set()
    versions = {entry["version"] for entry in entries}
    for item in stable.get("intermediate", []):
        extra = reference(item)
        coverage = item.get("coverage", "").strip()
        if not coverage or coverage in coverages:
            raise ValueError("Intermediate baseline requires distinct nonempty coverage")
        if (extra["version"] in versions or not
                version_key(original["version"]) < version_key(extra["version"]) < version_key(latest["version"])):
            raise ValueError("Intermediate baseline must be unique and between original and latest")
        entries.append({**extra, "roles": ["intermediate"], "coverage": coverage})
        coverages.add(coverage)
        versions.add(extra["version"])
    return {**plan, "mode": "preserved-release", "baselines": entries}


def download(reference: dict, bundle: Path) -> None:
    bundle.mkdir(parents=True)
    for filename, asset in reference["assets"].items():
        path = bundle / filename
        with urlopen(asset["url"], timeout=60) as response, path.open("wb") as destination:
            shutil.copyfileobj(response, destination)
        if digest(path) != asset["sha256"]:
            raise ValueError(f"Pinned SHA-256 mismatch for {filename}")
    release = verify_release(bundle, reference["version"], reference["source_commit"])
    if not release["stable_baseline_eligible"]:
        raise ValueError("Downloaded release is not an eligible stable baseline")
    verify_publication(release, json.loads((bundle / "publication.json").read_text()))


def evidence(root: Path, descriptor: Path, selection: dict) -> None:
    write_json(root / "selection.raw.json", selection)
    roots = {str(root.resolve()): "<SELECTION>", str(descriptor.resolve().parent): "<DESCRIPTOR>",
             str(Path(__file__).resolve().parents[1]): "<REPO>", str(Path.home()): "<HOME>",
             str(Path(sys.prefix)): "<DRIVER>"}
    for entry in selection["baselines"]:
        for asset in entry["assets"].values():
            url = urlsplit(asset["url"])
            if url.scheme == "file":
                roots[str(Path(unquote(url.path)).parent)] = "<LOCAL_ARTIFACT>"
    clean = normalized(selection, roots)
    clean["path_placeholders"] = {
        "<SELECTION>": "New selection output directory containing downloaded assets and individual comparisons",
        "<DESCRIPTOR>": "Directory containing the checked-out pinned descriptor",
        "<REPO>": "Checkout containing the comparison tooling",
        "<HOME>": "Maintainer home directory outside the checkout",
        "<DRIVER>": "Maintainer tooling installation",
        "<CANDIDATE>": "Independent candidate installation",
        "<CANDIDATE_ARTIFACTS>": "Directory containing the independently installed candidate artifact",
        "<LOCAL_ARTIFACT>": "Local controlled asset directory; original filename retained",
    }
    write_json(root / "selection.json", clean)


def fetch(descriptor: Path, root: Path) -> dict:
    selection = select(descriptor)
    if selection["mode"] != "preserved-release":
        raise ValueError("Provisional selection requires the pinned source regression check; no stable comparisons performed")
    root.mkdir(parents=True, exist_ok=False)
    evidence(root, descriptor, selection)
    for entry in selection["baselines"]:
        try:
            download(entry, root / entry["version"] / "assets")
            entry["status"] = "verified"
        except (ValueError, OSError, KeyError, TypeError) as error:
            entry.update(status="failed", error=str(error))
        evidence(root, descriptor, selection)
    selection["status"] = "failed" if any(entry["status"] == "failed" for entry in selection["baselines"]) else "verified"
    evidence(root, descriptor, selection)
    return selection


def run(args: argparse.Namespace, selection: dict) -> None:
    selection["status"] = "running"
    for entry in selection["baselines"]:
        if entry["status"] == "failed":
            continue
        directory = args.root / entry["version"]
        entry["status"] = "running"
        evidence(args.root, args.descriptor, selection)
        arguments = [sys.executable, str(Path(__file__).with_name("compare_runtime.py")),
                     "--baseline-capture", str((directory / "assets").resolve()), "--baseline-kind", "preserved-release",
                     "--candidate-python", str(args.candidate_python.resolve()),
                     "--candidate-artifact", str(args.candidate_artifact.resolve()), "--candidate-source", args.candidate_source,
                     "--root", str((directory / "comparison").resolve()), "--conda", args.conda]
        try:
            with (directory / "comparison.log").open("w") as log:
                result = subprocess.run(arguments, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise ValueError((directory / "comparison.log").read_text()[-4000:].strip())
            comparison = json.loads((directory / "comparison/comparison.json").read_text())
            if (comparison["status"] != "passed" or comparison["baseline_kind"] != "preserved-release"
                    or comparison["exact_baseline_runtime"] is not True
                    or comparison["journeys"] != {"run": 15, "failures": 0, "errors": 0, "skips": 0}):
                raise ValueError("Comparison did not pass all completed/incomplete upgrade journeys in the exact runtime")
            entry.update(status="passed", comparison=f"{entry['version']}/comparison/comparison.json")
        except (ValueError, OSError, KeyError, TypeError) as error:
            message = normalized(str(error), {
                str(args.candidate_python.resolve().parent.parent): "<CANDIDATE>",
                str(args.candidate_artifact.resolve().parent): "<CANDIDATE_ARTIFACTS>",
            })
            entry.update(status="failed", error=message, log=f"{entry['version']}/comparison.log")
        evidence(args.root, args.descriptor, selection)
    selection["status"] = "passed" if all(entry["status"] == "passed" for entry in selection["baselines"]) else "failed"
    evidence(args.root, args.descriptor, selection)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--output", type=Path, required=True)
    fetching = commands.add_parser("fetch")
    fetching.add_argument("--root", type=Path, required=True)
    running = commands.add_parser("run")
    running.add_argument("--root", type=Path, required=True)
    running.add_argument("--candidate-python", type=Path, required=True)
    running.add_argument("--candidate-artifact", type=Path, required=True)
    running.add_argument("--candidate-source", required=True)
    running.add_argument("--conda", default=os.environ.get("CONDA_EXE", "conda"))
    for command in (plan, fetching, running):
        command.add_argument("--descriptor", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            write_json(args.output, select(args.descriptor))
        else:
            selection = fetch(args.descriptor, args.root)
            if args.command == "run":
                run(args, selection)
            failed = [entry["version"] for entry in selection["baselines"] if entry["status"] == "failed"]
            if failed:
                raise ValueError("Failed baselines: " + ", ".join(failed) + "; inspect selection.json")
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, f"Compatibility selection failed: {error}\n")


if __name__ == "__main__":
    main()
