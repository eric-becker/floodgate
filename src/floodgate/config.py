"""Configuration loader."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "channel_policy": "blacklist",
    "channel_whitelist": [],
    "channel_blacklist": [
        "LongTurbo",
        "LongFast",
        "LongModerate",
        "MediumFast",
        "MediumSlow",
        "ShortFast",
        "ShortSlow",
        "ShortTurbo",
    ],
    "grpc_port": 9000,
    "health_port": 8080,
    # MQTT topic filter passed to EMQX ExHook — controls which topics EMQX
    # sends to this service. Use '#' suffix for wildcard subtopics.
    # Default covers all Meshtastic traffic regardless of region depth.
    "topic_filter": "msh/#",
    # How often (seconds) to emit a rolling stats summary at INFO level
    "stats_interval_s": 60,
    "log_level": "INFO",
    "log_format": "text",   # "text" | "json"
    "stats_log": True,
}


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load configuration from YAML file, with defaults."""
    config = _deep_copy_dict(DEFAULT_CONFIG)

    if config_path is None:
        config_path = os.environ.get("FLOODGATE_CONFIG")

    if config_path:
        path = Path(config_path)
        if path.exists():
            logger.info("Loading config from %s", path)
            with open(path) as f:
                user_config = yaml.safe_load(f) or {}
            _deep_merge(config, user_config)
        else:
            logger.warning("Config file %s not found, using defaults", path)

    # ENV var override for log_format — convenient for Docker/k8s without touching config files
    env_fmt = os.environ.get("FLOODGATE_LOG_FORMAT")
    if env_fmt is not None:
        config["log_format"] = env_fmt.lower()

    # Pre-compute sets for fast lookup
    # Use 'or []' to guard against YAML null (channel_whitelist: with no entries)
    config["_whitelist_set"] = set(config.get("channel_whitelist") or [])
    config["_blacklist_set"] = set(config.get("channel_blacklist") or [])

    # Apply log level
    log_level = config.get("log_level", "INFO").upper()
    logging.getLogger("floodgate").setLevel(getattr(logging, log_level, logging.INFO))

    return config


def should_zerohop(config: dict, channel_name: str) -> bool:
    """Determine whether a channel's packets should be zero-hopped.

    - whitelist policy: zero-hop everything EXCEPT channels in whitelist
    - blacklist policy: zero-hop ONLY channels in blacklist
    """
    policy = config.get("channel_policy", "whitelist")

    if policy == "whitelist":
        return channel_name not in config["_whitelist_set"]
    elif policy == "blacklist":
        return channel_name in config["_blacklist_set"]
    else:
        logger.warning("Unknown channel_policy '%s', defaulting to zero-hop", policy)
        return True


def _deep_copy_dict(d: dict) -> dict:
    """Simple deep copy for nested dicts/lists."""
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
    """Merge override into base, modifying base in place."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
