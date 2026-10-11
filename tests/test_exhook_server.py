"""Tests for the ExHook servicer: the response EMQX acts on for each outcome.

These pin the contract with EMQX's exhook handler: STOP_AND_RETURN with the
original message plus a replaced payload (modify), STOP_AND_RETURN with
`allow_publish: false` (drop), and IGNORE (passthru).
"""

import json
import threading
from unittest.mock import patch

import pytest

exhook_pb2 = pytest.importorskip("generated.exhook_pb2")

from floodgate.exhook_server import HookProviderServicer, _stats_reporter  # noqa: E402
from floodgate.zerohop import ProcessResult  # noqa: E402

TOPIC = "msh/US/2/json/LongFast/!aabbccdd"


def _config(**overrides):
    config = {
        "topic_filter": "msh/#",
        "zerohop_enabled": True,
        "_zerohop_channels_set": {"LongFast"},
        "drop_enabled": False,
        "_drop_channels_set": None,
        "_drop_portnums_set": set(),
    }
    config.update(overrides)
    return config


def _publish_request(payload: bytes, topic: str = TOPIC):
    msg = exhook_pb2.Message(
        node="emqx@127.0.0.1",
        id="0001",
        qos=1,
        topic=topic,
        payload=payload,
        timestamp=1234567890,
        headers={"username": "gw"},
    )
    setattr(msg, "from", "!aabbccdd")
    return exhook_pb2.MessagePublishRequest(message=msg)


class TestOnProviderLoaded:
    def test_registers_only_message_publish_with_configured_filter(self):
        servicer = HookProviderServicer(_config(topic_filter="msh/US/#"))
        request = exhook_pb2.ProviderLoadedRequest(
            broker=exhook_pb2.BrokerInfo(version="6.2.0", sysdescr="EMQX")
        )
        resp = servicer.OnProviderLoaded(request, None)
        assert [h.name for h in resp.hooks] == ["message.publish"]
        assert list(resp.hooks[0].topics) == ["msh/US/#"]


class TestOnMessagePublish:
    def test_modify_replaces_payload_and_keeps_everything_else(self):
        servicer = HookProviderServicer(_config())
        payload = json.dumps({"id": 1, "hop_start": 3, "hops_away": 0}).encode()
        request = _publish_request(payload)

        resp = servicer.OnMessagePublish(request, None)

        assert resp.type == exhook_pb2.ValuedResponse.STOP_AND_RETURN
        assert resp.WhichOneof("value") == "message"
        out = resp.message
        assert json.loads(out.payload)["hop_limit"] == 0
        assert "allow_publish" not in out.headers
        for field in ("node", "id", "qos", "topic", "timestamp"):
            assert getattr(out, field) == getattr(request.message, field)
        assert getattr(out, "from") == "!aabbccdd"
        assert dict(out.headers) == {"username": "gw"}

    def test_drop_denies_with_allow_publish_false_and_original_payload(self):
        servicer = HookProviderServicer(
            _config(
                drop_enabled=True,
                _drop_channels_set={"LongFast"},
                _drop_portnums_set={"TEXT_MESSAGE_APP"},
            )
        )
        payload = json.dumps({"id": 1, "type": "text", "hop_start": 3, "hops_away": 0}).encode()
        request = _publish_request(payload)

        resp = servicer.OnMessagePublish(request, None)

        assert resp.type == exhook_pb2.ValuedResponse.STOP_AND_RETURN
        assert resp.message.headers["allow_publish"] == "false"
        assert resp.message.headers["username"] == "gw"
        assert resp.message.payload == payload
        assert resp.message.topic == TOPIC

    def test_passthru_is_ignore_with_no_message(self):
        servicer = HookProviderServicer(_config())
        payload = json.dumps({"id": 1, "hop_start": 3, "hops_away": 0}).encode()

        request = _publish_request(payload, "msh/US/2/json/Private/!aabbccdd")

        resp = servicer.OnMessagePublish(request, None)

        assert resp.type == exhook_pb2.ValuedResponse.IGNORE
        assert resp.WhichOneof("value") is None

    def test_processing_failure_still_answers_ignore(self):
        # A bug in processing must not surface as a gRPC error; it fails open.
        servicer = HookProviderServicer(_config())
        with patch("floodgate.zerohop._process_message_inner", side_effect=RuntimeError("boom")):
            resp = servicer.OnMessagePublish(_publish_request(b"{}"), None)
        assert resp.type == exhook_pb2.ValuedResponse.IGNORE

    def test_debug_line_escapes_the_topic(self, caplog):
        servicer = HookProviderServicer(_config())
        with caplog.at_level("DEBUG", logger="floodgate.exhook_server"):
            servicer.OnMessagePublish(_publish_request(b"{}", "msh/US/2/e/X/!a\nFORGED"), None)
        assert not [r for r in caplog.records if "\n" in r.getMessage()]

    def test_dispatches_on_process_result(self):
        servicer = HookProviderServicer(_config())
        with patch(
            "floodgate.exhook_server.process_message", return_value=ProcessResult.modify(b"NEW")
        ):
            resp = servicer.OnMessagePublish(_publish_request(b"OLD"), None)
        assert resp.message.payload == b"NEW"


class TestStatsReporter:
    class _OneShotEvent:
        """Stands in for threading.Event: one interval elapses, then stop."""

        def __init__(self):
            self.calls = 0

        def wait(self, timeout):
            self.calls += 1
            return self.calls > 1

    def test_logs_and_resets_rolling_counters_each_interval(self, caplog):
        from floodgate.zerohop import stats

        stats.reset()
        stats.inc("zerohop")
        stats.inc("dropped")
        lifetime_before = stats.snapshot()

        with caplog.at_level("INFO", logger="floodgate.exhook_server"):
            _stats_reporter(60, self._OneShotEvent(), stats_log=True)

        records = [r for r in caplog.records if getattr(r, "event", None) == "stats"]
        assert len(records) == 1
        assert (records[0].zerohop, records[0].dropped, records[0].total) == (1, 1, 2)
        # Rolling window reset; lifetime counters (/health) untouched.
        assert stats.reset()["total"] == 0
        assert stats.snapshot() == lifetime_before

    def test_stats_log_false_resets_without_logging(self, caplog):
        from floodgate.zerohop import stats

        stats.reset()
        stats.inc("passthru")
        with caplog.at_level("INFO", logger="floodgate.exhook_server"):
            _stats_reporter(60, self._OneShotEvent(), stats_log=False)
        assert not [r for r in caplog.records if getattr(r, "event", None) == "stats"]
        assert stats.reset()["total"] == 0

    def test_real_event_stops_promptly(self):
        stop = threading.Event()
        thread = threading.Thread(target=_stats_reporter, args=(3600, stop))
        thread.start()
        stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
