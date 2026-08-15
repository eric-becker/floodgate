"""Configuration loader, schema validation, and policy evaluation."""

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

# Removed in this release; keys present in user YAML trigger ConfigError.
_REMOVED_KEYS = {
    "channel_policy":    "Replaced by `zerohop_enabled` (bool) and `zerohop_channels` (list).",
    "channel_blacklist": "Renamed to `zerohop_channels`.",
    "channel_whitelist": "Removed (whitelist mode is gone — use `zerohop_enabled: false` "
                         "or an empty `zerohop_channels` list).",
}

DEFAULT_CONFIG: dict[str, Any] = {
    # Zerohop: modify hop_limit to 0 and deliver. Existing behavior, new names.
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
    "drop_enabled":  False,
    "drop_channels": INHERIT_ZEROHOP_CHANNELS,
    "drop_portnums": [],

    # Transport / runtime
    "grpc_port":        9000,
    # Size of the gRPC server's thread pool. EMQX opens `pool_size` concurrent
    # connections to the ExHook (its own default is 8); if this is smaller,
    # calls queue behind busy workers. That only shows up as latency until the
    # broker runs `failed_action: deny`, where a call slower than
    # `request_timeout` means the message is denied — i.e. a dropped packet.
    # Match or exceed the broker's exhook pool_size.
    "grpc_max_workers": 16,
    "health_port":      8080,
    "topic_filter":     "msh/#",
    "stats_interval_s": 60,
    "log_level":        "INFO",
    "log_format":       "text",   # "text" | "json"
    "stats_log":        True,
}


class ConfigError(ValueError):
    """Raised at config-load time for unrecoverable schema problems."""


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load configuration from YAML, merge over defaults, and pre-compute lookup sets.

    Raises ConfigError if the YAML file uses keys removed in this release.
    """
    config = _deep_copy_dict(DEFAULT_CONFIG)

    if config_path is None:
        config_path = os.environ.get("FLOODGATE_CONFIG")

    user_keys: set[str] = set()
    if config_path:
        path = Path(config_path)
        if path.exists():
            logger.info("Loading config from %s", path)
            with open(path) as f:
                user_config = yaml.safe_load(f) or {}
            user_keys = set(user_config.keys())
            _reject_removed_keys(user_keys, source=str(path))
            _deep_merge(config, user_config)
        else:
            logger.warning("Config file %s not found, using defaults", path)

    # FLOODGATE_LOG_FORMAT is convenient in container/k8s environments where
    # editing the mounted config.yaml is awkward.
    env_fmt = os.environ.get("FLOODGATE_LOG_FORMAT")
    if env_fmt is not None:
        config["log_format"] = env_fmt.lower()

    config["_zerohop_channels_set"] = _validate_string_list(
        config.get("zerohop_channels"), key="zerohop_channels",
    )
    config["_drop_channels_set"]    = _resolve_drop_channels(
        config.get("drop_channels"),
        config["_zerohop_channels_set"],
    )
    config["_drop_portnums_set"]    = _validate_string_list(
        config.get("drop_portnums"), key="drop_portnums",
    )

    config["grpc_max_workers"] = _validate_positive_int(
        config.get("grpc_max_workers"), key="grpc_max_workers",
    )

    log_level = config.get("log_level", "INFO").upper()
    logging.getLogger("floodgate").setLevel(getattr(logging, log_level, logging.INFO))

    return config


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
        raise ConfigError(
            f"`{key}` entries must be strings; got non-string entries: {bad!r}"
        )
    return set(value)


def _validate_positive_int(value, key: str) -> int:
    """Coerce a YAML scalar into a positive int; reject anything else.

    `grpc.server(max_workers=...)` raises on 0 or a negative, and a value given
    as a string would be accepted by YAML but blow up at server construction —
    long after the config was "loaded". Fail at load time instead.
    Note bool is a subclass of int, so `grpc_max_workers: true` is rejected too.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"`{key}` must be a positive integer; got {value!r} ({type(value).__name__})"
        )
    if value < 1:
        raise ConfigError(f"`{key}` must be >= 1; got {value}")
    return value


def _reject_removed_keys(user_keys: set[str], source: str) -> None:
    found = sorted(user_keys & _REMOVED_KEYS.keys())
    if not found:
        return
    lines = [f"  - {k}: {_REMOVED_KEYS[k]}" for k in found]
    raise ConfigError(
        f"Removed config key(s) found in {source}:\n"
        + "\n".join(lines)
        + "\nUpdate your config — see README.md `Configuration` for the new schema."
    )


def _deep_copy_dict(d: dict) -> dict:
    """Shallow-recursive copy of nested dicts and lists. Sufficient for our
    config shape — no nested dicts of dicts deeper than two levels."""
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _deep_copy_dict(v)
        elif isinstance(v, list):
            result[k] = list(v)
        else:
            result[k] = v
    return result


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in place. Lists and scalars are replaced wholesale."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
