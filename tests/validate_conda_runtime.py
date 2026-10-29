"""Qualify real Conda restoration and controlled download failures after full tests."""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading


TOOL = Path(__file__).with_name("conda_runtime.py")


def command(conda: str, root: Path, name: str, *arguments: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    result = subprocess.run([sys.executable, str(TOOL), "--conda", conda, *arguments],
                            capture_output=True, text=True, env=environment)
    (root / f"{name}.log").write_text(result.stdout + result.stderr)
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result


def failed_downloads(conda: str, bundle: Path, root: Path) -> dict:
    original = json.loads((bundle / "runtime.json").read_text())
    manifest = (bundle / original["manifest"]["filename"]).read_text()
    python_entry = next(line for line in manifest.splitlines() if "/python-" in line)
    filename, checksum = python_entry.rsplit("/", 1)[1].split("#")
    requests = []
    response = {"status": 404}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            content = b"deliberately corrupt dependency artifact"
            self.send_response(response["status"])
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = {}
    try:
        for name, status, expected_message in (("missing-download", 404, "404"), ("corrupt-download", 200, "ChecksumMismatch")):
            response["status"] = status
            requests.clear()
            fixture = root / name
            fixture.mkdir()
            shutil.copy(bundle / original["artifact"]["filename"], fixture)
            controlled = (f"# platform: {original['platform']}\n@EXPLICIT\n"
                          f"http://127.0.0.1:{server.server_port}/{original['platform']}/{filename}#{checksum}\n")
            (fixture / original["manifest"]["filename"]).write_text(controlled)
            record = {**original, "manifest": {**original["manifest"], "sha256": hashlib.sha256(controlled.encode()).hexdigest()}}
            (fixture / "runtime.json").write_text(json.dumps(record))
            prefix = root / (name + "-environment")
            result = command(conda, root, name, "restore", "--capture", str(fixture), "--prefix", str(prefix),
                             "--fresh-cache", str(root / (name + "-cache")))
            if (result.returncode == 0 or expected_message not in result.stderr or not requests
                    or (prefix / "gwflow-runtime.json").exists()):
                raise ValueError(f"Controlled {name} did not fail through the expected Conda download check")
            evidence[name] = {"nonzero_exit": True, "download_observed": True, "verified_environment_created": False}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conda", default=os.environ.get("CONDA_EXE", "conda"))
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        evidence = failed_downloads(args.conda, args.capture.resolve(), root)
        result = command(args.conda, root, "qualification", "qualify", "--capture", str(args.capture.resolve()),
                         "--prefix", str(root / "restored"), "--fresh-cache", str(root / "fresh-cache"),
                         "--source", str(args.source.resolve()))
        if result.returncode:
            raise ValueError("Restoration qualification failed; inspect qualification.log")
        restored = json.loads((root / "restored/gwflow-runtime.json").read_text())
        if restored["status"] != "qualified" or not restored["fresh_cache"] or not restored["fixture_runtime_unchanged"]:
            raise ValueError("Restoration did not report all required qualification checks")
        evidence["qualification"] = restored
        (root / "summary.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print("Real Conda capture, controlled download failures, fresh restoration, and release smoke tests passed")
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Runtime acceptance failed: {error}\n")


if __name__ == "__main__":
    main()
