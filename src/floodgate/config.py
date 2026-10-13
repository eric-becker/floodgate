"""Configuration loader, schema validation, and policy evaluation."""

import difflib
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Sentinel string accepted in YAML for `drop_channels` to mean "use the same
# channel list as `zerohop_channels`". Avoids forcing users to maintain two
# identical lists when they want both filters scoped the same way.
INHERIT_ZEROHOP_CHANNELS = "zerohop_channels"

# Removed in 0.3.0; keys present in user YAML trigger ConfigError.
_REMOVED_KEYS = {
    "channel_policy": "Replaced by `zerohop_enabled` (bool) and `zerohop_channels` (list).",
    "channel_blacklist": "Renamed to `zerohop_channels`.",
    "channel_whitelist": "Removed (whitelist mode is gone — use `zerohop_enabled: false` "
    "or an empty `zerohop_channels` list).",
}

DEFAULT_CONFIG: dict[str, Any] = {
    # Zerohop: set hop_limit to 0 and hop_start to the hops travelled, then deliver.
    "zerohop_enabled": True,
    "zerohop_channels": [
        "LongTurbo",
        "LongFast",
        "LongModerate",
        "MediumFast",
        "MediumSlow",
        "ShortFast",
        "ShortSlow",
        "ShortTurbo",
    ],
    # Drop: deny entirely (EMQX does not deliver). Disabled by default.
    "drop_enabled": False,
    "drop_channels": INHERIT_ZEROHOP_CHANNELS,
    "drop_portnums": [],
    # Transport / runtime
    "grpc_port": 9000,
    # Size of the gRPC server's thread pool. EMQX opens `pool_size` concurrent
    # connections to the ExHook (its own default is 8); if this is smaller,
    # calls queue behind busy workers. A call slower than the broker's
    # `request_timeout` counts as failed, and EMQX then delivers the message
    # unmodified whatever `failed_action` says -- an undersized pool silently
    # loses zero-hop protection rather than dropping packets.
    # Match or exceed the broker's exhook pool_size.
    "grpc_max_workers": 16,
    "health_port": 8080,
    "topic_filter": "msh/#",
    "stats_interval_s": 60,
    "log_level": "INFO",
    "log_format": "text",  # "text" | "json"
    "stats_log": True,
}

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOG_FORMATS = ("text", "json")


