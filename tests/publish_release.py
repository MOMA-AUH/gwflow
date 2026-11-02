"""Publish a qualified CI bundle and prepare its verified release draft."""

import argparse
import hashlib
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen
from pathlib import Path
import shutil
import subprocess
import tempfile

from release_artifacts import (VALIDATION_FILES, archive_name, attach, digest, draft_assets, final_tag, github,
                               pack, unpack, verify)


def channel_package(args, release):
    """Only an exact-distribution 404 permits a non-replacing upload."""
    artifact = release["artifact"]
    filename = artifact["filename"]
    identity = "MOMA-AUH/gwflow/" + quote(args.tag[1:], safe="") + "/noarch/" + quote(filename, safe="")
    try:
        with urlopen(args.api_url.rstrip("/") + "/dist/" + identity, timeout=60) as response:
            remote = json.load(response)
    except HTTPError as error:
        if error.code == 404:
            return False
        raise ValueError(f"Cannot inspect channel distribution: {error}") from error
    if (remote["basename"] != "noarch/" + filename or remote["version"] != args.tag[1:]
            or remote["attrs"]["subdir"] != "noarch" or "main" not in remote["labels"]
            or remote["sha256"] != artifact["sha256"]):
        raise ValueError("Existing channel package identity differs; refusing replacement")
    checksum = hashlib.sha256()
    with urlopen(args.api_url.rstrip("/") + "/download/" + identity, timeout=60) as response:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            checksum.update(chunk)
    if checksum.hexdigest() != artifact["sha256"]:
        raise ValueError("Existing channel package bytes differ; refusing replacement")
    print("Verified existing channel package identity and SHA256")
    return True


def ensure_channel_package(args, release):
    if channel_package(args, release):
        return
    if not os.environ.get("ANACONDA_API_TOKEN"):
        raise ValueError("ANACONDA_API_TOKEN is required to upload a missing distribution")
    result = subprocess.run([args.conda, "run", "-n", "base", "anaconda", "upload", "--user", "MOMA-AUH",
                             "--register", "--label", "main", "--no-progress", "--fail",
                             str(args.bundle / release["artifact"]["filename"])])
    # A failed response can follow a successful upload, including a racing publisher.
    # Never infer acceptance from the exit status or suppress a conflict blindly.
    if not channel_package(args, release):
        raise ValueError(f"Channel package remains absent after upload (exit status {result.returncode})")


def resume_archive(args, assets, work):
    """Reuse earlier publication evidence only for this exact qualified CI bundle."""
    if not final_tag(args.tag):
        return
    expected = {path.name for path in assets.iterdir()}
    existing = draft_assets(args.tag, args.repo, args.gh, expected) or set()
    archive = archive_name(args.tag)
    if archive not in existing:
        return
    previous = work / "previous-assets"
    previous.mkdir()
    github(args.gh, "release", "download", args.tag, "--repo", args.repo,
           "--pattern", archive, "--dir", str(previous))
    package = next(name for name in expected if name not in {archive, "SHA256SUMS"})
    shutil.copyfile(assets / package, previous / package)
    (previous / "SHA256SUMS").write_text("".join(
        f"{digest(previous / name)}  {name}\n" for name in sorted((package, archive))))
    restored = work / "previous-bundle"
    unpack(previous, restored, args.tag, args.source)
    for name in VALIDATION_FILES - {"publication.json"}:
        if (restored / name).read_bytes() != (args.bundle / name).read_bytes():
            raise ValueError(f"Existing validation archive differs from selected CI bundle: {name}")
    # attach() will also download and compare every existing asset, including sums.
    for name in (archive, "SHA256SUMS"):
        shutil.copyfile(previous / name, assets / name)
    print("Preserved verified prior publication archive; fresh checks recorded separately")


def publish(args):
    release = verify(args.bundle, args.tag, args.source)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Evidence output already exists; select a fresh destination")
    ensure_channel_package(args, release)
    with tempfile.TemporaryDirectory(prefix="gwflow-publication-") as temporary:
        work = Path(temporary)
        prefix = str(work / "environment")
        build = release["artifact"]["filename"].removesuffix(".conda").removesuffix(".tar.bz2").rsplit("-", 1)[1]
        for attempt in range(10):
            result = subprocess.run([args.conda, "create", "-y", "-p", prefix, "python=3.12",
                                     "gwflow=" + args.tag[1:] + "=" + build, "--override-channels",
                                     "-c", "MOMA-AUH", "-c", "gwforg", "-c", "conda-forge"])
            if result.returncode == 0:
                break
            if attempt == 9:
                raise ValueError("Published package was not installable after 10 attempts")
            time.sleep(args.retry_delay)
        run = [args.conda, "run", "-p", prefix, "python"]
        subprocess.run([*run, "-m", "pip", "install", "./examples/packaged/packages/summary-task",
                        "./examples/packaged/packages/report-task"], check=True)
        subprocess.run([*run, "tests/release_smoke.py"], check=True)
        publication = work / "publication-attempt.json"
        subprocess.run([*run, "tests/release_provenance.py", "--artifacts", str(args.bundle), "--tag", args.tag,
                        "--source", args.source, "--output", str(publication)], check=True)
        assets = work / "assets"
        pack(args.bundle, publication, assets, args.tag, args.source)
        resume_archive(args, assets, work)
        attach(assets, args.tag, args.source, args.repo, args.gh)
        args.output.mkdir()
        shutil.copytree(assets, args.output / "assets")
        shutil.copyfile(publication, args.output / publication.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("tag", "source", "repo"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--conda", default="conda")
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--retry-delay", type=float, default=30, help="Seconds between channel installation attempts")
    parser.add_argument("--api-url", default="https://api.anaconda.org")
    args = parser.parse_args()
    try:
        publish(args)
    except (ValueError, KeyError, OSError, URLError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Publication failed: {error}\n")


if __name__ == "__main__":
    main()
