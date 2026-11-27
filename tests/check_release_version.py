"""Check that a requested gwflow version follows the latest published final release."""

import argparse
import re


NUMBER = r"(?:0|[1-9][0-9]*)"
FINAL_TAG = rf"v{NUMBER}\.{NUMBER}\.{NUMBER}"
# A Python package prerelease can also be a development build, e.g. rc1.dev2.
RELEASE_TAG = rf"({FINAL_TAG})(?:(?:a|b|rc){NUMBER})?(?:\.dev{NUMBER})?"


def check_release_version(latest, requested):
    if not re.fullmatch(FINAL_TAG, latest):
        raise ValueError(f"Expected a canonical published final tag, got {latest!r}")
    major, minor, patch = map(int, latest[1:].split("."))
    allowed = (f"v{major}.{minor}.{patch + 1}", f"v{major}.{minor + 1}.0", f"v{major + 1}.0.0")
    candidate = re.fullmatch(RELEASE_TAG, requested)
    if candidate is None or candidate[1] not in allowed:
        raise ValueError(f"{requested!r} cannot follow {latest}. Next release versions are {', '.join(allowed)}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("latest", help="latest published final tag")
    parser.add_argument("requested", help="next patch, minor, or major tag, optionally with aN, bN, rcN or .devN")
    args = parser.parse_args()
    try:
        check_release_version(args.latest, args.requested)
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
