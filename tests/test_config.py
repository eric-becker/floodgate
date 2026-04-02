"""Tests for configuration loading and channel policy logic."""

import yaml

from floodgate.config import load_config, should_zerohop


class TestShouldZerohop:

    def test_whitelist_empty_zerohops_all(self):
        config = {"channel_policy": "whitelist", "_whitelist_set": set(), "_blacklist_set": set()}
        assert should_zerohop(config, "LongFast") is True
        assert should_zerohop(config, "MyPrivateChannel") is True

    def test_whitelist_with_entries_passes_through(self):
        config = {
            "channel_policy": "whitelist",
            "_whitelist_set": {"PrivateChannel", "MyGroup"},
            "_blacklist_set": set(),
        }
        assert should_zerohop(config, "LongFast") is True
        assert should_zerohop(config, "PrivateChannel") is False
        assert should_zerohop(config, "MyGroup") is False

    def test_blacklist_zerohops_listed(self):
        config = {
            "channel_policy": "blacklist",
            "_whitelist_set": set(),
            "_blacklist_set": {"LongFast", "ShortFast"},
        }
        assert should_zerohop(config, "LongFast") is True
        assert should_zerohop(config, "MyPrivate") is False

    def test_blacklist_empty_zerohops_none(self):
        config = {"channel_policy": "blacklist", "_whitelist_set": set(), "_blacklist_set": set()}
        assert should_zerohop(config, "LongFast") is False

    def test_unknown_policy_defaults_to_zerohop(self):
        config = {"channel_policy": "invalid", "_whitelist_set": set(), "_blacklist_set": set()}
        assert should_zerohop(config, "anything") is True


class TestLoadConfig:

    def test_default_config(self):
        config = load_config(None)
        assert config["channel_policy"] == "blacklist"
        assert config["channel_whitelist"] == []
        assert len(config["channel_blacklist"]) == 8
        assert config["grpc_port"] == 9000
        assert config["stats_interval_s"] == 60

    def test_load_from_file(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"channel_policy": "blacklist", "grpc_port": 8080}))
        config = load_config(str(cfg_file))
        assert config["channel_policy"] == "blacklist"
        assert config["grpc_port"] == 8080
        assert "channel_whitelist" in config
        assert "stats_interval_s" in config

    def test_load_from_env(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"channel_policy": "blacklist"}))
        monkeypatch.setenv("FLOODGATE_CONFIG", str(cfg_file))
        config = load_config(None)
        assert config["channel_policy"] == "blacklist"

    def test_missing_file_uses_defaults(self):
        config = load_config("/nonexistent/config.yaml")
        assert config["channel_policy"] == "blacklist"

    def test_sets_are_computed(self):
        config = load_config(None)
        assert isinstance(config["_whitelist_set"], set)
        assert isinstance(config["_blacklist_set"], set)

    def test_stats_interval_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"stats_interval_s": 300}))
        config = load_config(str(cfg_file))
        assert config["stats_interval_s"] == 300

    def test_default_log_format(self):
        config = load_config(None)
        assert config["log_format"] == "text"

    def test_log_format_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"log_format": "json"}))
        config = load_config(str(cfg_file))
        assert config["log_format"] == "json"

    def test_log_format_env_overrides_yaml(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"log_format": "text"}))
        monkeypatch.setenv("FLOODGATE_LOG_FORMAT", "json")
        config = load_config(str(cfg_file))
        assert config["log_format"] == "json"
