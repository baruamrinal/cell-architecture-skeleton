"""Which version of each independently delivered service an environment runs.

``deploy/versions/<env>.yaml`` holds one ``component: tag`` line per service (tags are
``<version>-<commit>``). A service's own deploy writes it after a successful rollout;
anything that installs the service later (``make up``, ``cellctl cells deploy``) reads
it, so a new cell never gets an unreleased build. Locally the file is
``deploy/versions/local.yaml`` (git-ignored); production files are committed and
updated by each service's pipeline (stage 7).

Standard library only: ``make up`` imports it before the virtual environment exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path

LOCAL = Path("deploy") / "versions" / "local.yaml"
_LINE = re.compile(r"^([a-z][a-z0-9-]*):\s*([A-Za-z0-9._-]+)\s*$")
_VERSION = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

Git = Callable[[Sequence[str]], str]


def read_versions(root: Path) -> dict[str, str]:
    path = root / LOCAL
    if not path.is_file():
        return {}
    versions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _LINE.match(line)
        if match:
            versions[match.group(1)] = match.group(2)
    return versions


def write_version(root: Path, component: str, tag: str) -> None:
    versions = {**read_versions(root), component: tag}
    path = root / LOCAL
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by make up / make deploy-*: what this machine's clusters run."]
    lines += [f"{name}: {versions[name]}" for name in sorted(versions)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def source_version(init_file: Path) -> str:
    """The ``__version__`` a service declares in its package."""
    match = _VERSION.search(init_file.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"no __version__ in {init_file}")
    return match.group(1)


def package_version(package_json: Path) -> str:
    """The ``version`` of a Node package (web-ui)."""
    version = json.loads(package_json.read_text(encoding="utf-8")).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"no version in {package_json}")
    return version


def image_tag(version: str, git: Git, now: float) -> str:
    """``<version>-<commit>``; a changed working tree adds a timestamp, so every local
    deploy of uncommitted code gets a new tag (and its pods roll)."""
    try:
        commit = git(["git", "rev-parse", "--short", "HEAD"]).strip()
        dirty = bool(git(["git", "status", "--porcelain"]).strip())
    except OSError:
        return f"{version}-local.{int(now)}"
    tag = f"{version}-{commit}"
    return f"{tag}-dirty.{int(now)}" if dirty else tag
