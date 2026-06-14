"""ONVIF discovery helpers for the SPASA W3-T2 integration.

Pure (no socket/network) building blocks for:
  * WS-Discovery — multicast SOAP Probe for ONVIF ``NetworkVideoTransmitter`` devices and
    parsing of the ``ProbeMatches`` responses (device service address + friendly name).
  * ONVIF Media (ver10) — ``GetProfiles`` / ``GetStreamUri`` SOAP request builders and
    response parsers to resolve a camera's RTSP stream URL.

The network I/O (multicast send/recv, HTTP POST to the device) lives in
``app/api/onvif.py``; keeping the SOAP construction/parsing pure here makes it unit-
testable without hardware (mirrors ``shared/vmti.py`` / ``shared/security.py``).
"""

import urllib.parse
import xml.etree.ElementTree as ET
from typing import List, Optional

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)

_PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
    ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
    ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header>"
    "<w:MessageID>uuid:{message_id}</w:MessageID>"
    "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
    "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
    "</e:Header>"
    "<e:Body>"
    "<d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>"
    "</e:Body></e:Envelope>"
)


def build_probe(message_id: str) -> bytes:
    """Builds a WS-Discovery Probe for ONVIF transmitters (UTF-8 bytes for the socket)."""
    return _PROBE_TEMPLATE.format(message_id=message_id).encode("utf-8")


def _localname(tag: str) -> str:
    """Strips the XML namespace from a tag (``{ns}Local`` → ``Local``)."""
    return tag.rsplit("}", 1)[-1]


def _iter_local(root: ET.Element, name: str):
    """Yields all descendants whose local name equals ``name`` (namespace-agnostic)."""
    for el in root.iter():
        if _localname(el.tag) == name:
            yield el


def parse_probe_matches(xml: str) -> List[dict]:
    """Parses a ``ProbeMatches`` SOAP envelope into ``[{"xaddrs": [...], "scopes": [...]}]``.

    Namespace-agnostic (devices vary in prefixes). Malformed XML ⇒ empty list."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for match in _iter_local(root, "ProbeMatch"):
        xaddrs = []
        scopes = []
        for el in _iter_local(match, "XAddrs"):
            if el.text:
                xaddrs.extend(el.text.split())
        for el in _iter_local(match, "Scopes"):
            if el.text:
                scopes.extend(el.text.split())
        if xaddrs:
            out.append({"xaddrs": xaddrs, "scopes": scopes})
    return out


def name_from_scopes(scopes: List[str]) -> Optional[str]:
    """Extracts the friendly name from ONVIF scopes (``onvif://.../name/<URL-encoded>``)."""
    for scope in scopes:
        marker = "/name/"
        idx = scope.find(marker)
        if idx != -1:
            raw = scope[idx + len(marker):].strip("/")
            if raw:
                return urllib.parse.unquote(raw)
    return None


# ── ONVIF Media (ver10) — RTSP URL resolution ──

_GET_PROFILES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl">'
    "<s:Body><trt:GetProfiles/></s:Body></s:Envelope>"
)

_GET_STREAM_URI_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
    ' xmlns:tt="http://www.onvif.org/ver10/schema">'
    "<s:Body><trt:GetStreamUri>"
    "<trt:StreamSetup>"
    "<tt:Stream>RTP-Unicast</tt:Stream>"
    '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
    "</trt:StreamSetup>"
    "<trt:ProfileToken>{token}</trt:ProfileToken>"
    "</trt:GetStreamUri></s:Body></s:Envelope>"
)


def build_get_profiles() -> bytes:
    """Builds the ONVIF Media ``GetProfiles`` SOAP request."""
    return _GET_PROFILES.encode("utf-8")


def parse_profiles(xml: str) -> List[str]:
    """Returns the profile tokens from a ``GetProfilesResponse`` (the ``token`` attribute
    of each ``Profiles`` element). Malformed XML ⇒ empty list."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    tokens = []
    for el in _iter_local(root, "Profiles"):
        token = el.get("token")
        if token:
            tokens.append(token)
    return tokens


def build_get_stream_uri(profile_token: str) -> bytes:
    """Builds the ONVIF Media ``GetStreamUri`` SOAP request for a profile (RTP-Unicast/RTSP)."""
    safe = profile_token.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _GET_STREAM_URI_TEMPLATE.format(token=safe).encode("utf-8")


def parse_stream_uri(xml: str) -> Optional[str]:
    """Returns the RTSP URL from a ``GetStreamUriResponse`` (its ``Uri`` element), or None."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    for el in _iter_local(root, "Uri"):
        if el.text and el.text.strip():
            return el.text.strip()
    return None
