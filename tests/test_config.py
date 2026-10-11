"""Tests for configuration loading, schema validation, and policy evaluation."""

from pathlib import Path

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

    @pytest.mark.parametrize("key,expected", [
        ("zerohop_enabled",     True),
        ("drop_enabled",        False),
        ("drop_channels",       INHERIT_ZEROHOP_CHANNELS),
        ("drop_portnums",       []),
        ("grpc_port",           9000),
        ("health_port",         8080),
        ("log_format",          "text"),
        ("_drop_portnums_set",  set()),
    ])
    def test_default_value(self, key, expected):
        assert load_config(None)[key] == expected

    def test_default_zerohop_channels_eight_presets(self):
        cfg = load_config(None)
        assert len(cfg["zerohop_channels"]) == 8
        assert "LongFast" in cfg["zerohop_channels"]

    def test_precomputed_zerohop_set_matches_list(self):
        cfg = load_config(None)
        assert cfg["_zerohop_channels_set"] == set(cfg["zerohop_channels"])

    def test_precomputed_drop_set_inherits_zerohop_set(self):
        cfg = load_config(None)
        assert cfg["_drop_channels_set"] == cfg["_zerohop_channels_set"]


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

    def test_missing_explicit_file_raises(self):
        # A wrong mount path must not silently run on defaults (drop off).
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/config.yaml")

    def test_missing_file_from_env_raises(self, monkeypatch):
        monkeypatch.setenv("FLOODGATE_CONFIG", "/nonexistent/config.yaml")
        with pytest.raises(ConfigError, match="not found"):
            load_config()

    def test_no_path_uses_defaults(self, monkeypatch):
        monkeypatch.delenv("FLOODGATE_CONFIG", raising=False)
        cfg = load_config()
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


# ---------------------------------------------------------------------------
# grpc_max_workers
# ---------------------------------------------------------------------------

class TestGrpcMaxWorkers:
    """The gRPC thread pool must be sized against the broker's exhook pool_size.

    EMQX opens `pool_size` concurrent connections (its own default is 8). If
    floodgate's pool is smaller, calls queue behind busy workers -- invisible
    until the broker runs `failed_action: deny`, where a call slower than
    `request_timeout` is a denied publish, i.e. a dropped mesh packet.
    """

    def _write(self, tmp_path, body):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(body))
        return str(path)

    def test_default_is_at_least_emqx_default_pool_size(self, tmp_path):
        config = load_config(self._write(tmp_path, {}))
        # EMQX's exhook pool_size defaults to 8; anything smaller ships a
        # bottleneck out of the box.
        assert config["grpc_max_workers"] >= 8

    def test_user_value_overrides_default(self, tmp_path):
        config = load_config(self._write(tmp_path, {"grpc_max_workers": 32}))
        assert config["grpc_max_workers"] == 32

    def test_one_worker_is_allowed(self, tmp_path):
        config = load_config(self._write(tmp_path, {"grpc_max_workers": 1}))
        assert config["grpc_max_workers"] == 1

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_rejected(self, tmp_path, bad):
        with pytest.raises(ConfigError, match="grpc_max_workers"):
            load_config(self._write(tmp_path, {"grpc_max_workers": bad}))

    @pytest.mark.parametrize("bad", ["16", 1.5, [16], None])
    def test_non_int_rejected(self, tmp_path, bad):
        # A string sails through YAML and only explodes later inside
        # grpc.server(), long after config load reported success.
        with pytest.raises(ConfigError, match="grpc_max_workers"):
            load_config(self._write(tmp_path, {"grpc_max_workers": bad}))

    def test_bool_rejected(self, tmp_path):
        # bool is a subclass of int, so this needs an explicit guard.
        with pytest.raises(ConfigError, match="grpc_max_workers"):
            load_config(self._write(tmp_path, {"grpc_max_workers": True}))


# ---------------------------------------------------------------------------
# Unknown-key rejection
# ---------------------------------------------------------------------------

