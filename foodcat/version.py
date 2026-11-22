"""Which build of Food Cat is running.

The pipeline puts a small build-info.json into every build zip (see deploy/build.py). The gateway
shows it at /api/version, the web page prints it at the bottom, and the deploy script on the server
checks it after a restart. Hence everybody can see that production really runs the new build.

A copy that was never built by the pipeline (for example on your own computer) has no build-info.json,
so it falls back to the VERSION file and calls itself 'local'.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("version", "build", "commit", "built_at", "build_name")


def read_version(root=ROOT):
    """The version number in the VERSION file, for example '1.0'."""
    try:
        text = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


def build_info(root=ROOT):
    """Version, build number, commit, build time and build name of this copy of Food Cat."""
    root = Path(root)
    info = {"version": read_version(root), "build": "local", "commit": "", "built_at": "", "build_name": "local"}
    try:
        saved = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    if isinstance(saved, dict):
        for field in FIELDS:
            if field in saved:
                info[field] = str(saved[field])
    return info
