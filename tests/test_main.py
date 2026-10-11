"""Tests for the command-line entry point."""

import subprocess
import sys

import yaml


def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "floodgate", *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_bad_config_exits_2_with_a_clean_message(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.dump({"drop_enabled": "false", "zerohop_channel": []}))
    result = _run("--config", str(cfg))
    assert result.returncode == 2
    assert "zerohop_channel" in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_config_file_exits_2(tmp_path):
    result = _run("--config", str(tmp_path / "absent.yaml"))
    assert result.returncode == 2
    assert "not found" in result.stderr
    assert "Traceback" not in result.stderr


def test_version_flag():
    from floodgate import __version__

    result = _run("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"floodgate {__version__}"