class TestUnknownKeysRejected:
    """A misspelled key would otherwise merge in silently and leave the
    default in effect -- e.g. `zerohop_channel:` keeps the 8 presets."""

    def test_unknown_key_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"zerohop_channel": ["LongFast"]}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        assert "zerohop_channel" in str(exc.value)

    def test_error_message_lists_all_unknown_keys(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"drop_portnum": [], "telemetry": True}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        msg = str(exc.value)
        assert "drop_portnum" in msg
        assert "telemetry"    in msg

    def test_every_default_key_is_accepted(self, tmp_path):
        from floodgate.config import DEFAULT_CONFIG
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(DEFAULT_CONFIG))
        load_config(str(cfg_file))

    def test_shipped_config_yaml_loads(self):
        load_config(str(Path(__file__).parent.parent / "config.yaml"))

    def test_integration_config_loads(self):
        load_config(str(Path(__file__).parent / "integration" / "config.yaml"))

    def test_k8s_manifest_config_loads(self, tmp_path):
        repo = Path(__file__).parent.parent
        configmap = yaml.safe_load((repo / "k8s" / "configmap.yaml").read_text())
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(configmap["data"]["config.yaml"])
        load_config(str(cfg_file))

    def test_chart_default_config_loads(self, tmp_path):
        repo = Path(__file__).parent.parent
        values = yaml.safe_load((repo / "charts" / "floodgate" / "values.yaml").read_text())
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(values["config"]))
        load_config(str(cfg_file))


    def test_removed_and_unknown_keys_reported_together(self, tmp_path):
        """One error covers both, so fixing a config doesn't take two restarts."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"channel_policy": "blacklist", "drop_portnum": []}))
        with pytest.raises(ConfigError) as exc:
            load_config(str(cfg_file))
        msg = str(exc.value)
        assert "channel_policy" in msg
        assert "drop_portnum"   in msg


# ---------------------------------------------------------------------------
# Value validation
# ---------------------------------------------------------------------------

def _load(tmp_path, data):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(data) if not isinstance(data, str) else data)
    return load_config(str(cfg_file))


class TestValueValidation:
    """Every key is type- and range-checked at load time. Each of these used to
    load: a quoted "false" is truthy, so `drop_enabled: "false"` turned drop ON."""

    @pytest.mark.parametrize("key", ["zerohop_enabled", "drop_enabled", "stats_log"])
    @pytest.mark.parametrize("value", ["false", "off", "true", 0, 1, None])
    def test_bool_keys_reject_non_bools(self, tmp_path, key, value):
        with pytest.raises(ConfigError, match=key):
            _load(tmp_path, {key: value})

    @pytest.mark.parametrize("key", ["zerohop_enabled", "drop_enabled", "stats_log"])
    def test_bool_keys_accept_bools(self, tmp_path, key):
        assert _load(tmp_path, {key: False})[key] is False

    @pytest.mark.parametrize("key", ["grpc_port", "health_port"])
    @pytest.mark.parametrize("value", ["9000", 0, -1, 65536, True, None])
    def test_ports_reject_out_of_range_or_non_int(self, tmp_path, key, value):
        with pytest.raises(ConfigError, match=key):
            _load(tmp_path, {key: value})

    @pytest.mark.parametrize("key", ["grpc_port", "health_port"])
    @pytest.mark.parametrize("value", [1, 65535])
    def test_ports_accept_valid_range(self, tmp_path, key, value):
        assert _load(tmp_path, {key: value})[key] == value

    @pytest.mark.parametrize("value", [0, -5, "60", True, None])
    def test_stats_interval_rejects_non_positive(self, tmp_path, value):
        # 0 used to make the stats thread busy-loop on stop_event.wait(0).
        with pytest.raises(ConfigError, match="stats_interval_s"):
            _load(tmp_path, {"stats_interval_s": value})

    def test_log_level_normalised(self, tmp_path):
        assert _load(tmp_path, {"log_level": "debug"})["log_level"] == "DEBUG"

    @pytest.mark.parametrize("value", ["VERBOSE", "", True, None, 10])
    def test_log_level_rejects_unknown(self, tmp_path, value):
        with pytest.raises(ConfigError, match="log_level"):
            _load(tmp_path, {"log_level": value})

    def test_log_format_normalised(self, tmp_path):
        assert _load(tmp_path, {"log_format": "JSON"})["log_format"] == "json"

    @pytest.mark.parametrize("value", ["xml", "", None, True])
    def test_log_format_rejects_unknown(self, tmp_path, value):
        with pytest.raises(ConfigError, match="log_format"):
            _load(tmp_path, {"log_format": value})

    def test_log_format_env_var_validated(self, monkeypatch):
        monkeypatch.delenv("FLOODGATE_CONFIG", raising=False)
        monkeypatch.setenv("FLOODGATE_LOG_FORMAT", "xml")
        with pytest.raises(ConfigError, match="FLOODGATE_LOG_FORMAT"):
            load_config()

    @pytest.mark.parametrize("value", ["", None, 5, ["msh/#"]])
    def test_topic_filter_rejects_non_string(self, tmp_path, value):
        with pytest.raises(ConfigError, match="topic_filter"):
            _load(tmp_path, {"topic_filter": value})

    @pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n"])
    def test_top_level_must_be_a_mapping(self, tmp_path, text):
        with pytest.raises(ConfigError, match="mapping"):
            _load(tmp_path, text)

    def test_empty_file_uses_defaults(self, tmp_path):
        assert _load(tmp_path, "")["zerohop_enabled"] is True

    def test_all_value_errors_reported_together(self, tmp_path):
        with pytest.raises(ConfigError) as exc:
            _load(tmp_path, {"drop_enabled": "false", "grpc_port": 0, "log_level": "LOUD"})
        msg = str(exc.value)
        assert "drop_enabled" in msg
        assert "grpc_port" in msg
        assert "log_level" in msg


class TestDropPortnumNames:
    """A misspelled portnum used to load and log "Drop ENABLED" while matching nothing."""

    def test_unknown_portnum_rejected_with_suggestion(self, tmp_path):
        pytest.importorskip("meshtastic")
        with pytest.raises(ConfigError) as exc:
            _load(tmp_path, {"drop_portnums": ["RANGETEST_APP"]})
        msg = str(exc.value)
        assert "RANGETEST_APP" in msg
        assert "RANGE_TEST_APP" in msg   # did-you-mean

    def test_wrong_case_portnum_rejected(self, tmp_path):
        pytest.importorskip("meshtastic")
        with pytest.raises(ConfigError, match="range_test_app"):
            _load(tmp_path, {"drop_portnums": ["range_test_app"]})

    def test_known_portnums_accepted(self, tmp_path):
        pytest.importorskip("meshtastic")
        cfg = _load(tmp_path, {"drop_portnums": ["RANGE_TEST_APP", "DETECTION_SENSOR_APP"]})
        assert cfg["_drop_portnums_set"] == {"RANGE_TEST_APP", "DETECTION_SENSOR_APP"}

