#!/usr/bin/env python3
"""Validate a rendered Helm chart beyond what a schema check can see.

Reads `helm template` output on stdin and fails unless:
  - the ConfigMap's config.yaml loads with floodgate's own load_config, so a
    chart default (or --set override) floodgate would refuse is caught here
    rather than as a crash-looping pod;
  - the Deployment carries the checksum/config annotation and uses
    maxUnavailable: 0 -- together these are what keep the old pod serving
    when a bad config is rolled out (README, "ExHook failure policy").

Usage: helm template floodgate charts/floodgate [--set ...] | scripts/check_chart.py
"""

import sys
import tempfile

import yaml

from floodgate.config import ConfigError, load_config


def main() -> int:
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    configmaps = [d for d in docs if d["kind"] == "ConfigMap"]
    deployments = [d for d in docs if d["kind"] == "Deployment"]
    problems = []

    if len(configmaps) != 1 or "config.yaml" not in configmaps[0].get("data", {}):
        problems.append("expected exactly one ConfigMap with a config.yaml key")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
            f.write(configmaps[0]["data"]["config.yaml"])
            f.flush()
            try:
                load_config(f.name)
            except ConfigError as exc:
                problems.append(f"rendered config.yaml is rejected by floodgate:\n{exc}")

    if len(deployments) != 1:
        problems.append("expected exactly one Deployment")
    else:
        spec = deployments[0]["spec"]
        annotations = spec["template"]["metadata"].get("annotations") or {}
        if "checksum/config" not in annotations:
            problems.append("Deployment pod template lacks the checksum/config annotation")
        rolling = (spec.get("strategy") or {}).get("rollingUpdate") or {}
        max_unavailable = rolling.get("maxUnavailable")
        if max_unavailable != 0:
            problems.append(
                f"Deployment rollingUpdate.maxUnavailable is {max_unavailable!r}, not 0"
            )

    for problem in problems:
        print(f"FAIL: {problem}")
    if not problems:
        print("Chart OK: config loads, checksum annotation and maxUnavailable: 0 present")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
