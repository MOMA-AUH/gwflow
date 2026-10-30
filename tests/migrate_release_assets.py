"""Repackage a mutable pre-1.0 release after preserving its original assets."""

import argparse
import json
from pathlib import Path
import tempfile
import tarfile

from conda_runtime import digest
from release_artifacts import archive_name, asset_names, final_tag, github, pack, stable_tag, unpack, verify, verify_publication


def release_state(gh, repo, tag):
    state = json.loads(github(gh, "api", f"repos/{repo}/releases/tags/{tag}").stdout)
    if (state["tag_name"] != tag or state["draft"] or state["prerelease"]
            or state.get("immutable") is not False):
        raise ValueError("Migration requires a mutable, published, non-prerelease release")
    return state


def download(gh, repo, tag, name, destination):
    with tempfile.TemporaryDirectory(prefix="gwflow-asset-download-") as temporary:
        github(gh, "release", "download", tag, "--repo", repo, "--pattern", name, "--dir", temporary)
        path = destination / name
        pending = path.with_name(path.name + ".tmp")
        pending.write_bytes((Path(temporary) / name).read_bytes())
        pending.replace(path)
    return path


def migrate(args):
    archive = archive_name(args.tag)
    if not final_tag(args.tag):
        raise ValueError("Migration requires a final pre-1.0 tag")
    if stable_tag(args.tag):
        raise ValueError("Published stable baselines cannot be repackaged")
    state = release_state(args.gh, args.repo, args.tag)
    args.backup.mkdir(parents=True, exist_ok=True)
    receipt = args.backup / "original-release.json"
    if receipt.exists():
        original = json.loads(receipt.read_text())
        if original["id"] != state["id"] or original["tag_name"] != args.tag:
            raise ValueError("Backup belongs to a different release")
    else:
        receipt.write_text(json.dumps(state, indent=2) + "\n")
        original = state
    backup = args.backup / "original-assets"
    backup.mkdir(exist_ok=True)
    original_names = {asset["name"] for asset in original["assets"]}
    # This operation deliberately accepts only the old seven-file layout.
    metadata = {"runtime.explicit.txt", "runtime.json", "qualification.json", "release.json", "publication.json", "SHA256SUMS"}
    packages = original_names - metadata
    if len(packages) != 1 or len(original_names) != 7:
        raise ValueError("Expected the original seven release assets in the backup")
    for name in original_names:
        if Path(name).name != name or "\\" in name:
            raise ValueError("Unsafe original asset filename")
        if not (backup / name).exists():
            download(args.gh, args.repo, args.tag, name, backup)
    release = verify(backup, args.tag, args.source)
    verify_publication(release, json.loads((backup / "publication.json").read_text()))
    if original_names != asset_names(release) | {"SHA256SUMS", "publication.json"}:
        raise ValueError("Original release asset set differs from its qualified records")
    for asset in original["assets"]:
        if asset.get("digest") != "sha256:" + digest(backup / asset["name"]):
            raise ValueError("Original asset differs from its saved GitHub digest")
    compact = args.backup / "compact-assets"
    if not compact.exists():
        pack(backup, backup / "publication.json", compact, args.tag, args.source)
    with tempfile.TemporaryDirectory(prefix="gwflow-migration-check-") as temporary:
        restored = Path(temporary) / "restored"
        unpack(compact, restored, args.tag, args.source)
        for name in original_names:
            if (restored / name).read_bytes() != (backup / name).read_bytes():
                raise ValueError("Archive does not preserve the original bytes")
    notes = args.notes_file.read_text()
    obsolete = metadata - {"SHA256SUMS"}
    for name in obsolete:
        if f"/releases/download/{args.tag}/{name}" in notes:
            raise ValueError("Release notes still link to a metadata asset being removed")
    if not notes.strip():
        raise ValueError("Release notes must not be empty")
    desired = {release["artifact"]["filename"], archive, "SHA256SUMS"}
    current = {asset["name"] for asset in state["assets"]}
    if current - (original_names | {archive}) or release["artifact"]["filename"] not in current:
        raise ValueError("Published release assets changed outside this migration")
    # Recheck every surviving remote file before overwriting checksums or deleting metadata.
    current_hashes = {}
    with tempfile.TemporaryDirectory(prefix="gwflow-migration-current-") as temporary:
        for name in current:
            downloaded = download(args.gh, args.repo, args.tag, name, Path(temporary))
            expected = {digest(compact / name)} if name in desired else {digest(backup / name)}
            if name == "SHA256SUMS":
                expected.add(digest(backup / name))
            current_hashes[name] = digest(downloaded)
            if current_hashes[name] not in expected:
                raise ValueError(f"Published asset changed; refusing replacement: {name}")
    print("Release assets: " + ", ".join(sorted(desired)))
    if not args.apply:
        print("Verified backup and archive; preview only. Use --apply with this backup to migrate.")
        return
    if archive not in current:
        github(args.gh, "release", "upload", args.tag, str(compact / archive), "--repo", args.repo)
    with tempfile.TemporaryDirectory(prefix="gwflow-migration-upload-") as temporary:
        uploaded = download(args.gh, args.repo, args.tag, archive, Path(temporary))
        if digest(uploaded) != digest(compact / archive):
            raise ValueError("Uploaded archive differs; old metadata retained")
    if current_hashes.get("SHA256SUMS") != digest(compact / "SHA256SUMS"):
        github(args.gh, "release", "upload", args.tag, str(compact / "SHA256SUMS"), "--repo", args.repo, "--clobber")
    with tempfile.TemporaryDirectory(prefix="gwflow-migration-checksum-") as temporary:
        uploaded = download(args.gh, args.repo, args.tag, "SHA256SUMS", Path(temporary))
        if digest(uploaded) != digest(compact / "SHA256SUMS"):
            raise ValueError("Uploaded checksums differ; old metadata retained")
    github(args.gh, "release", "edit", args.tag, "--repo", args.repo, "--notes-file", str(args.notes_file))
    updated = release_state(args.gh, args.repo, args.tag)
    if updated["body"].replace("\r\n", "\n").strip() != notes.strip():
        raise ValueError("Release notes were not updated; old metadata retained")
    for name in sorted(obsolete & {asset["name"] for asset in updated["assets"]}):
        github(args.gh, "release", "delete-asset", args.tag, name, "--repo", args.repo, "--yes")
    final = release_state(args.gh, args.repo, args.tag)
    if {asset["name"] for asset in final["assets"]} != desired:
        raise ValueError("Migration incomplete; rerun with the same backup")
    with tempfile.TemporaryDirectory(prefix="gwflow-migration-final-") as temporary:
        downloaded = Path(temporary) / "assets"
        downloaded.mkdir()
        for name in desired:
            download(args.gh, args.repo, args.tag, name, downloaded)
        unpack(downloaded, Path(temporary) / "verified", args.tag, args.source)
    print("Migrated and verified release assets; original package, records, and notes retained in backup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--notes-file", required=True, type=Path)
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        migrate(args)
    except (ValueError, OSError, KeyError, tarfile.TarError, EOFError) as error:
        parser.exit(1, f"Release migration failed: {error}\n")


if __name__ == "__main__":
    main()
