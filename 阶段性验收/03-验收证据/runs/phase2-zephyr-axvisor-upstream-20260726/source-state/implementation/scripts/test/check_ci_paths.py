#!/usr/bin/env python3

import re
import sys
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_MANIFEST = WORKSPACE_ROOT / "Cargo.toml"
CI_WORKFLOW = WORKSPACE_ROOT / ".github/workflows/ci.yml"
REQUIRED_CI_PATHS = {"configs/**"}


def main() -> int:
    source_roots = workspace_source_roots()
    ci_paths = ci_check_paths()
    expected_paths = {f"{root}/**" for root in source_roots} | REQUIRED_CI_PATHS
    missing_paths = sorted(path for path in expected_paths if path not in ci_paths)

    if not missing_paths:
        return 0

    print("CI path filter is missing workspace source directories:", file=sys.stderr)
    for path in missing_paths:
        print(f"  - {path}", file=sys.stderr)
    return 1


def workspace_source_roots() -> set[str]:
    manifest = WORKSPACE_MANIFEST.read_text(encoding="utf-8")
    members = manifest.split("members = [", maxsplit=1)[1].split("]", maxsplit=1)[0]
    package_paths = re.findall(r'^\s+"([^"]+)",?$', members, flags=re.MULTILINE)
    package_paths.extend(re.findall(r'\bpath\s*=\s*"([^"]+)"', manifest))
    return {Path(package_path).parts[0] for package_path in package_paths}


def ci_check_paths() -> set[str]:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    ci_checks = workflow.split("            ci_checks:\n", maxsplit=1)[1].split(
        "            base_container_publish:\n", maxsplit=1
    )[0]
    return set(re.findall(r'^\s+- "([^"]+)"$', ci_checks, flags=re.MULTILINE))


if __name__ == "__main__":
    sys.exit(main())
