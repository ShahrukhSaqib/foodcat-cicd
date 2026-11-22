"""Tests for the CI/CD part: the version that the live site shows, the build zip, the launcher
settings that the AWS server uses, and the files that the server installs."""
import json
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import run_local
from deploy import build as builder
from foodcat.version import build_info, read_version

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
FIXED_TIME = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)


def small_project(root, version="2.5"):
    """A tiny copy of the project layout, enough for the build script."""
    root.mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "requirements.txt").write_text("Flask\n", encoding="utf-8")
    (root / "run_local.py").write_text("print('hi')\n", encoding="utf-8")
    for folder in ("foodcat", "web", "deploy"):
        (root / folder).mkdir()
    (root / "foodcat" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "web" / "app.js").write_text("// page\n", encoding="utf-8")
    (root / "deploy" / "install_build.sh").write_text("echo hi\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- the version on the live site

def test_the_version_file_holds_a_plain_version_number():
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", read_version())


def test_a_copy_that_was_never_built_calls_itself_local(tmp_path):
    (tmp_path / "VERSION").write_text("2.5\n", encoding="utf-8")
    assert build_info(tmp_path) == {"version": "2.5", "build": "local", "commit": "", "built_at": "", "build_name": "local"}


def test_the_build_file_says_which_build_is_running(tmp_path):
    (tmp_path / "VERSION").write_text("2.5\n", encoding="utf-8")
    saved = {"version": "2.6", "build": "41", "commit": "abc1234", "built_at": "2026-11-22T10:00:00Z", "build_name": "build-v2.6-41"}
    (tmp_path / "build-info.json").write_text(json.dumps(saved), encoding="utf-8")
    assert build_info(tmp_path) == saved


def test_a_broken_build_file_or_a_missing_version_does_not_stop_the_site(tmp_path):
    (tmp_path / "build-info.json").write_text("{not json", encoding="utf-8")
    assert build_info(tmp_path)["version"] == "unknown" and build_info(tmp_path)["build"] == "local"


def test_the_gateway_tells_everybody_which_build_is_live(stack):
    reply = stack.client.get("/api/version")  # no login needed
    assert reply.status_code == 200
    assert set(reply.get_json()) == {"version", "build", "commit", "built_at", "build_name"}
    assert reply.get_json()["version"] == read_version()
    assert reply.headers["Cache-Control"] == "no-store"


def test_the_page_prints_the_live_version_as_plain_text():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'api("GET", "/version")' in script and "line.textContent = versionText()" in script
    assert script.count('class: "version"') + script.count('class: "foot-version version"') == 2
    assert ".foot-version" in (ROOT / "web" / "style.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the build zip (step 5)

def test_the_build_zip_holds_what_the_server_needs_and_nothing_else(tmp_path):
    target, info = builder.build(tmp_path, "7", "0123456789abcdef", now=FIXED_TIME)
    version = read_version()
    assert target.name == "build-v%s-7.zip" % version
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        packed_info = json.loads(archive.read("build-info.json"))
    for needed in ("run_local.py", "requirements.txt", "VERSION", "build-info.json", "foodcat/gateway.py",
                   "foodcat/version.py", "web/index.html", "web/app.js", "deploy/install_build.sh",
                   "deploy/pull_latest_build.sh", "deploy/foodcat.service"):
        assert needed in names, needed
    for name in names:
        assert not name.startswith(("tests/", ".github/", "data/", ".git", "dist/")), name
        assert "__pycache__" not in name and not name.endswith((".pyc", ".db", ".zip")) and "\\" not in name, name
    assert packed_info == info == {"version": version, "build": "7", "commit": "0123456",
                                   "built_at": "2026-11-22T10:00:00Z", "build_name": "build-v%s-7" % version}


def test_the_build_leaves_out_caches_and_database_files(tmp_path):
    root = small_project(tmp_path / "project")
    (root / "foodcat" / "__pycache__").mkdir()
    (root / "foodcat" / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0")
    (root / "foodcat" / "orders.db").write_bytes(b"\0")
    target, _ = builder.build(tmp_path / "out", "3", root=root)
    with zipfile.ZipFile(target) as archive:
        assert sorted(archive.namelist()) == ["VERSION", "build-info.json", "deploy/install_build.sh", "foodcat/app.py",
                                              "requirements.txt", "run_local.py", "web/app.js"]


def test_code_that_does_not_compile_never_reaches_s3(tmp_path):
    root = small_project(tmp_path / "project")
    (root / "foodcat" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="does not compile"):
        builder.build(tmp_path / "out", "3", root=root)
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


@pytest.mark.parametrize("version, number, commit, problem", [
    ("1.x", "3", "", "VERSION must look like"),
    ("1.0", "3; rm -rf /", "", "whole number"),
    ("1.0", "3", "not-a-commit", "does not look like a git commit"),
])
def test_the_build_refuses_odd_input(tmp_path, version, number, commit, problem):
    root = small_project(tmp_path / "project", version=version)
    with pytest.raises(SystemExit, match=problem):
        builder.build(tmp_path / "out", number, commit, root=root)


def test_the_build_tells_github_actions_the_names_it_used(tmp_path, monkeypatch, capsys):
    outputs = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    builder.main(["--build-number", "12", "--commit", "abcdef1234567", "--out", str(tmp_path / "dist")])
    name = "build-v%s-12" % read_version()
    assert outputs.read_text(encoding="utf-8").splitlines() == ["zip_name=%s.zip" % name, "build_name=%s" % name]
    assert "Built " in capsys.readouterr().out
    assert (tmp_path / "dist" / (name + ".zip")).is_file()


# ---------------------------------------------------------------- the server (steps 7 and 8)

def test_the_launcher_stays_on_this_computer_unless_told_otherwise():
    args = run_local.parse_args([])
    assert (args.host, args.port, Path(args.data_dir)) == ("127.0.0.1", 8000, ROOT / "data")


def test_only_the_gateway_moves_to_the_public_port():
    programs = run_local.programs_for(80)
    assert programs[0] == ("gateway", 80, "gateway")
    assert programs[1:] == run_local.PROGRAMS[1:]  # the services behind it keep their private ports


def test_the_service_file_and_the_launcher_agree():
    unit = (DEPLOY / "foodcat.service").read_text(encoding="utf-8")
    assert "\nUser=foodcat\n" in unit and "\nAmbientCapabilities=CAP_NET_BIND_SERVICE\n" in unit
    assert "\nEnvironmentFile=/etc/foodcat/foodcat.env\n" in unit and "\nNoNewPrivileges=true\n" in unit
    command = shlex.split(re.search(r"^ExecStart=(.+)$", unit, re.M).group(1))
    assert command[:2] == ["/opt/foodcat/current/.venv/bin/python", "run_local.py"]
    args = run_local.parse_args(command[2:])
    assert (args.host, args.port, args.data_dir, args.fresh) == ("0.0.0.0", 80, "/var/lib/foodcat", False)


def test_the_pipeline_fills_in_both_settings_of_the_pull_script():
    script = (DEPLOY / "pull_latest_build.sh").read_text(encoding="utf-8").splitlines()
    assert script.count("export AWS_DEFAULT_REGION=''") == 1 and script.count("BUCKET=''") == 1
    workflow = (ROOT / ".github" / "workflows" / "cicd.yml").read_text(encoding="utf-8")
    assert "s|^export AWS_DEFAULT_REGION=''$|" in workflow and "s|^BUCKET=''$|" in workflow


def test_the_health_check_reads_the_same_name_that_the_build_writes():
    script = (DEPLOY / "install_build.sh").read_text(encoding="utf-8")
    assert "CHECK_URL=http://127.0.0.1/api/version" in script and '["build_name"]' in script
    assert '.get("build_name")' in script


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs a Linux shell")
@pytest.mark.parametrize("name", ["pull_latest_build.sh", "install_build.sh"])
def test_the_server_scripts_are_valid_bash(name):
    result = subprocess.run(["bash", "-n", str(DEPLOY / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
