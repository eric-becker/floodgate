"""Tests for configuration loading, schema validation, and policy evaluation."""

import pytest
import yaml

from floodgate.config import (
    INHERIT_ZEROHOP_CHANNELS,
    ConfigError,
    load_config,
    should_drop,
    should_zerohop,
)

# ---------------------------------------------------------------------------
# should_zerohop
# ---------------------------------------------------------------------------

class TestShouldZerohop:

    def test_enabled_and_channel_in_set_zerohops(self):
        config = {
            "zerohop_enabled": True,
            "_zerohop_channels_set": {"LongFast", "ShortFast"},
        }
        assert should_zerohop(config, "LongFast") is True
        assert should_zerohop(config, "ShortFast") is True

    def test_enabled_but_channel_not_in_set_passes_through(self):
        config = {
            "zerohop_enabled": True,
            "_zerohop_channels_set": {"LongFast"},
        }
        assert should_zerohop(config, "MyPrivate") is False

    def test_disabled_never_zerohops(self):
        config = {
            "zerohop_enabled": False,
            "_zerohop_channels_set": {"LongFast"},
        }
        assert should_zerohop(config, "LongFast") is False

    def test_empty_channel_set_never_zerohops(self):
        config = {"zerohop_enabled": True, "_zerohop_channels_set": set()}
        assert should_zerohop(config, "LongFast") is False

    def test_default_enabled_when_key_missing(self):
        # Belt and suspenders: should_zerohop defaults zerohop_enabled to True
        # so a malformed config still applies the channel filter.
        config = {"_zerohop_channels_set": {"LongFast"}}
        assert should_zerohop(config, "LongFast") is True


# ---------------------------------------------------------------------------
# should_drop
# ---------------------------------------------------------------------------

class TestShouldDrop:

    def _config(self, *, enabled=True, drop_channels=None, drop_portnums=("RANGE_TEST_APP",)):
        return {
            "drop_enabled": enabled,
            "_drop_channels_set": drop_channels,
            "_drop_portnums_set": set(drop_portnums),
        }

    def test_disabled_never_drops(self):
        config = self._config(enabled=False, drop_channels={"LongFast"})
        assert should_drop(config, "LongFast", "RANGE_TEST_APP") is False

    def test_match_channel_and_portnum_drops(self):
        config = self._config(drop_channels={"LongFast"})
        assert should_drop(config, "LongFast", "RANGE_TEST_APP") is True

    def test_portnum_not_in_set_does_not_drop(self):
        config = self._config(drop_channels={"LongFast"})
        assert should_drop(config, "LongFast", "TEXT_MESSAGE_APP") is False

    def test_channel_not_in_set_does_not_drop(self):
        config = self._config(drop_channels={"LongFast"})
        assert should_drop(config, "MyPrivate", "RANGE_TEST_APP") is False

    def test_unknown_portnum_never_drops(self):
        # When floodgate can't read the portnum (custom-keyed channel,
        # unrecognized JSON type), the policy errs on deliver.
        config = self._config(drop_channels={"LongFast"})
        assert should_drop(config, "LongFast", None) is False

    def test_drop_channels_none_means_all_channels(self):
        config = self._config(drop_channels=None)
        assert should_drop(config, "LongFast",  "RANGE_TEST_APP") is True
        assert should_drop(config, "AnyChannel", "RANGE_TEST_APP") is True


# ---------------------------------------------------------------------------
# load_config — defaults and basic merge
# ---------------------------------------------------------------------------

class TestLoadConfigDefaults:

    def test_default_zerohop_enabled_true(self):
        cfg = load_config(None)
        assert cfg["zerohop_enabled"] is True

    def test_default_zerohop_channels_eight_presets(self):
        cfg = load_config(None)
        assert len(cfg["zerohop_channels"]) == 8
        assert "LongFast" in cfg["zerohop_channels"]

    def test_default_drop_disabled(self):
        cfg = load_config(None)
        assert cfg["drop_enabled"] is False

    def test_default_drop_channels_inherits(self):
        cfg = load_config(None)
        assert cfg["drop_channels"] == INHERIT_ZEROHOP_CHANNELS

    def test_default_drop_portnums_empty(self):
        cfg = load_config(None)
        assert cfg["drop_portnums"] == []

    def test_default_grpc_port(self):
        assert load_config(None)["grpc_port"] == 9000

    def test_default_health_port(self):
        assert load_config(None)["health_port"] == 8080

    def test_default_log_format_is_text(self):
        assert load_config(None)["log_format"] == "text"

    def test_precomputed_zerohop_set_matches_list(self):
        cfg = load_config(None)
        assert cfg["_zerohop_channels_set"] == set(cfg["zerohop_channels"])

    def test_precomputed_drop_set_inherits_zerohop_set(self):
        cfg = load_config(None)
        assert cfg["_drop_channels_set"] == cfg["_zerohop_channels_set"]

    def test_precomputed_drop_portnums_empty_set(self):
        assert load_config(None)["_drop_portnums_set"] == set()


