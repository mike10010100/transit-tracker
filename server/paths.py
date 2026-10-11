"""Filesystem locations shared by the server modules.

Release artifacts (the OTA binary, its manifest and the server identity files)
live "next to tracker-arm": the ``server/`` directory first, then the
repository root. In Docker both resolve to ``/app/``.

Mutable runtime state (the GTFS index and the control token) lives in
``CACHE_DIR``: the ``CACHE_DIR`` env var if set, else ``/app/cache`` when that
directory exists or can be created, else ``<repo>/cache``.
"""

import os
from typing import Optional

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SERVER_DIR)
DOCKER_CACHE_DIR = "/app/cache"


def artifact_candidates(name: str) -> list[str]:
    """Search order for a release artifact: server/ first, then the repo root."""
    return [os.path.join(SERVER_DIR, name), os.path.join(REPO_DIR, name)]


def find_artifact(name: str) -> Optional[str]:
    """Returns the first existing candidate path for ``name``, or None."""
    for path in artifact_candidates(name):
        if os.path.exists(path):
            return path
    return None


def resolve_cache_dir() -> str:
    """Resolves CACHE_DIR (see module docstring). Never raises."""
    env = os.environ.get("CACHE_DIR", "").strip()
    if env:
        return env
    try:
        os.makedirs(DOCKER_CACHE_DIR, exist_ok=True)
        if os.access(DOCKER_CACHE_DIR, os.W_OK):
            return DOCKER_CACHE_DIR
    except OSError:
        pass
    return os.path.join(REPO_DIR, "cache")


def resolve_dashboards_dir() -> str:
    """Resolves DASHBOARDS_DIR. Never raises."""
    env = os.environ.get("DASHBOARDS_DIR", "").strip()
    if env:
        return env
    docker_dash_dir = "/app/config/dashboards"
    try:
        os.makedirs(docker_dash_dir, exist_ok=True)
        if os.access(docker_dash_dir, os.W_OK):
            return docker_dash_dir
    except OSError:
        pass
    repo_dash_dir = os.path.join(REPO_DIR, "config", "dashboards")
    try:
        os.makedirs(repo_dash_dir, exist_ok=True)
    except OSError:
        pass
    return repo_dash_dir
