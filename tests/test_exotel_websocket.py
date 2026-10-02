"""Tests for Exotel WebSocket protocol parsing and event handling."""

import json
from app.exotel.websocket import (
    create_exotel_media_message,
    parse_exotel_message,
)


def test_parse_connected_event():
    msg = json.dumps({"event": "connected"})
    parsed = parse_exotel_message(msg)
    assert parsed is not None
    assert parsed.event == "connected"


def test_parse_start_event():
    raw = json.dumps({
        "event": "start",
        "sequence_number": "1",
        "stream_sid": "stream_12345",
        "start": {
            "account_sid": "acc_001",
            "stream_sid": "stream_12345",
            "call_sid": "call_98765",
            "tracks": ["inbound", "outbound"],
            "media_format": {
                "encoding": "audio/x-linear",
                "sample_rate": 8000,
                "channels": 1,
            },
        },
    })
    parsed = parse_exotel_message(raw)
    assert parsed is not None
    assert parsed.event == "start"
    assert parsed.stream_sid == "stream_12345"
    assert parsed.start.call_sid == "call_98765"
    assert parsed.start.media_format.sample_rate == 8000


def test_parse_media_event():
    raw = json.dumps({
        "event": "media",
        "sequence_number": "2",
        "stream_sid": "stream_12345",
        "media": {
            "track": "inbound",
            "chunk": "1",
            "timestamp": "12345678",
            "payload": "AQIDBA==",
        },
    })
    parsed = parse_exotel_message(raw)
    assert parsed is not None
    assert parsed.event == "media"
    assert parsed.media.payload == "AQIDBA=="


def test_parse_stop_event():
    raw = json.dumps({
        "event": "stop",
        "sequence_number": "3",
        "stream_sid": "stream_12345",
        "stop": {
            "account_sid": "acc_001",
            "call_sid": "call_98765",
        },
    })
    parsed = parse_exotel_message(raw)
    assert parsed is not None
    assert parsed.event == "stop"
    assert parsed.stop.call_sid == "call_98765"


def test_create_exotel_media_message():
    stream_sid = "test_stream_sid_99"
    payload = "AQIDBA=="
    json_str = create_exotel_media_message(stream_sid, payload)
    data = json.loads(json_str)

    assert data["event"] == "media"
    assert data["stream_sid"] == stream_sid
    assert data["media"]["payload"] == payload