class TestLoadConfigFromFile:

    def test_load_from_file(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"zerohop_enabled": False, "grpc_port": 8080}))
        cfg = load_config(str(cfg_file))
        assert cfg["zerohop_enabled"] is False
        assert cfg["grpc_port"] == 8080
        # untouched defaults still present
        assert cfg["health_port"] == 8080
        assert cfg["stats_interval_s"] == 60

    def test_load_from_env(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"zerohop_enabled": False}))
        monkeypatch.setenv("FLOODGATE_CONFIG", str(cfg_file))
        cfg = load_config(None)
        assert cfg["zerohop_enabled"] is False

    def test_missing_file_uses_defaults(self):
        cfg = load_config("/nonexistent/config.yaml")
        assert cfg["zerohop_enabled"] is True
        assert len(cfg["zerohop_channels"]) == 8

    def test_log_format_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"log_format": "json"}))
        assert load_config(str(cfg_file))["log_format"] == "json"

    def test_log_format_env_overrides_yaml(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"log_format": "text"}))
        monkeypatch.setenv("FLOODGATE_LOG_FORMAT", "json")
        assert load_config(str(cfg_file))["log_format"] == "json"

    def test_user_zerohop_channels_replaces_default(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"zerohop_channels": ["MyChannel"]}))
        cfg = load_config(str(cfg_file))
        assert cfg["zerohop_channels"] == ["MyChannel"]
        assert cfg["_zerohop_channels_set"] == {"MyChannel"}


# ---------------------------------------------------------------------------
# Removed-key rejection
# ---------------------------------------------------------------------------

class TestRemovedKeysRejected:

    @pytest.mark.parametrize("key,value", [
        ("channel_policy",    "blacklist"),
        ("channel_blacklist", ["LongFast"]),
        ("channel_whitelist", []),
    ])
    def test_each_removed_key_raises(self, tmp_path, key, value):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({key: value}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        assert key in str(exc.value)

    def test_error_message_lists_all_removed_keys_present(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "channel_policy":    "blacklist",
            "channel_whitelist": [],
        }))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        msg = str(exc.value)
        assert "channel_policy"    in msg
        assert "channel_whitelist" in msg


# ---------------------------------------------------------------------------
# drop_channels resolution
# ---------------------------------------------------------------------------

class TestDropChannelsResolution:

    def test_inherits_zerohop_channels_via_sentinel_string(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "zerohop_channels": ["LongFast", "MediumFast"],
            "drop_channels":    INHERIT_ZEROHOP_CHANNELS,
        }))
        cfg = load_config(str(cfg_file))
        assert cfg["_drop_channels_set"] == {"LongFast", "MediumFast"}

    def test_explicit_list(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "zerohop_channels": ["LongFast"],
            "drop_channels":    ["MediumFast", "ShortFast"],
        }))
        cfg = load_config(str(cfg_file))
        assert cfg["_drop_channels_set"] == {"MediumFast", "ShortFast"}

    def test_null_means_all_channels(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("drop_channels: null\nzerohop_channels: ['LongFast']\n")
        cfg = load_config(str(cfg_file))
        assert cfg["_drop_channels_set"] is None

    def test_empty_list_means_all_channels(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"drop_channels": []}))
        cfg = load_config(str(cfg_file))
        assert cfg["_drop_channels_set"] is None

    def test_invalid_string_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"drop_channels": "something_else"}))
        with pytest.raises(ConfigError):
            load_config(str(cfg_file))

    def test_invalid_type_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"drop_channels": 42}))
        with pytest.raises(ConfigError):
            load_config(str(cfg_file))


# ---------------------------------------------------------------------------
# drop_portnums set
# ---------------------------------------------------------------------------

class TestDropPortnums:

    def test_user_list_becomes_set(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "drop_portnums": ["RANGE_TEST_APP", "TELEMETRY_APP"],
        }))
        cfg = load_config(str(cfg_file))
        assert cfg["_drop_portnums_set"] == {"RANGE_TEST_APP", "TELEMETRY_APP"}

    def test_yaml_null_becomes_empty_set(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("drop_portnums:\n")
        cfg = load_config(str(cfg_file))
        assert cfg["_drop_portnums_set"] == set()


# ---------------------------------------------------------------------------
# String-list validation (catches numeric portnum IDs, etc.)
# ---------------------------------------------------------------------------

class TestStringListValidation:
    """A user typing `drop_portnums: [66]` (the numeric portnum) instead of
    `["RANGE_TEST_APP"]` would silently never match without this check."""

    def test_drop_portnums_with_int_entry_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"drop_portnums": [66, "TELEMETRY_APP"]}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        assert "drop_portnums" in str(exc.value)
        assert "66"            in str(exc.value)

    def test_drop_channels_with_int_entry_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"drop_channels": [42, "LongFast"]}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        assert "drop_channels" in str(exc.value)

    def test_zerohop_channels_with_int_entry_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"zerohop_channels": [1, "LongFast"]}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        assert "zerohop_channels" in str(exc.value)

    def test_zerohop_channels_not_a_list_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"zerohop_channels": "LongFast"}))
        with pytest.raises(ConfigError):
            load_config(str(cfg_file))