class ConfigError(ValueError):
    """Raised at config-load time for unrecoverable schema problems."""


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load configuration from YAML over the defaults, validate it, and
    pre-compute lookup sets.

    `config_path` falls back to $FLOODGATE_CONFIG; with neither set, the
    built-in defaults are used. Raises ConfigError if a named file is missing
    or is not a mapping, if it uses removed or unknown keys, or if any value
    has the wrong type or range. Value problems are reported together.
    """
    config = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULT_CONFIG.items()}

    if config_path is None:
        config_path = os.environ.get("FLOODGATE_CONFIG")

    source = "built-in defaults"
    if config_path:
        path = Path(config_path)
        if not path.is_file():
            # Running on defaults would silently disable drop and any custom
            # channel list -- a wrong mount path must be loud.
            raise ConfigError(f"Config file {path} not found")
        logger.info("Loading config from %s", path)
        try:
            with open(path) as f:
                user_config = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Config file {path} is not valid YAML:\n{exc}") from None
        except OSError as exc:
            raise ConfigError(f"Config file {path} could not be read: {exc}") from None
        if user_config is None:
            user_config = {}
        if not isinstance(user_config, dict):
            raise ConfigError(
                f"Config file {path} must be a mapping of keys to values; "
                f"got {type(user_config).__name__}"
            )
        bad_keys = [k for k in user_config if not isinstance(k, str)]
        if bad_keys:
            raise ConfigError(
                f"Config file {path}: keys must be strings; got {', '.join(map(repr, bad_keys))}"
            )
        _reject_bad_keys(set(user_config), source=str(path))
        config.update(user_config)
        source = str(path)

    errors: list[str] = []
    _validate_values(config, errors)
    if errors:
        raise ConfigError(
            f"Invalid config value(s) in {source}:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    logging.getLogger("floodgate").setLevel(config["log_level"])
    return config


def _validate_values(config: dict[str, Any], errors: list[str]) -> None:
    """Type- and range-check every key, normalizing where a spelling is
    unambiguous (log_level case, log_format case). Appends to `errors`.

    Booleans must be real YAML booleans: a quoted "false" is a truthy string,
    so `drop_enabled: "false"` would otherwise turn drop ON.
    """
    for key in ("zerohop_enabled", "drop_enabled", "stats_log"):
        if not isinstance(config[key], bool):
            errors.append(f"`{key}` must be true or false (unquoted); got {config[key]!r}")

    _check_int(config, "grpc_port", errors, 1, 65535)
    _check_int(config, "health_port", errors, 1, 65535)
    _check_int(config, "grpc_max_workers", errors, 1)
    _check_int(config, "stats_interval_s", errors, 1)

    level = config["log_level"]
    if isinstance(level, str) and level.upper() in _LOG_LEVELS:
        config["log_level"] = level.upper()
    else:
        errors.append(f"`log_level` must be one of {', '.join(_LOG_LEVELS)}; got {level!r}")

    # FLOODGATE_LOG_FORMAT is convenient in container/k8s environments where
    # editing the mounted config.yaml is awkward.
    env_fmt = os.environ.get("FLOODGATE_LOG_FORMAT") or None  # empty means unset
    fmt, label = (
        (env_fmt, "FLOODGATE_LOG_FORMAT")
        if env_fmt is not None
        else (config["log_format"], "`log_format`")
    )
    if isinstance(fmt, str) and fmt.lower() in _LOG_FORMATS:
        config["log_format"] = fmt.lower()
    else:
        errors.append(f"{label} must be one of {', '.join(_LOG_FORMATS)}; got {fmt!r}")

    topic_filter = config["topic_filter"]
    if not isinstance(topic_filter, str) or not topic_filter:
        errors.append(f"`topic_filter` must be a non-empty string; got {topic_filter!r}")

    try:
        config["_zerohop_channels_set"] = _validate_string_list(
            config["zerohop_channels"],
            key="zerohop_channels",
        )
        config["_drop_channels_set"] = _resolve_drop_channels(
            config["drop_channels"],
            config["_zerohop_channels_set"],
        )
    except ConfigError as exc:
        errors.append(str(exc))
    try:
        config["_drop_portnums_set"] = _validate_string_list(
            config["drop_portnums"],
            key="drop_portnums",
        )
        _check_portnum_names(config["_drop_portnums_set"], errors)
    except ConfigError as exc:
        errors.append(str(exc))


def _check_int(
    config: dict[str, Any], key: str, errors: list[str], low: int, high: int | None = None
) -> None:
    """Append an error unless config[key] is an int in [low, high].

    bool is a subclass of int, so `grpc_port: true` is rejected explicitly.
    """
    value = config[key]
    in_range = (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= low
        and (high is None or value <= high)
    )
    if not in_range:
        bound = f">= {low}" if high is None else f"between {low} and {high}"
        errors.append(f"`{key}` must be an integer {bound}; got {value!r}")


def _check_portnum_names(names: set[str], errors: list[str]) -> None:
    """Reject `drop_portnums` entries that are not Meshtastic PortNum names.

    floodgate only ever compares against canonical enum names, so a typo
    (`RANGETEST_APP`) or the wrong case would never match anything.
    """
    try:
        from meshtastic import portnums_pb2
    except ImportError:
        # Only possible without generated stubs (some test setups); the
        # runtime image always has them.
        logger.warning("Meshtastic protobufs not importable; drop_portnums names not checked")
        return
    known = set(portnums_pb2.PortNum.keys())
    for name in sorted(names - known):
        hint = difflib.get_close_matches(name.upper(), known, n=1)
        suggestion = f" (did you mean {hint[0]!r}?)" if hint else ""
        errors.append(f"`drop_portnums` entry {name!r} is not a Meshtastic portnum{suggestion}")


def should_zerohop(config: dict, channel_name: str) -> bool:
    """True if a packet on `channel_name` should have hop_limit zeroed."""
    if not config.get("zerohop_enabled", True):
        return False
    return channel_name in config["_zerohop_channels_set"]


def should_drop(config: dict, channel_name: str, portnum: str | None) -> bool:
    """True if a packet should be denied (EMQX will not deliver it).

    `portnum` may be None when floodgate cannot read it (custom-keyed
    encrypted packet, JSON without recognizable type, etc.) — in that
    case we always deliver, since drop is destructive.
    """
    if not config.get("drop_enabled", False):
        return False
    if portnum is None:
        return False
    if portnum not in config["_drop_portnums_set"]:
        return False
    drop_channels = config["_drop_channels_set"]
    # None ⇒ "all channels" (no scoping)
    if drop_channels is None:
        return True
    return channel_name in drop_channels


def _resolve_drop_channels(value, zerohop_set: set[str]) -> set[str] | None:
    """Translate the raw `drop_channels` config value into a lookup set or None.

    None / [] / missing  → None  (means "all channels")
    "zerohop_channels"   → a copy of the zerohop set
    list of strings      → set(value)
    anything else        → ConfigError
    """
    if value is None:
        return None
    if isinstance(value, str):
        if value == INHERIT_ZEROHOP_CHANNELS:
            return set(zerohop_set)
        raise ConfigError(
            f"`drop_channels` must be a list, null, or the literal string "
            f"{INHERIT_ZEROHOP_CHANNELS!r}; got {value!r}"
        )
    if isinstance(value, list):
        if not value:
            return None
        return _validate_string_list(value, key="drop_channels")
    raise ConfigError(
        f"`drop_channels` must be a list, null, or the literal string "
        f"{INHERIT_ZEROHOP_CHANNELS!r}; got {type(value).__name__}"
    )


def _validate_string_list(value, key: str) -> set[str]:
    """Coerce a YAML list into a set of strings; reject non-string entries.

    Without this check, e.g. a portnum given as `66` (the int) instead of
    `"RANGE_TEST_APP"` would silently never match anything at runtime.
    """
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ConfigError(f"`{key}` must be a list of strings; got {type(value).__name__}")
    bad = [v for v in value if not isinstance(v, str)]
    if bad:
        raise ConfigError(f"`{key}` entries must be strings; got non-string entries: {bad!r}")
    return set(value)


def _reject_bad_keys(user_keys: set[str], source: str) -> None:
    """Refuse removed and unknown keys, reporting both in one error.

    Unknown keys are refused because a typo would otherwise merge in silently
    and leave the default in effect -- `zerohop_channel:` would leave the 8
    presets zero-hopped with no warning.
    """
    removed = sorted(user_keys & _REMOVED_KEYS.keys())
    unknown = sorted(user_keys - DEFAULT_CONFIG.keys() - _REMOVED_KEYS.keys())
    if not removed and not unknown:
        return
    parts = []
    if removed:
        lines = [f"  - {k}: {_REMOVED_KEYS[k]}" for k in removed]
        parts.append(
            f"Removed config key(s) found in {source}:\n"
            + "\n".join(lines)
            + "\nUpdate your config — see README.md `Configuration` for the new schema."
        )
    if unknown:
        parts.append(
            f"Config file {source} contains unknown key(s): {', '.join(unknown)}\n"
            f"Known keys: {', '.join(sorted(DEFAULT_CONFIG))}"
        )
    raise ConfigError("\n".join(parts))
