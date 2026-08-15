"""EMQX ExHook gRPC server — intercepts PUBLISH events to drop or modify in-flight."""

import logging
import threading
from concurrent import futures

import grpc

from .zerohop import ACTION_DROP, ACTION_MODIFY, process_message
from .zerohop import stats as packet_stats

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
        logger.debug("OnMessagePublish: topic=%s bytes=%d", msg.topic, len(msg.payload))
        result = process_message(msg.topic, msg.payload, self.config)
        logger.debug("OnMessagePublish: action=%s", result.action)

        resp = _exhook_pb2.ValuedResponse()
        if result.action == ACTION_MODIFY:
            resp.type = _exhook_pb2.ValuedResponse.STOP_AND_RETURN
            resp.message.CopyFrom(msg)
            resp.message.payload = result.payload
        elif result.action == ACTION_DROP:
            # EMQX honors `allow_publish: false` in message headers to deny
            # the publish. STOP_AND_RETURN ends the hook chain so EMQX uses
            # this response directly.
            resp.type = _exhook_pb2.ValuedResponse.STOP_AND_RETURN
            resp.message.CopyFrom(msg)
            resp.message.headers["allow_publish"] = "false"
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

def _stats_reporter(interval_s: int, stop_event: threading.Event,
                    stats_log: bool = True):
    """Log rolling stats every interval_s seconds (resets counters each cycle)."""
    while not stop_event.wait(timeout=interval_s):
        snap = packet_stats.reset()
        if not stats_log:
            continue
        if snap["total"] > 0:
            logger.info(
                "stats",
                extra={
                    "event":      "stats",
                    "interval_s": interval_s,
                    "zerohop":    snap["zerohop"],
                    "passthru":   snap["passthru"],
                    "noop":       snap["noop"],
                    "dropped":    snap["dropped"],
                    "skipped":    snap["skipped"],
                    "errors":     snap["errors"],
                    "total":      snap["total"],
                },
            )
        else:
            logger.debug("No meshtastic messages in last %ds", interval_s)


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

def _log_startup_policy(config: dict):
    interval     = config.get("stats_interval_s", 60)
    topic_filter = config.get("topic_filter", "msh/#")
    logger.info("Topic filter: %s", topic_filter)

    if config.get("zerohop_enabled", True):
        channels = config.get("zerohop_channels", []) or []
        if channels:
            logger.info(
                "Zerohop ENABLED — zero-hopping packets on: %s",
                ", ".join(channels),
            )
        else:
            logger.info(
                "Zerohop ENABLED — `zerohop_channels` is empty, no channels will be zero-hopped"
            )
    else:
        logger.info("Zerohop DISABLED — packets will not be zero-hopped")

    if config.get("drop_enabled", False):
        portnums = config.get("drop_portnums", []) or []
        if not portnums:
            logger.warning(
                "Drop ENABLED but `drop_portnums` is empty — drop is a no-op"
            )
        else:
            scope = _format_drop_channel_scope(config)
            logger.info(
                "Drop ENABLED — dropping portnums [%s] on %s",
                ", ".join(portnums), scope,
            )
    else:
        logger.info("Drop DISABLED")

    logger.info("Stats will be logged every %d seconds", interval)
    logger.info(
        "Per-message outcomes ([ZEROHOP]/[PASSTHRU]/[NOOP]/[DROPPED]) logged at INFO. "
        "Run with -v for verbose DEBUG."
    )


def _format_drop_channel_scope(config: dict) -> str:
    drop_set = config.get("_drop_channels_set")
    if drop_set is None:
        return "all channels"
    return f"channels [{', '.join(sorted(drop_set))}]"


def serve(config: dict):
    """Start the ExHook gRPC server and block until terminated."""
    _load_exhook_protos()

    port           = config.get("grpc_port", 9000)
    health_port    = config.get("health_port", 8080)
    stats_interval = config.get("stats_interval_s", 60)
    max_workers    = config.get("grpc_max_workers", 16)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    _exhook_pb2_grpc.add_HookProviderServicer_to_server(
        HookProviderServicer(config), server
    )
    try:
        server.add_insecure_port(f"[::]:{port}")
        server.start()
    except Exception as exc:
        logger.error("Failed to start gRPC server on port %d: %s(%s)",
                     port, type(exc).__name__, exc, exc_info=True)
        raise

    from .health import start_health_server
    start_health_server(health_port)

    logger.info("ExHook gRPC server listening on port %d  (max_workers=%d)", port, max_workers)
    logger.info("Waiting for EMQX to connect and register ExHook...")
    _log_startup_policy(config)

    stats_log = config.get("stats_log", True)
    stop_event   = threading.Event()
    stats_thread = threading.Thread(
        target=_stats_reporter,
        args=(stats_interval, stop_event, stats_log),
        daemon=True,
        name="stats-reporter",
    )
    stats_thread.start()

    try:
        server.wait_for_termination()
    except Exception as exc:
        logger.error("gRPC server terminated unexpectedly: %s(%s)",
                     type(exc).__name__, exc, exc_info=True)
        raise
    finally:
        stop_event.set()
