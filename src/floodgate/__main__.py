"""floodgate entry point."""

import argparse
import logging
import sys

from . import __version__
from .config import load_config


def main():
    parser = argparse.ArgumentParser(
        prog="floodgate",
        description=(
            "Zero-hop MQTT anti-flood service for Meshtastic/EMQX.\n"
            "Sets MeshPacket.hop_limit=0 in-flight via EMQX ExHook before delivery to subscribers."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to config.yaml (default: $FLOODGATE_CONFIG or built-in defaults)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help=(
            "Enable DEBUG logging — emits [ZEROHOP]/[PASSTHRU]/[NOOP]/[SKIP] "
            "for every message processed. Equivalent to log_level: DEBUG in config.yaml."
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(args.config)

    if args.verbose:
        config["log_level"] = "DEBUG"

    log_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    logging.getLogger("floodgate").setLevel(log_level)

    logger = logging.getLogger("floodgate")
    logger.info("floodgate %s starting", __version__)

    from .exhook_server import serve
    serve(config)


if __name__ == "__main__":
    main()
