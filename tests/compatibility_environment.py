"""Inspect installed compatibility runtimes without inherited import overrides."""

import argparse
import json
from pathlib import Path
import subprocess

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name


PROBE = """
import hashlib, importlib.metadata as m, importlib.util, json, os, pathlib, platform, sys, sysconfig
import gwflow
package = pathlib.Path(gwflow.__file__).parent
digest = hashlib.sha256()
for path in sorted(package.rglob('*.py')):
    digest.update(str(path.relative_to(package)).encode())
    digest.update(path.read_bytes())
distribution = m.distribution('gwflow')
distributions = [{'name': d.metadata['Name'], 'version': d.version,
                  'location': str(d.locate_file('').resolve()),
                  'requires_python': d.metadata.get('Requires-Python'),
                  'requires': d.requires or []} for d in m.distributions()]
imports = {}
for name in m.packages_distributions():
    if name in sys.stdlib_module_names or '.' in name:
        continue
    loaded = sys.modules.get(name)
    if loaded is not None and getattr(loaded, '__spec__', None) is None:
        imports[name] = [loaded.__file__] if getattr(loaded, '__file__', None) else []
        continue
    spec = importlib.util.find_spec(name)
    if spec is not None:
        imports[name] = ([spec.origin] if spec.origin and spec.origin not in ('built-in', 'frozen') else [])
        imports[name] += list(spec.submodule_search_locations or [])
print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
    'prefix_identity': {'device': pathlib.Path(sys.prefix).stat().st_dev, 'inode': pathlib.Path(sys.prefix).stat().st_ino},
    'python': sys.version, 'python_version': platform.python_version(), 'platform': platform.platform(),
    'stdlib': sysconfig.get_path('stdlib'), 'sys_path': sys.path, 'package': str(package),
    'package_sha256': digest.hexdigest(), 'distributions': distributions, 'imports': imports,
    'versions': {d['name']: m.version(d['name']) for d in distributions},
    'markers': {'implementation_name': sys.implementation.name, 'implementation_version': platform.python_version(),
                'os_name': os.name, 'platform_machine': platform.machine(), 'platform_release': platform.release(),
                'platform_system': platform.system(), 'platform_version': platform.version(),
                'python_full_version': platform.python_version(), 'python_version': '.'.join(platform.python_version_tuple()[:2]),
                'platform_python_implementation': platform.python_implementation(), 'sys_platform': sys.platform},
    'direct_url': json.loads(distribution.read_text('direct_url.json') or 'null')}))
"""


def supported_runtime(result: dict) -> None:
    installed: dict[str, dict] = {}
    for item in result["distributions"]:
        installed.setdefault(canonicalize_name(item["name"]), item)
    pending: list[tuple[str, frozenset[str]]] = [("gwflow", frozenset())]
    visited = set()
    while pending:
        name, extras = pending.pop()
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        package = installed[name]
        if package["requires_python"] and result["python_version"] not in SpecifierSet(package["requires_python"]):
            raise ValueError(f"Python {result['python_version']} does not satisfy {name}'s {package['requires_python']}")
        for value in package["requires"]:
            requirement = Requirement(value)
            if requirement.marker and not any(requirement.marker.evaluate({**result["markers"], "extra": extra})
                                              for extra in (extras or {""})):
                continue
            dependency = canonicalize_name(requirement.name)
            actual = installed.get(dependency)
            if actual is None or actual["version"] not in requirement.specifier:
                raise ValueError(f"Installed {dependency} does not satisfy {name}'s {requirement}")
            pending.append((dependency, frozenset(requirement.extras)))


def installed_environment(python: Path, source: str, independent: bool = False) -> dict:
    observation = subprocess.run([str(python), "-I", "-c", PROBE], capture_output=True, text=True)
    if observation.returncode:
        raise ValueError(f"Cannot inspect {python}: {observation.stderr.strip()}")
    result = json.loads(observation.stdout)
    prefix = Path(result["prefix"]).resolve()
    if not Path(result["package"]).resolve().is_relative_to(prefix):
        raise ValueError(f"gwflow must be independently installed inside {prefix}")
    supported_runtime(result)
    if independent:
        paths = [item["location"] for item in result["distributions"]]
        paths += [path for paths in result["imports"].values() for path in paths]
        paths += [result["stdlib"], *result["sys_path"]]
        for path in paths:
            if not Path(path).resolve().is_relative_to(prefix):
                raise ValueError(f"Runtime resolves outside its own environment: {path} (prefix {prefix})")
    return {"source": source, "supported_runtime": True,
            "dependency_policy": "independent" if independent else "provisional-shared-runtime", **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--independent", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(installed_environment(args.python, args.source, args.independent), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Compatibility environment rejected: {error}\n")


if __name__ == "__main__":
    main()
