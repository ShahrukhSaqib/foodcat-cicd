"""The build step of the pipeline: package Food Cat into one zip, ready for S3.

    python deploy/build.py --build-number 7 --commit 1a2b3c4d5e   (what GitHub Actions runs)
    python deploy/build.py                                         (on your own computer, build 'local')

The zip is called build-v<VERSION>-<build number>.zip, for example build-v1.1-7.zip. It holds only
what the server needs: the code, the web page, the launcher, requirements.txt, the deploy scripts and
a small build-info.json that says which version, build and commit it is. Tests, git files and local
data stay out. Every Python file is compiled first, so code that cannot even start never reaches S3.
"""
import argparse
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["foodcat", "web", "deploy", "run_local.py", "requirements.txt", "VERSION"]
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".db", ".db-wal", ".db-shm", ".zip"}


def files_to_pack(root=ROOT):
    """Every file that goes into the zip, in a fixed order."""
    root = Path(root)
    for entry in INCLUDE:
        path = root / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            for item in sorted(path.rglob("*")):
                parts = set(item.relative_to(root).parts)
                if item.is_file() and not parts & SKIP_DIRS and item.suffix not in SKIP_SUFFIXES:
                    yield item
        else:
            raise SystemExit("Build stopped: %s is missing." % entry)


def check_python(files):
    """Compile every Python file in memory. A syntax error stops the build here."""
    for path in files:
        if path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except SyntaxError as problem:
                raise SystemExit("Build stopped: %s does not compile (%s)." % (path, problem))


def build(out_dir="dist", build_number="local", commit="", root=ROOT, now=None):
    """Make the zip and return (path of the zip, build information)."""
    root = Path(root)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+(\.\d+){1,2}", version):
        raise SystemExit("Build stopped: VERSION must look like 1.0 or 1.0.1, not %r." % version)
    build_number = str(build_number)
    if not re.fullmatch(r"\d+|local", build_number):
        raise SystemExit("Build stopped: the build number must be a whole number, not %r." % build_number)
    commit = (commit or "")[:7]
    if commit and not re.fullmatch(r"[0-9a-f]{7}", commit):
        raise SystemExit("Build stopped: %r does not look like a git commit." % commit)

    files = list(files_to_pack(root))
    check_python(files)

    name = "build-v%s-%s" % (version, build_number)
    info = {
        "version": version,
        "build": build_number,
        "commit": commit,
        "built_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build_name": name,
    }
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / (name + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())  # forward slashes, even on Windows
        archive.writestr("build-info.json", json.dumps(info, indent=2) + "\n")
    return target, info


def main(argv=None):
    parser = argparse.ArgumentParser(description="Package Food Cat into build-v<VERSION>-<build>.zip")
    parser.add_argument("--build-number", default="local", help="the GitHub Actions run number")
    parser.add_argument("--commit", default="", help="the git commit that is being built")
    parser.add_argument("--out", default="dist", help="folder for the zip (default: dist)")
    args = parser.parse_args(argv)

    target, info = build(args.out, args.build_number, args.commit)
    with zipfile.ZipFile(target) as archive:
        count = len(archive.namelist())
    print("Built %s (%d files, %.0f KB)" % (target.as_posix(), count, target.stat().st_size / 1024))
    print("Version %s, build %s, commit %s" % (info["version"], info["build"], info["commit"] or "none"))

    # GitHub Actions reads these values to name the artifact and to check the live site later.
    outputs = os.environ.get("GITHUB_OUTPUT")
    if outputs:
        with open(outputs, "a", encoding="utf-8") as handle:
            handle.write("zip_name=%s\n" % target.name)
            handle.write("build_name=%s\n" % info["build_name"])


if __name__ == "__main__":
    main()
