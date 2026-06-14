"""MISB ST 0903 (VMTI — Video Moving-Target Indicator) Local Set parser.

The UAS Datalink (ST 0601) stream carries VMTI as a nested Local Set in tag 74. This
module decodes that nested Local Set into a flat list of moving-target detections with
**absolute** geo positions, in the JSON contract the SPASA server's VMTI consumer
expects (``GET /api/streams/<name>/vmti/latest``):

    {"streamName": ..., "present": bool, "ageMs": int,
     "targets": [{"targetId": int, "lat": float, "lon": float,
                  "haeM": float|None, "confidence": int|None, "priority": int|None}, ...]}

Scope (v1): the **VTarget Series** (VMTI tag 101) is decoded; per target the absolute
**Target Location** (VTarget tag 17), **Target Priority** (tag 4) and **Target
Confidence Level** (tag 5) are extracted. Targets without a resolvable absolute location
are skipped — the SPASA side needs lat/lon to materialize a track. Pixel-only targets
(centroid without a Target Location pack) require the parent frame footprint to
geo-locate and are deferred (the server degrades cleanly: such targets simply do not
appear).

Pure standard-library (no Flask / klvdata), so the parser is unit-testable in isolation
via the ``encode_vmti_local_set`` round-trip — that round-trip is the ST 0903 *framing*
contract; live-stream fidelity against real sensors is validated end-to-end with the
SPASA server (``make run-docker-all``).
"""

import struct
from typing import Any, Dict, List, Optional, Tuple

# VMTI Local Set (ST 0903) tag ids used here.
_VMTI_TAG_VTARGET_SERIES = 101

# VTarget Local Set (ST 0903) tag ids used here.
_VTARGET_TAG_PRIORITY = 4          # 1 byte, 1..255 (lower = higher priority)
_VTARGET_TAG_CONFIDENCE = 5        # 1 byte, 0..100 (percent)
_VTARGET_TAG_TARGET_LOCATION = 17  # Target Location pack (lat/lon/HAE [+ sigma/rho])

# KLV coordinate mappings (shared with ST 0601). Latitude ±90 and longitude ±180 map to
# the full signed int32 range; the reserved value (-2^31, "error") decodes to None.
_INT32_MAX = 2 ** 31 - 1
_INT32_ERR = -(2 ** 31)
# Height: uint16 maps -900 m .. +19000 m (ST 0601 sensor-height style).
_HAE_MIN = -900.0
_HAE_MAX = 19000.0
_UINT16_MAX = 2 ** 16 - 1


def parse_ber_length(data: bytes, offset: int) -> Tuple[int, int]:
    """Decodes a BER length at ``offset``; returns ``(length, new_offset)``."""
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    n = first & 0x7F
    length = int.from_bytes(data[offset:offset + n], "big")
    return length, offset + n


def parse_ber_oid(data: bytes, offset: int) -> Tuple[int, int]:
    """Decodes a BER-OID (base-128, MSB continuation) integer; returns ``(value, off)``."""
    value = 0
    while offset < len(data):
        b = data[offset]
        offset += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return value, offset


def _decode_lat(raw: int) -> Optional[float]:
    return None if raw == _INT32_ERR else raw * 90.0 / _INT32_MAX


def _decode_lon(raw: int) -> Optional[float]:
    return None if raw == _INT32_ERR else raw * 180.0 / _INT32_MAX


def _decode_hae(raw: int) -> float:
    return _HAE_MIN + (raw * (_HAE_MAX - _HAE_MIN) / _UINT16_MAX)


def _parse_target_location(data: bytes) -> Optional[Dict[str, Any]]:
    """Decodes a VTarget Target Location pack (tag 17): lat(4) lon(4) [HAE(2) ...].

    Returns ``{"lat", "lon", "haeM"}`` or ``None`` if too short / lat-lon unresolvable.
    """
    if len(data) < 8:
        return None
    lat_raw = struct.unpack(">i", data[0:4])[0]
    lon_raw = struct.unpack(">i", data[4:8])[0]
    lat = _decode_lat(lat_raw)
    lon = _decode_lon(lon_raw)
    if lat is None or lon is None:
        return None
    hae = None
    if len(data) >= 10:
        hae = _decode_hae(struct.unpack(">H", data[8:10])[0])
    return {"lat": lat, "lon": lon, "haeM": hae}


