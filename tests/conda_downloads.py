"""Identify and measure archive-only Conda download caches used by CI."""

import argparse
import hashlib
import json
from pathlib import Path
import platform


INPUTS = ("environment.yml", "pyproject.toml", "conda-recipe/meta.yaml", "tests/conda_downloads.py",
          "tests/prepare_ci_conda.sh", ".github/actions/cache-conda-downloads/action.yml")


def key(source: Path, profile: str, version: str, target: str, manifest: Path | None) -> str:
    inputs = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in INPUTS}
    if profile == "runtime":
        if manifest is None:
            raise ValueError("Runtime download keys require the exact explicit manifest")
        inputs["runtime.explicit.txt"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    identity = {"profile": profile, "conda": version, "platform": target, "inputs": inputs}
    return "gwflow-conda-downloads-v1-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def summary(packages: Path) -> dict:
    # Match the action's archive-only include/exclude rules. Extracted packages,
    # repository metadata, and the locally built candidate never enter the cache.
    archives = {path.name: path.stat().st_size for path in packages.glob("*")
                if path.is_file() and not path.is_symlink() and not path.name.startswith("gwflow-")
                and path.name.endswith((".conda", ".tar.bz2"))}
    return {"archive_count": len(archives), "archive_bytes": sum(archives.values()), "archives": archives}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identify = commands.add_parser("key")
    identify.add_argument("--source", type=Path, default=Path.cwd())
    identify.add_argument("--profile", choices=("build", "source", "runtime"), required=True)
    identify.add_argument("--conda-version", required=True)
    identify.add_argument("--platform", default=platform.system() + "-" + platform.machine())
    identify.add_argument("--manifest", type=Path)
    measure = commands.add_parser("summary")
    measure.add_argument("--packages", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "key":
            print(key(args.source, args.profile, args.conda_version, args.platform, args.manifest))
        else:
            print(json.dumps(summary(args.packages), sort_keys=True))
    except (OSError, ValueError) as error:
        parser.exit(1, f"Conda download cache failed: {error}\n")


if __name__ == "__main__":
    main()
