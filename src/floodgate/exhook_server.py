"""EMQX ExHook gRPC server — intercepts PUBLISH events to modify payloads in-flight."""

import logging
import threading
from concurrent import futures

import grpc

from .antiflood import process_message, stats as antiflood_stats

logger = logging.getLogger(__name__)

_exhook_pb2      = None
_exhook_pb2_grpc = None


def _load_exhook_protos():
    global _exhook_pb2, _exhook_pb2_grpc
    if _exhook_pb2 is None:
        from generated import exhook_pb2, exhook_pb2_grpc
        _exhook_pb2      = exhook_pb2
        _exhook_pb2_grpc = exhook_pb2_grpc


# ---------------------------------------------------------------------------
# gRPC servicer
# ---------------------------------------------------------------------------

class HookProviderServicer:

    def __init__(self, config: dict):
        self.config = config
        _load_exhook_protos()

    def OnProviderLoaded(self, request, context):
        topic_filter = self.config.get("topic_filter", "msh/#")
        logger.info(
            "ExHook connected: EMQX %s (%s)  topic_filter=%s",
            request.broker.version,
            request.broker.sysdescr,
            topic_filter,
        )
        resp = _exhook_pb2.LoadedResponse()
        hook = resp.hooks.add()
        hook.name = "message.publish"
        hook.topics.append(topic_filter)
        return resp

    def OnProviderUnloaded(self, request, context):
        logger.info("ExHook disconnected from EMQX")
        return _exhook_pb2.EmptySuccess()

    def OnMessagePublish(self, request, context):
        msg = request.message
        modified_payload = process_message(msg.topic, msg.payload, self.config)

        resp = _exhook_pb2.ValuedResponse()
        if modified_payload is not None:
            resp.type = _exhook_pb2.ValuedResponse.STOP_AND_RETURN
            resp.message.CopyFrom(msg)
            resp.message.payload = modified_payload
        else:
            resp.type = _exhook_pb2.ValuedResponse.IGNORE
        return resp

    # Remaining hooks — all no-ops -----------------------------------------

    def OnClientConnect(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnClientConnack(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnClientConnected(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnClientDisconnected(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnClientAuthenticate(self, request, context):
        resp = _exhook_pb2.ValuedResponse()
        resp.type = _exhook_pb2.ValuedResponse.IGNORE
        return resp

    def OnClientAuthorize(self, request, context):
        resp = _exhook_pb2.ValuedResponse()
        resp.type = _exhook_pb2.ValuedResponse.IGNORE
        return resp

    def OnClientSubscribe(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnClientUnsubscribe(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionCreated(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionSubscribed(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionUnsubscribed(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionResumed(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionDiscarded(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionTakenover(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnSessionTerminated(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnMessageDelivered(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnMessageDropped(self, request, context):
        return _exhook_pb2.EmptySuccess()

    def OnMessageAcked(self, request, context):
        return _exhook_pb2.EmptySuccess()


# ---------------------------------------------------------------------------
# Periodic stats reporter
# ---------------------------------------------------------------------------

def _stats_reporter(interval_s: int, stop_event: threading.Event):
    """Log rolling stats every interval_s seconds (resets counters each cycle)."""
    while not stop_event.wait(timeout=interval_s):
        snap = antiflood_stats.reset()
        if snap["total"] > 0:
            logger.info(
                "Stats [last %ds]  zerohopped=%-4d  passthru=%-4d  noop=%-4d"
                "  skipped=%-4d  errors=%-4d  total=%d",
                interval_s,
                snap["zerohopped"], snap["passthru"], snap["noop"],
                snap["skipped"],    snap["errors"],   snap["total"],
            )
        else:
            logger.debug("Stats [last %ds]  no meshtastic messages received", interval_s)


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def _log_startup_policy(config: dict):
    policy        = config.get("channel_policy", "whitelist")
    interval      = config.get("stats_interval_s", 60)
    topic_filter  = config.get("topic_filter", "msh/#")
    logger.info("Topic filter: %s", topic_filter)

    if policy == "whitelist":
        channels = config.get("channel_whitelist", [])
        if channels:
            logger.info(
                "Channel policy: WHITELIST — zero-hopping all channels EXCEPT: %s",
                ", ".join(channels),
            )
        else:
            logger.info(
                "Channel policy: WHITELIST — whitelist is empty, zero-hopping ALL channels"
            )
    else:
        channels = config.get("channel_blacklist", [])
        logger.info(
            "Channel policy: BLACKLIST — zero-hopping only: %s",
            ", ".join(channels) if channels else "(none configured)",
        )

    logger.info("Stats will be logged every %d seconds", interval)
    logger.info(
        "Set log_level: DEBUG (or run with -v) to see per-message outcome logs"
    )


def serve(config: dict):
    """Start the ExHook gRPC server and block until terminated."""
    _load_exhook_protos()

    port           = config.get("grpc_port", 9000)
    stats_interval = config.get("stats_interval_s", 60)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    _exhook_pb2_grpc.add_HookProviderServicer_to_server(
        HookProviderServicer(config), server
    )
    server.add_insecure_port(f"[::]:{port}")
    server.start()

    logger.info("ExHook gRPC server listening on port %d", port)
    logger.info("Waiting for EMQX to connect and register ExHook...")
    _log_startup_policy(config)

    stop_event   = threading.Event()
    stats_thread = threading.Thread(
        target=_stats_reporter,
        args=(stats_interval, stop_event),
        daemon=True,
        name="stats-reporter",
    )
    stats_thread.start()

    try:
        server.wait_for_termination()
    finally:
        stop_event.set()