def _parse_vtarget_pack(pack: bytes) -> Optional[Dict[str, Any]]:
    """Decodes one VTarget pack: BER-OID target id + nested VTarget Local Set."""
    if not pack:
        return None
    target_id, offset = parse_ber_oid(pack, 0)
    priority: Optional[int] = None
    confidence: Optional[int] = None
    location: Optional[Dict[str, Any]] = None
    end = len(pack)
    while offset < end:
        tag, offset = parse_ber_oid(pack, offset)
        if offset >= end:
            break
        length, offset = parse_ber_length(pack, offset)
        if offset + length > end:
            break
        value = pack[offset:offset + length]
        offset += length
        if tag == _VTARGET_TAG_PRIORITY and length >= 1:
            priority = value[0]
        elif tag == _VTARGET_TAG_CONFIDENCE and length >= 1:
            confidence = value[0]
        elif tag == _VTARGET_TAG_TARGET_LOCATION:
            location = _parse_target_location(value)
    if location is None:
        return None  # no absolute position → not materializable as a track
    return {
        "targetId": target_id,
        "lat": location["lat"],
        "lon": location["lon"],
        "haeM": location["haeM"],
        "confidence": confidence,
        "priority": priority,
    }


def _parse_vtarget_series(data: bytes) -> List[Dict[str, Any]]:
    """Decodes the VTarget Series (VMTI tag 101): concatenated length-prefixed packs.

    Each pack is a BER-length-prefixed VTarget pack (the on-wire ST 0903 framing).
    """
    targets: List[Dict[str, Any]] = []
    offset = 0
    end = len(data)
    while offset < end:
        pack_len, offset = parse_ber_length(data, offset)
        if offset + pack_len > end:
            break
        pack = data[offset:offset + pack_len]
        offset += pack_len
        t = _parse_vtarget_pack(pack)
        if t is not None:
            targets.append(t)
    return targets


def parse_vmti_local_set(data: bytes) -> List[Dict[str, Any]]:
    """Decodes a VMTI (ST 0903) Local Set body into a list of target dicts.

    ``data`` is the *value* of ST 0601 tag 74 (the nested VMTI Local Set, no key/length).
    Returns only targets with a resolvable absolute Target Location.
    """
    targets: List[Dict[str, Any]] = []
    offset = 0
    end = len(data)
    while offset < end:
        tag, offset = parse_ber_oid(data, offset)
        if offset >= end:
            break
        length, offset = parse_ber_length(data, offset)
        if offset + length > end:
            break
        value = data[offset:offset + length]
        offset += length
        if tag == _VMTI_TAG_VTARGET_SERIES:
            targets.extend(_parse_vtarget_series(value))
    return targets


def build_vmti_response(
    vmti_ls: Optional[bytes], age_ms: int, stream_name: str
) -> Dict[str, Any]:
    """Builds the SPASA ``/vmti/latest`` JSON from a raw VMTI Local Set body."""
    targets = parse_vmti_local_set(vmti_ls) if vmti_ls else []
    return {
        "streamName": stream_name,
        "present": bool(targets),
        "ageMs": age_ms,
        "targets": targets,
    }


# ── Encoders (test fixtures / synthetic ST 0903 generation) ──────────────────────────


def _encode_ber_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _encode_ber_oid(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    chunks = []
    while value > 0:
        chunks.append(value & 0x7F)
        value >>= 7
    chunks.reverse()
    return bytes([c | 0x80 for c in chunks[:-1]] + [chunks[-1]])


def _encode_target_location(lat: float, lon: float, hae_m: Optional[float]) -> bytes:
    lat_raw = int(round(lat * _INT32_MAX / 90.0))
    lon_raw = int(round(lon * _INT32_MAX / 180.0))
    out = struct.pack(">i", lat_raw) + struct.pack(">i", lon_raw)
    if hae_m is not None:
        hae_raw = int(round((hae_m - _HAE_MIN) * _UINT16_MAX / (_HAE_MAX - _HAE_MIN)))
        out += struct.pack(">H", max(0, min(_UINT16_MAX, hae_raw)))
    return out


def encode_vmti_local_set(targets: List[Dict[str, Any]]) -> bytes:
    """Encodes targets into a VMTI Local Set body (ST 0903 framing) for tests."""
    series = b""
    for t in targets:
        pack = _encode_ber_oid(int(t["targetId"]))
        if t.get("priority") is not None:
            pack += _encode_ber_oid(_VTARGET_TAG_PRIORITY) + _encode_ber_length(1) + bytes([int(t["priority"])])
        if t.get("confidence") is not None:
            pack += _encode_ber_oid(_VTARGET_TAG_CONFIDENCE) + _encode_ber_length(1) + bytes([int(t["confidence"])])
        if t.get("lat") is not None and t.get("lon") is not None:
            loc = _encode_target_location(t["lat"], t["lon"], t.get("haeM"))
            pack += _encode_ber_oid(_VTARGET_TAG_TARGET_LOCATION) + _encode_ber_length(len(loc)) + loc
        series += _encode_ber_length(len(pack)) + pack
    return _encode_ber_oid(_VMTI_TAG_VTARGET_SERIES) + _encode_ber_length(len(series)) + series
