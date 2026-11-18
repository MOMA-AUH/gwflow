"""Build and verify run-scoped Apptainer fixtures independently of the gwflow runtime."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import time


IMAGES = {
    "basic.sif": "tests/fixtures/container.def",
    "summary.sif": "tests/fixtures/packaged_tasks/images/summary.def",
    "report.sif": "tests/fixtures/packaged_tasks/images/report.def",
}
INPUTS = ("tests/fixtures/container.def", "tests/fixtures/gwflow-image-tool",
          "tests/fixtures/packaged_tasks/images", "tests/fixtures/packaged_tasks/packages")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(source: Path, apptainer: str, run_id: str) -> dict:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    names = subprocess.check_output(["git", "ls-files", "-z", "--", *INPUTS], cwd=source).decode().split("\0")
    inputs = {name: digest(source / name) for name in names if name}
    if not set(IMAGES.values()).issubset(inputs) or "tests/fixtures/gwflow-image-tool" not in inputs:
        raise ValueError("Fixture definitions and image tool must belong to the checkout")
    version = subprocess.check_output([apptainer, "--version"], text=True).strip()
    if version != "apptainer version 1.5.4":
        raise ValueError(f"Expected Apptainer 1.5.4, found {version}")
    return {"schema_version": 1, "source_commit": commit, "run_id": run_id,
            "platform": f"{platform.system()}-{platform.machine()}", "apptainer": version, "inputs": inputs}


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
        subprocess.run([apptainer, "build", str(bundle / name), definition], cwd=source, check=True)
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
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Fixture source, run, platform, or Apptainer identity differs from preparation")
    if set(record["images"]) != set(IMAGES):
        raise ValueError("Expected exactly the three shared fixture images")
    for name, definition in IMAGES.items():
        image = record["images"][name]
        if image["definition"] != definition or digest(bundle / name) != image["sha256"]:
            raise ValueError(f"Shared fixture checksum or definition differs: {name}")
    if github_env is not None:
        with github_env.open("a") as stream:
            for variable, name in (("GWFLOW_TEST_SIF", "basic.sif"), ("GWFLOW_TEST_SUMMARY_SIF", "summary.sif"),
                                   ("GWFLOW_TEST_REPORT_SIF", "report.sif")):
                stream.write(f"{variable}={bundle / name}\n")
            stream.write("GWFLOW_TEST_BIOINFORMATICS=1\n")
    print(f"Verified three shared container fixtures in {time.monotonic() - started:.3f} seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apptainer", default="apptainer")
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args.source.resolve(), args.bundle.resolve(), args.apptainer, args.run_id)
        else:
            verify(args.source.resolve(), args.bundle.resolve(), args.apptainer, args.run_id, args.github_env)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"CI fixtures rejected: {error}\n")


if __name__ == "__main__":
    main()
