"""Capture and restore exact tested Conda runtimes for release and upgrade checks."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import unquote, urlsplit


FIXTURES = ("gwflow-summary-task", "gwflow-report-task")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(*arguments: str, environment: dict[str, str] | None = None, show_output: bool = False) -> str:
    result = subprocess.run(arguments, text=True, capture_output=True, env=environment)
    if result.returncode:
        raise ValueError(f"{arguments[0]} failed ({result.returncode}): {result.stdout}{result.stderr}")
    if show_output:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    return result.stdout


def host_platform() -> str:
    platforms = {("Linux", "x86_64"): "linux-64", ("Linux", "aarch64"): "linux-aarch64",
                 ("Darwin", "x86_64"): "osx-64", ("Darwin", "arm64"): "osx-arm64"}
    try:
        return platforms[platform.system(), platform.machine()]
    except KeyError:
        raise ValueError("Unsupported host platform for Conda runtime preservation") from None


def installed_inventory(conda: str, prefix: Path, fixtures: dict[str, str]) -> dict[str, dict]:
    paths = list((prefix / "conda-meta").glob("*.json"))
    if not paths:
        raise ValueError("The tested prefix has no Conda package records")
    records = {}
    for path in paths:
        record = json.loads(path.read_text())
        name = record["name"]
        if name in records:
            raise ValueError(f"Duplicate installed package: {name}")
        records[name] = record
    observed = json.loads(run(conda, "list", "--prefix", str(prefix), "--json"))
    for item in observed:
        if item["name"] in FIXTURES and item["version"] != fixtures[item["name"]]:
            raise ValueError(f"Installed example fixture differs from source: {item['name']}")
    runtime = [item for item in observed if item["name"] not in FIXTURES]
    if {item["name"] for item in runtime} != set(records):
        raise ValueError("Installed runtime includes missing records or unsupported package sources")
    for item in runtime:
        record = records[item["name"]]
        if (item["version"], item["build_string"]) != (record["version"], record["build"]):
            raise ValueError(f"Installed runtime differs from its Conda record: {item['name']}")
        if item.get("channel") == "pypi" or item.get("platform") == "pypi":
            raise ValueError(f"Unsupported runtime package source: {item['name']}")
    if not {"python", "gwflow"} <= set(records):
        raise ValueError("The tested runtime must include Conda Python and gwflow")
    verify_installed_files(prefix, records)
    return records


def verify_installed_files(prefix: Path, records: dict[str, dict]) -> None:
    python_version = ".".join(records["python"]["version"].split(".")[:2])
    paths = {}
    for name, record in records.items():
        if "paths_data" not in record:
            raise ValueError(f"Missing installed-file integrity metadata: {name}")
        recorded_paths = set()
        for item in record["paths_data"]["paths"]:
            relative = item["_path"]
            if relative.startswith("site-packages/"):
                relative = f"lib/python{python_version}/" + relative
            elif relative.startswith("python-scripts/"):
                relative = "bin/" + relative.removeprefix("python-scripts/")
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError(f"Invalid installed file path: {name}")
            recorded_paths.add(relative)
            paths[prefix / relative] = (name, item)
        if recorded_paths != set(record["files"]):
            raise ValueError(f"Incomplete installed-file integrity metadata: {name}")
    for path, (name, item) in paths.items():
        kind = item["path_type"]
        if kind in ("pyc_file", "unix_python_entry_point"):
            # Conda generates these during installation without package-file hashes.
            continue
        if kind == "softlink":
            target = path.resolve()
            # Conda's link hash describes the build-time target, which can come
            # from a different dependency build than this runtime selected.
            if (not path.is_symlink() or not target.is_relative_to(prefix) or not target.exists()
                    or (not target.is_dir() and target not in paths)):
                raise ValueError(f"Installed link differs from its Conda artifact: {name}: {path.relative_to(prefix)}")
            continue
        if kind != "hardlink":
            raise ValueError(f"Unsupported installed-file metadata: {name}: {kind}")
        expected = item.get("sha256_in_prefix")
        if expected is None and not item.get("prefix_placeholder"):
            expected = item.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"Missing installed-file SHA-256: {name}: {path.relative_to(prefix)}")
        if path.is_symlink() or not path.resolve().is_relative_to(prefix) or not path.is_file() or digest(path) != expected:
            raise ValueError(f"Installed file differs from its Conda artifact: {name}: {path.relative_to(prefix)}")


def package_entry(record: dict, target: str, *, local: bool = False) -> str:
    checksum = record.get("sha256", "")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError(f"Missing or invalid SHA-256 for {record['name']}")
    url = record.get("url", "")
    parsed = urlsplit(url)
    if (parsed.scheme not in (("https", "http", "file") if local else ("https", "http"))
            or (parsed.scheme != "file" and not parsed.netloc) or parsed.fragment
            or parsed.username or parsed.password or parsed.query):
        raise ValueError(f"Missing or unsupported artifact URL for {record['name']}")
    filename = record.get("fn", "")
    expected = f"{record['name']}-{record['version']}-{record['build']}"
    if filename not in (expected + ".conda", expected + ".tar.bz2") or unquote(parsed.path.rsplit("/", 1)[-1]) != filename:
        raise ValueError(f"Artifact filename differs from installed identity: {record['name']}")
    if record.get("subdir") not in (target, "noarch"):
        raise ValueError(f"Installed package platform mismatch: {record['name']}")
    return url + "#" + checksum


def fixture_inventory(source: Path) -> dict[str, str]:
    names = run("git", "-C", str(source), "ls-files", "examples/packaged/packages").splitlines()
    if not names:
        raise ValueError("Source checkout has no tracked example fixtures")
    return {name: digest(source / name) for name in names}


def capture(conda: str, prefix: Path, artifact: Path, source: Path, output: Path) -> None:
    target = host_platform()
    fixtures = {}
    for name in ("summary-task", "report-task"):
        with (source / "examples/packaged/packages" / name / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        fixtures[project["name"]] = project["version"]
    records = installed_inventory(conda, prefix, fixtures)
    entries = {name: package_entry(record, target, local=name == "gwflow") for name, record in records.items()}
    exported = run(conda, "list", "--prefix", str(prefix), "--explicit", "--sha256")
    exported_entries = [line.strip() for line in exported.splitlines() if line.strip() and not line.startswith("#")]
    if not exported_entries or exported_entries[0] != "@EXPLICIT" or sorted(exported_entries[1:]) != sorted(entries.values()):
        raise ValueError("Conda explicit export does not match the complete installed runtime inventory")
    package = records["gwflow"]
    if artifact.name != package["fn"] or digest(artifact) != package["sha256"]:
        raise ValueError("Supplied gwflow artifact does not match the tested installation")
    manifest = "# platform: " + target + "\n@EXPLICIT\n" + "".join(entries[name] + "\n" for name in sorted(entries) if name != "gwflow")
    record = {"schema_version": 1, "platform": target,
              "tools": {"conda": run(conda, "--version").strip(), "python": platform.python_version()},
              "source_commit": run("git", "-C", str(source), "rev-parse", "HEAD").strip(),
              "artifact": {"filename": artifact.name, "sha256": digest(artifact), "subdir": package["subdir"]},
              "manifest": {"filename": "runtime.explicit.txt", "sha256": hashlib.sha256(manifest.encode()).hexdigest()},
              "fixtures": fixture_inventory(source), "fixture_versions": fixtures}
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(artifact, output / artifact.name)
    if digest(output / artifact.name) != package["sha256"]:
        raise ValueError("The tested artifact changed during capture")
    (output / "runtime.explicit.txt").write_text(manifest)
    (output / "runtime.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"Captured {len(entries) - 1} runtime dependencies and the exact gwflow artifact")


def preserved_file(bundle: Path, identity: dict, kind: str) -> Path:
    name = identity["filename"]
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError(f"Invalid {kind} filename")
    path = bundle / name
    if digest(path) != identity["sha256"]:
        raise ValueError(f"{kind} SHA-256 does not match its recorded identity")
    return path


def explicit_identity(entry: str, target: str) -> dict:
    url, separator, checksum = entry.rpartition("#")
    if not separator:
        raise ValueError("Every explicit dependency requires a SHA-256")
    parsed = urlsplit(url)
    filename = unquote(parsed.path.rsplit("/", 1)[-1])
    suffix = next((suffix for suffix in (".conda", ".tar.bz2") if filename.endswith(suffix)), None)
    if suffix is None:
        raise ValueError("Unsupported dependency artifact format")
    name, version, build = filename[:-len(suffix)].rsplit("-", 2)
    record = {"name": name, "version": version, "build": build, "fn": filename,
              "subdir": parsed.path.rsplit("/", 2)[-2], "url": url, "sha256": checksum}
    package_entry(record, target)
    return record


def runtime_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "CONDA_SUBDIR")}
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def identities(records: dict[str, dict]) -> dict[str, dict]:
    fields = ("name", "version", "build", "subdir", "fn", "sha256")
    return {name: {field: record.get(field) for field in fields} for name, record in records.items()}


def restore(conda: str, bundle: Path, prefix: Path, fresh_cache: Path | None = None,
            recovered: tuple[Path, ...] = ()) -> dict:
    record = json.loads((bundle / "runtime.json").read_text())
    if record["schema_version"] != 1:
        raise ValueError("Unsupported runtime capture schema")
    if record["platform"] != host_platform():
        raise ValueError("Recorded target platform does not match this host")
    artifact = preserved_file(bundle, record["artifact"], "artifact")
    manifest = preserved_file(bundle, record["manifest"], "manifest")
    lines = [line.strip() for line in manifest.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if not lines or lines.pop(0) != "@EXPLICIT":
        raise ValueError("Expected a native Conda explicit manifest")
    dependencies = [explicit_identity(line, record["platform"]) for line in lines]
    expected = {item["name"]: item for item in dependencies}
    if len(expected) != len(dependencies) or "gwflow" in expected or "python" not in expected:
        raise ValueError("Dependency manifest has duplicate packages, includes gwflow, or omits Python")
    recovered_names = []
    for path in recovered:
        matches = [item for item in dependencies if item["fn"] == path.name]
        if len(matches) != 1 or path.name in recovered_names:
            raise ValueError("Recovered dependency must identify one recorded artifact")
        dependency = matches[0]
        if digest(path) != dependency["sha256"]:
            raise ValueError(f"recovered dependency SHA-256 does not match: {path.name}")
        original = dependency["url"] + "#" + dependency["sha256"]
        lines[lines.index(original)] = path.as_uri() + "#" + dependency["sha256"]
        recovered_names.append(path.name)
    artifact_identity = explicit_identity(
        f"https://preserved.invalid/{record['artifact']['subdir']}/{artifact.name}#{record['artifact']['sha256']}", record["platform"])
    if artifact_identity["name"] != "gwflow":
        raise ValueError("Preserved artifact must contain gwflow")
    expected["gwflow"] = artifact_identity
    if prefix.exists():
        raise ValueError("Restoration requires a new environment prefix")
    environment = runtime_environment()
    if fresh_cache is not None:
        fresh_cache.mkdir(parents=True, exist_ok=False)
        environment["CONDA_PKGS_DIRS"] = str(fresh_cache)
    with tempfile.TemporaryDirectory(prefix="gwflow-explicit-") as directory:
        explicit = Path(directory) / "installation.txt"
        explicit.write_text("@EXPLICIT\n" + "\n".join(lines) + "\n" + artifact.as_uri() + "#" + record["artifact"]["sha256"] + "\n")
        run(conda, "create", "--yes", "--prefix", str(prefix), "--no-default-packages", "--file", str(explicit), environment=environment)
    installed = installed_inventory(conda, prefix, record["fixture_versions"])
    if identities(installed) != identities(expected):
        raise ValueError("Restored installed runtime differs from the recorded dependency artifacts")
    evidence = {"status": "restored", "platform": record["platform"], "artifact": record["artifact"],
                "manifest": record["manifest"], "packages": identities(installed), "fresh_cache": fresh_cache is not None,
                "recovered_dependencies": recovered_names,
                "tools": {"conda": run(conda, "--version").strip(), "python": platform.python_version()}}
    (prefix / "gwflow-runtime.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"Restored and verified {len(installed)} exact Conda packages")
    return record


def runtime_files(prefix: Path, packages: dict[str, dict]) -> dict[str, str]:
    result = {}
    for package in packages.values():
        for name in package["files"]:
            path = prefix / name
            if path.is_symlink():
                result[name] = "link:" + os.readlink(path)
            elif not name.endswith(".pyc") and path.is_file():
                result[name] = digest(path)
    return result


def qualify(conda: str, bundle: Path, prefix: Path, fresh_cache: Path, source: Path) -> None:
    record = json.loads((bundle / "runtime.json").read_text())
    for name, expected in record["fixtures"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts or digest(source / name) != expected:
            raise ValueError(f"Example fixture source differs from the tested source: {name}")
    restore(conda, bundle, prefix, fresh_cache)
    before = installed_inventory(conda, prefix, record["fixture_versions"])
    before_files = runtime_files(prefix, before)
    python = str(prefix / "bin/python")
    environment = runtime_environment()
    run(python, "-I", "-c", "from pathlib import Path; import gwflow, sys; "
        "assert Path(gwflow.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), 'gwflow is outside the restored environment'",
        environment=environment)
    with tempfile.TemporaryDirectory(prefix="gwflow-fixtures-") as directory:
        clean_source = Path(directory) / "source"
        for name, expected in record["fixtures"].items():
            copied = clean_source / name
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, copied)
            if digest(copied) != expected:
                raise ValueError(f"Example fixture source changed while copying: {name}")
        wheel_directory = Path(directory) / "wheels"
        wheel_directory.mkdir()
        packages = [str(clean_source / "examples/packaged/packages" / name) for name in ("summary-task", "report-task")]
        run(python, "-m", "pip", "wheel", "--no-deps", "--disable-pip-version-check", "--wheel-dir", str(wheel_directory),
            *packages, environment=environment, show_output=True)
        wheels = sorted(wheel_directory.glob("*.whl"))
        if len(wheels) != len(FIXTURES):
            raise ValueError("Expected one wheel for each source-matched example fixture")
        run(python, "-m", "pip", "install", "--no-deps", "--no-index", "--disable-pip-version-check",
            *(str(path) for path in wheels), environment=environment, show_output=True)
    after = installed_inventory(conda, prefix, record["fixture_versions"])
    if identities(after) != identities(before) or runtime_files(prefix, after) != before_files:
        raise ValueError("Example fixture installation changed the recorded runtime dependencies")
    run(python, "-m", "pip", "check", environment=environment, show_output=True)
    run(python, str(source / "tests/release_smoke.py"), environment=environment, show_output=True)
    if identities(installed_inventory(conda, prefix, record["fixture_versions"])) != identities(before):
        raise ValueError("Release smoke tests changed the recorded runtime dependencies")
    evidence_path = prefix / "gwflow-runtime.json"
    evidence = json.loads(evidence_path.read_text())
    evidence.update(status="qualified", fixture_runtime_unchanged=True, fixture_versions=record["fixture_versions"],
                    source_commit=record["source_commit"], smoke={"command": "python tests/release_smoke.py", "exit_status": 0})
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    print("Qualified the restored runtime with unchanged dependencies and passing release smoke tests")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conda", default=os.environ.get("CONDA_EXE", "conda"))
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture", help="Capture an environment after its full package tests pass")
    capture_parser.add_argument("--prefix", type=Path, required=True)
    capture_parser.add_argument("--artifact", type=Path, required=True)
    capture_parser.add_argument("--source", type=Path, required=True)
    capture_parser.add_argument("--output", type=Path, required=True)
    restore_parser = commands.add_parser("restore", help="Restore a captured runtime without solving dependencies")
    restore_parser.add_argument("--capture", type=Path, required=True)
    restore_parser.add_argument("--prefix", type=Path, required=True)
    restore_parser.add_argument("--fresh-cache", type=Path)
    restore_parser.add_argument("--recovered", type=Path, action="append", default=[])
    qualify_parser = commands.add_parser("qualify", help="Restore with an empty cache and run the source-matched release smoke tests")
    qualify_parser.add_argument("--capture", type=Path, required=True)
    qualify_parser.add_argument("--prefix", type=Path, required=True)
    qualify_parser.add_argument("--fresh-cache", type=Path, required=True)
    qualify_parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            capture(args.conda, args.prefix.resolve(), args.artifact.resolve(), args.source.resolve(), args.output.resolve())
        elif args.command == "restore":
            restore(args.conda, args.capture.resolve(), args.prefix.resolve(), args.fresh_cache.resolve() if args.fresh_cache else None,
                    tuple(path.resolve() for path in args.recovered))
        else:
            qualify(args.conda, args.capture.resolve(), args.prefix.resolve(), args.fresh_cache.resolve(), args.source.resolve())
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Runtime preservation failed: {error}\n")


if __name__ == "__main__":
    main()
