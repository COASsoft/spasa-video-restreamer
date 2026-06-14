"""ONVIF discovery endpoint for the SPASA W3-T2 integration.

Exposes ``GET /api/onvif/discover`` returning cameras found on the LAN via WS-Discovery,
each with a resolved RTSP URL, in the flat contract the SPASA server's ONVIF onboarding
(`POST /restreamer/onvif/add`) consumes:

    [{"name": "...", "rtspUrl": "rtsp://...", "profile": "Profile_1"}]

The SOAP building/parsing is pure and unit-tested in :mod:`shared.onvif`; this module is
the network glue (UDP multicast probe + per-device ONVIF Media HTTP). It degrades
cleanly: a device whose RTSP URL can't be resolved is still listed (``rtspUrl`` empty),
and a host without any ONVIF cameras yields ``[]`` rather than an error.

Read access is viewer-gated by the app's fail-closed RBAC default (a GET on an unlisted
``/api/`` route requires viewer); no explicit rule is needed.
"""

import socket
import uuid

import requests
from flask import Blueprint, jsonify

from shared.onvif import (
    WS_DISCOVERY_ADDR,
    build_get_profiles,
    build_get_stream_uri,
    build_probe,
    name_from_scopes,
    parse_probe_matches,
    parse_profiles,
    parse_stream_uri,
)

onvif_bp = Blueprint('onvif', __name__)

# WS-Discovery is best-effort and bounded so the endpoint never hangs the caller.
_DISCOVERY_TIMEOUT_S = 3.0
_DEVICE_HTTP_TIMEOUT_S = 4.0
_SOAP_HEADERS = {'Content-Type': 'application/soap+xml; charset=utf-8'}


def _discover_devices(timeout=_DISCOVERY_TIMEOUT_S):
    """Multicasts a WS-Discovery probe and returns ``[{"xaddrs": [...], "scopes": [...]}]``
    for every responding ONVIF transmitter. Best-effort: socket errors ⇒ empty list."""
    probe = build_probe(str(uuid.uuid4()))
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        sock.sendto(probe, WS_DISCOVERY_ADDR)
        devices = []
        seen = set()
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            for match in parse_probe_matches(data.decode('utf-8', 'ignore')):
                key = tuple(match['xaddrs'])
                if key not in seen:
                    seen.add(key)
                    devices.append(match)
        return devices
    except OSError:
        return []
    finally:
        if sock is not None:
            sock.close()


def _resolve_rtsp(device_service_url):
    """Best-effort ONVIF Media resolution of a camera's RTSP URL from its device-service
    XAddr. Returns ``(rtsp_url, profile_token)`` or ``(None, None)`` on any failure
    (anonymous SOAP; cameras that require auth degrade to no URL — the operator can fill
    it in before adding)."""
    try:
        resp = requests.post(
            device_service_url,
            data=build_get_profiles(),
            headers=_SOAP_HEADERS,
            timeout=_DEVICE_HTTP_TIMEOUT_S,
        )
        tokens = parse_profiles(resp.text) if resp.ok else []
        if not tokens:
            return None, None
        token = tokens[0]
        resp = requests.post(
            device_service_url,
            data=build_get_stream_uri(token),
            headers=_SOAP_HEADERS,
            timeout=_DEVICE_HTTP_TIMEOUT_S,
        )
        if not resp.ok:
            return None, token
        return parse_stream_uri(resp.text), token
    except requests.RequestException:
        return None, None


@onvif_bp.route('/api/onvif/discover', methods=['GET'])
def onvif_discover():
    """Cameras found on the LAN via WS-Discovery, with resolved RTSP URLs (best-effort)."""
    cameras = []
    for device in _discover_devices():
        xaddr = device['xaddrs'][0]
        name = name_from_scopes(device['scopes']) or xaddr
        rtsp_url, profile = _resolve_rtsp(xaddr)
        cameras.append({
            'name': name,
            'rtspUrl': rtsp_url or '',
            'profile': profile,
        })
    return jsonify(cameras), 200
