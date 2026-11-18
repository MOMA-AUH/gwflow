"""Build and verify run-scoped Apptainer fixtures independently of the gwflow runtime."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import time


IMAGES = {
    "basic.sif": "tests/fixtures/container.def",
    "summary.sif": "tests/fixtures/packaged_tasks/images/summary.def",
    "report.sif": "tests/fixtures/packaged_tasks/images/report.def",
}
INPUTS = ("tests/fixtures/container.def", "tests/fixtures/gwflow-image-tool",
          "tests/fixtures/packaged_tasks/images", "tests/fixtures/packaged_tasks/packages",
          "tests/fixtures/image-build-requirements.txt", "tests/ci_fixtures.py",
          ".github/actions/install-apptainer/action.yml")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(source: Path, apptainer: str, run_id: str) -> dict:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    paths = []
    names = set(INPUTS)
    for name in IMAGES.values():
        definition = (source / name).read_text()
        if not re.search(r"^From: \S+@sha256:[0-9a-f]{64}$", definition, re.MULTILINE):
            raise ValueError(f"Fixture base image must use an immutable SHA-256 digest: {name}")
        files = False
        for line in definition.splitlines():
            if line.startswith("%"):
                files = line.strip() == "%files"
            elif files and line.strip() and not line.lstrip().startswith("#"):
                copied = shlex.split(line)[0]
                path = (source / copied).resolve()
                if not path.is_relative_to(source) or not path.exists():
                    raise ValueError(f"Copied fixture source must exist inside the checkout: {copied}")
                names.add(copied)
    for name in names:
        path = source / name
        if not path.exists():
            raise ValueError(f"Missing fixture build input: {name}")
        paths.extend(path.rglob("*") if path.is_dir() else (path,))
    if any(path.is_symlink() for path in paths):
        raise ValueError("Copied fixture inputs must not contain symbolic links")
    inputs = {path.relative_to(source).as_posix(): {"sha256": digest(path), "mode": path.stat().st_mode & 0o777}
              for path in sorted(paths) if path.is_file()}
    if not set(IMAGES.values()).issubset(inputs) or "tests/fixtures/gwflow-image-tool" not in inputs:
        raise ValueError("Fixture definitions and image tool must belong to the checkout")
    version = subprocess.check_output([apptainer, "--version"], text=True).strip()
    if version != "apptainer version 1.5.4":
        raise ValueError(f"Expected Apptainer 1.5.4, found {version}")
    return {"schema_version": 2, "source_commit": commit, "run_id": run_id,
            "platform": f"{platform.system()}-{platform.machine()}",
            "runner_image": os.environ.get("ImageOS", platform.system()),
            "apptainer": version, "inputs": inputs}


def cache_key(record: dict) -> str:
    stable = {key: value for key, value in record.items() if key not in ("source_commit", "run_id")}
    return "gwflow-fixtures-v1-" + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def verify_images(bundle: Path, record: dict) -> None:
    if set(record["images"]) != set(IMAGES):
        raise ValueError("Expected exactly the three shared fixture images")
    for name, definition in IMAGES.items():
        image = record["images"][name]
        if image["definition"] != definition or digest(bundle / name) != image["sha256"]:
            raise ValueError(f"Shared fixture checksum or definition differs: {name}")


def build(source: Path, bundle: Path, apptainer: str, run_id: str) -> None:
    record = identity(source, apptainer, run_id)
    bundle.mkdir(parents=True)
    manifest = bundle / "fixtures.json"
    if manifest.exists():
        raise ValueError("Fixture bundle already contains a manifest")
    started = time.monotonic()
    images = {}
    for name, definition in IMAGES.items():
        image_started = time.monotonic()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("APPTAINER_", "SINGULARITY_"))}
        subprocess.run([apptainer, "build", str(bundle / name), definition], cwd=source, env=environment, check=True)
        images[name] = {"sha256": digest(bundle / name), "definition": definition,
                        "build_seconds": round(time.monotonic() - image_started, 3)}
    # Do not publish a manifest if checkout inputs changed while the images built.
    if identity(source, apptainer, run_id) != record:
        raise ValueError("Fixture source changed during preparation")
    record.update(images=images, preparation_seconds=round(time.monotonic() - started, 3))
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    print(f"Prepared three shared container fixtures in {record['preparation_seconds']} seconds")


def verify(source: Path, bundle: Path, apptainer: str, run_id: str, github_env: Path | None) -> None:
    started = time.monotonic()
    expected = identity(source, apptainer, run_id)
    record = json.loads((bundle / "fixtures.json").read_text())
    if not isinstance(record, dict):
        raise ValueError("Fixture manifest must be a JSON object")
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Fixture source, run, platform, or Apptainer identity differs from preparation")
    verify_images(bundle, record)
    if github_env is not None:
        with github_env.open("a") as stream:
            for variable, name in (("GWFLOW_TEST_SIF", "basic.sif"), ("GWFLOW_TEST_SUMMARY_SIF", "summary.sif"),
                                   ("GWFLOW_TEST_REPORT_SIF", "report.sif")):
                stream.write(f"{variable}={bundle / name}\n")
            stream.write("GWFLOW_TEST_BIOINFORMATICS=1\n")
    print(f"Verified three shared container fixtures in {time.monotonic() - started:.3f} seconds")


def prepare(source: Path, bundle: Path, cache: Path, apptainer: str, run_id: str) -> None:
    if bundle == cache or bundle.is_relative_to(cache) or cache.is_relative_to(bundle):
        raise ValueError("Run bundle and fixture cache must be separate directories")
    if bundle.exists():
        raise ValueError("Current-run fixture bundle already exists")
    expected = identity(source, apptainer, run_id)
    started = time.monotonic()
    try:
        record = json.loads((cache / "fixtures.json").read_text())
        if not isinstance(record, dict):
            raise ValueError("Cached fixture manifest must be a JSON object")
        if any(record.get(key) != value for key, value in expected.items()
               if key not in ("source_commit", "run_id")):
            raise ValueError("Cached fixture inputs differ")
        verify_images(cache, record)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Fixture cache miss or rejection: {error}")
        if cache.exists():
            shutil.rmtree(cache)
        build(source, cache, apptainer, run_id)
        record = json.loads((cache / "fixtures.json").read_text())
        status = "miss"
    else:
        status = "hit"
    bundle.mkdir(parents=True)
    for name in IMAGES:
        shutil.copyfile(cache / name, bundle / name)
    origin = {key: record[key] for key in ("source_commit", "run_id")}
    record.update(expected, cache_status=status, cache_key=cache_key(expected), cache_origin=origin,
                  preparation_seconds=round(time.monotonic() - started, 3))
    (bundle / "fixtures.json").write_text(json.dumps(record, indent=2) + "\n")
    verify(source, bundle, apptainer, run_id, None)
    print(f"Fixture cache {status}; prepared current-run artifact in {record['preparation_seconds']} seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify", "prepare", "key"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--apptainer", default="apptainer")
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "key":
            print(cache_key(identity(args.source.resolve(), args.apptainer, "cache-key")))
            return
        if args.bundle is None:
            raise ValueError("Fixture preparation and verification require --bundle")
        if not args.run_id:
            raise ValueError("Fixture preparation and verification require --run-id")
        if args.command == "build":
            build(args.source.resolve(), args.bundle.resolve(), args.apptainer, args.run_id)
        elif args.command == "prepare":
            if args.cache is None:
                raise ValueError("prepare requires --cache")
            prepare(args.source.resolve(), args.bundle.resolve(), args.cache.resolve(), args.apptainer, args.run_id)
        else:
            verify(args.source.resolve(), args.bundle.resolve(), args.apptainer, args.run_id, args.github_env)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"CI fixtures rejected: {error}\n")


if __name__ == "__main__":
    main()
