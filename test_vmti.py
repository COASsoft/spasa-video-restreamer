"""Unit tests for the MISB ST 0903 (VMTI) Local Set parser (``shared/vmti.py``).

Pure parser tests — no Flask / ffmpeg / network. Validate the ST 0903 framing contract
via encode→decode round-trip and the SPASA ``/vmti/latest`` response shape.
"""

from shared.vmti import (
    build_vmti_response,
    encode_vmti_local_set,
    parse_vmti_local_set,
)


def test_round_trip_single_target():
    ls = encode_vmti_local_set(
        [{"targetId": 7, "lat": 40.1, "lon": -3.1, "haeM": 120.0, "confidence": 80, "priority": 2}]
    )
    targets = parse_vmti_local_set(ls)
    assert len(targets) == 1
    t = targets[0]
    assert t["targetId"] == 7
    assert abs(t["lat"] - 40.1) < 1e-4
    assert abs(t["lon"] - (-3.1)) < 1e-4
    assert abs(t["haeM"] - 120.0) < 1.0
    assert t["confidence"] == 80
    assert t["priority"] == 2


def test_round_trip_multiple_targets_and_optional_fields():
    ls = encode_vmti_local_set(
        [
            {"targetId": 1, "lat": 10.0, "lon": 20.0, "haeM": None, "confidence": None, "priority": None},
            {"targetId": 258, "lat": -33.9, "lon": 151.2, "haeM": 5.0, "confidence": 50, "priority": 1},
        ]
    )
    targets = parse_vmti_local_set(ls)
    assert [t["targetId"] for t in targets] == [1, 258]  # BER-OID id > 127 round-trips
    assert targets[0]["haeM"] is None
    assert targets[0]["confidence"] is None
    assert targets[1]["confidence"] == 50


def test_target_without_location_is_skipped():
    # A target carrying only priority/confidence (no Target Location) is not
    # materializable as a track → dropped.
    ls = encode_vmti_local_set([{"targetId": 9, "lat": None, "lon": None, "confidence": 99, "priority": 3}])
    assert parse_vmti_local_set(ls) == []


def test_build_response_shape():
    ls = encode_vmti_local_set([{"targetId": 4, "lat": 0.0, "lon": 0.0, "haeM": 0.0}])
    resp = build_vmti_response(ls, age_ms=60, stream_name="feed-a")
    assert resp["streamName"] == "feed-a"
    assert resp["present"] is True
    assert resp["ageMs"] == 60
    assert len(resp["targets"]) == 1


def test_build_response_empty_when_no_vmti():
    resp = build_vmti_response(None, age_ms=0, stream_name="feed-a")
    assert resp["present"] is False
    assert resp["targets"] == []
