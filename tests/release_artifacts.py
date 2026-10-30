"""Prepare and verify the qualified artifacts retained by a Conda release."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from conda_runtime import digest, explicit_identity, identities, preserved_file, read_dependency_manifest


def stable_tag(tag: str) -> bool:
    return re.fullmatch(r"v[1-9][0-9]*\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag) is not None


def asset_names(record: dict) -> set[str]:
    return {record["artifact"]["filename"], "runtime.explicit.txt", "runtime.json", "qualification.json", "release.json"}


def qualified_capture(capture: Path, qualification: Path, tag: str, source: str) -> dict:
    record = json.loads((capture / "runtime.json").read_text())
    evidence = json.loads(qualification.read_text())
    artifact = preserved_file(capture, record["artifact"], "artifact")
    manifest = preserved_file(capture, record["manifest"], "manifest")
    if (record["schema_version"] != 1 or record["source_commit"] != source
            or re.fullmatch(r"[0-9a-f]{40}", source) is None):
        raise ValueError("Capture does not identify the requested source commit")
    if record["manifest"]["filename"] != "runtime.explicit.txt":
        raise ValueError("Expected the captured native runtime manifest")
    package = explicit_identity(
        f"https://preserved.invalid/{record['artifact']['subdir']}/{artifact.name}#{record['artifact']['sha256']}",
        record["platform"])
    if package["name"] != "gwflow" or tag != "v" + package["version"]:
        raise ValueError("Release tag does not match the tested gwflow artifact")
    if (evidence["status"] != "qualified" or evidence["fresh_cache"] is not True
            or evidence["fixture_runtime_unchanged"] is not True
            or evidence["smoke"] != {"command": "python tests/release_smoke.py", "exit_status": 0}):
        raise ValueError("Release requires successful fresh-cache reconstruction and unchanged-runtime smoke qualification")
    for field in ("platform", "artifact", "manifest", "source_commit", "fixture_versions"):
        if evidence[field] != record[field]:
            raise ValueError(f"Qualification differs from the captured runtime: {field}")
    _, expected = read_dependency_manifest(manifest, record["platform"])
    expected["gwflow"] = package
    if evidence["packages"] != identities(expected):
        raise ValueError("Qualified package inventory differs from the release artifacts")
    return record


def prepare(capture: Path, qualification: Path, tag: str, source: str, output: Path) -> None:
    record = qualified_capture(capture, qualification, tag, source)
    artifact = preserved_file(capture, record["artifact"], "artifact")
    manifest = preserved_file(capture, record["manifest"], "manifest")
    output.mkdir(parents=True, exist_ok=False)
    for path in (artifact, manifest, capture / "runtime.json"):
        shutil.copyfile(path, output / path.name)
    shutil.copyfile(qualification, output / "qualification.json")
    release = {"schema_version": 1, "release": tag, "source_commit": source,
               "stable_baseline_eligible": stable_tag(tag), "artifact": record["artifact"], "manifest": record["manifest"]}
    (output / "release.json").write_text(json.dumps(release, indent=2) + "\n")
    files = sorted(path for path in output.iterdir() if path.is_file())
    (output / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in files))
    verify(output, tag, source)
    print("Prepared qualified release artifacts")


def verify(bundle: Path, tag: str, source: str) -> dict:
    release = json.loads((bundle / "release.json").read_text())
    if release["schema_version"] != 1 or release["release"] != tag or release["source_commit"] != source:
        raise ValueError("Release bundle does not match the requested tag and source")
    checked = set()
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        checksum, filename = line.split("  ", 1)
        if filename in checked:
            raise ValueError("Duplicate file in the release checksum list")
        preserved_file(bundle, {"filename": filename, "sha256": checksum}, "release file")
        checked.add(filename)
    if checked != asset_names(release):
        raise ValueError("Release checksum list must cover the complete qualified asset set")
    record = qualified_capture(bundle, bundle / "qualification.json", tag, source)
    if (release["stable_baseline_eligible"] is not stable_tag(tag)
            or any(release[field] != record[field] for field in ("artifact", "manifest"))):
        raise ValueError("Release identities or stable classification differ from the qualified capture")
    return release


def github(gh: str, *arguments: str) -> subprocess.CompletedProcess:
    result = subprocess.run([gh, *arguments], text=True, capture_output=True)
    if result.returncode:
        raise ValueError(f"GitHub release operation failed: {result.stderr.strip()}")
    return result


def verify_publication(release: dict, provenance: dict) -> None:
    if (provenance["release"] != release["release"] or provenance["source_commit"] != release["source_commit"]
            or any(provenance["artifact"][field] != release["artifact"][field] for field in ("filename", "sha256"))
            or provenance["artifact"]["url"].casefold() != (
                "https://conda.anaconda.org/moma-auh/noarch/" + release["artifact"]["filename"]).casefold()):
        raise ValueError("Published package provenance does not match the qualified release artifact")


def attach(bundle: Path, publication: Path, tag: str, source: str, repo: str, gh: str) -> None:
    release = verify(bundle, tag, source)
    verify_publication(release, json.loads(publication.read_text()))
    if publication.name != "publication.json":
        raise ValueError("Expected publication.json provenance attachment")
    if not release["stable_baseline_eligible"]:
        print("Pre-stable or prerelease provenance verified; no stable baseline attachments created")
        return
    result = subprocess.run([gh, "release", "view", tag, "--repo", repo,
                             "--json", "tagName,isDraft,isPrerelease,assets"], text=True, capture_output=True)
    state: dict
    if result.returncode:
        if "release not found" not in result.stderr and "HTTP 404" not in result.stderr:
            raise ValueError(f"Cannot inspect GitHub release: {result.stderr.strip()}")
        github(gh, "release", "create", tag, "--repo", repo, "--verify-tag", "--draft", "--title", tag,
               "--notes", "Qualified package and runtime attachments. Review release notes before publishing.")
        state = {"tagName": tag, "isDraft": True, "isPrerelease": False, "assets": []}
    else:
        state = json.loads(result.stdout)
    if state["tagName"] != tag or state["isPrerelease"] or not state["isDraft"]:
        raise ValueError("Stable preservation requires the matching non-prerelease draft; published assets are immutable")
    files = [bundle / name for name in sorted(asset_names(release) | {"SHA256SUMS"})] + [publication]
    existing = {asset["name"] for asset in state["assets"]}
    with tempfile.TemporaryDirectory(prefix="gwflow-release-assets-") as temporary:
        for path in files:
            if path.name in existing:
                github(gh, "release", "download", tag, "--repo", repo, "--pattern", path.name, "--dir", temporary)
                if digest(Path(temporary) / path.name) != digest(path):
                    raise ValueError(f"Existing release asset differs; refusing replacement: {path.name}")
        missing = [str(path) for path in files if path.name not in existing]
        if missing:
            github(gh, "release", "upload", tag, *missing, "--repo", repo)
    print("Preserved exact stable artifacts in the release draft; publish reviewed notes before selecting a baseline")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare", help="Prepare attachments after runtime qualification passes")
    preparation.add_argument("--capture", required=True, type=Path)
    preparation.add_argument("--qualification", required=True, type=Path)
    preparation.add_argument("--output", required=True, type=Path)
    verification = commands.add_parser("verify", help="Verify the transferred bundle before publication")
    verification.add_argument("--bundle", required=True, type=Path)
    attachment = commands.add_parser("attach", help="Retain stable artifacts in a draft after channel provenance passes")
    attachment.add_argument("--bundle", required=True, type=Path)
    attachment.add_argument("--publication", required=True, type=Path)
    attachment.add_argument("--repo", required=True)
    attachment.add_argument("--gh", default="gh")
    for command in (preparation, verification, attachment):
        command.add_argument("--tag", required=True)
        command.add_argument("--source", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.capture, args.qualification, args.tag, args.source, args.output)
        elif args.command == "verify":
            verify(args.bundle, args.tag, args.source)
        else:
            attach(args.bundle, args.publication, args.tag, args.source, args.repo, args.gh)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Release preservation failed: {error}\n")


if __name__ == "__main__":
    main()
