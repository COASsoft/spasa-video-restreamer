"""Unit tests for the ONVIF discovery helpers (``shared/onvif.py``).

Pure tests — no socket / HTTP / network. Validate the WS-Discovery probe + ProbeMatches
parsing and the ONVIF Media SOAP builders/parsers the SPASA server (W3-T2) consumes.
"""

from shared.onvif import (
    build_get_profiles,
    build_get_stream_uri,
    build_probe,
    name_from_scopes,
    parse_probe_matches,
    parse_profiles,
    parse_stream_uri,
)


def test_build_probe_is_ws_discovery_probe():
    msg = build_probe("abc-123").decode("utf-8")
    assert "uuid:abc-123" in msg
    assert "ws/2005/04/discovery/Probe" in msg
    assert "NetworkVideoTransmitter" in msg


_PROBE_MATCHES = """<?xml version="1.0"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <SOAP-ENV:Body>
  <d:ProbeMatches>
   <d:ProbeMatch>
    <d:Scopes>onvif://www.onvif.org/name/Axis%20P1448 onvif://www.onvif.org/hardware/P1448</d:Scopes>
    <d:XAddrs>http://10.0.0.5/onvif/device_service http://[fe80::1]/onvif/device_service</d:XAddrs>
   </d:ProbeMatch>
   <d:ProbeMatch>
    <d:Scopes>onvif://www.onvif.org/name/Hikvision</d:Scopes>
    <d:XAddrs>http://10.0.0.6/onvif/device_service</d:XAddrs>
   </d:ProbeMatch>
  </d:ProbeMatches>
 </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def test_parse_probe_matches_two_cameras():
    matches = parse_probe_matches(_PROBE_MATCHES)
    assert len(matches) == 2
    assert matches[0]["xaddrs"][0] == "http://10.0.0.5/onvif/device_service"
    assert len(matches[0]["xaddrs"]) == 2  # IPv4 + IPv6
    assert name_from_scopes(matches[0]["scopes"]) == "Axis P1448"  # URL-decoded
    assert name_from_scopes(matches[1]["scopes"]) == "Hikvision"


def test_parse_probe_matches_malformed_is_empty():
    assert parse_probe_matches("not xml <<<") == []
    # Well-formed but no matches.
    assert parse_probe_matches("<a><b/></a>") == []


def test_name_from_scopes_absent():
    assert name_from_scopes(["onvif://www.onvif.org/hardware/X"]) is None
    assert name_from_scopes([]) is None


def test_get_profiles_request():
    body = build_get_profiles().decode("utf-8")
    assert "GetProfiles" in body
    assert "media/wsdl" in body


_PROFILES_RESPONSE = """<?xml version="1.0"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
 <env:Body>
  <trt:GetProfilesResponse>
   <trt:Profiles token="Profile_1"><Name>mainstream</Name></trt:Profiles>
   <trt:Profiles token="Profile_2"><Name>substream</Name></trt:Profiles>
  </trt:GetProfilesResponse>
 </env:Body>
</env:Envelope>"""


def test_parse_profiles_tokens():
    assert parse_profiles(_PROFILES_RESPONSE) == ["Profile_1", "Profile_2"]
    assert parse_profiles("garbage") == []


def test_build_get_stream_uri_embeds_token_and_escapes():
    body = build_get_stream_uri("Profile_1").decode("utf-8")
    assert "<trt:ProfileToken>Profile_1</trt:ProfileToken>" in body
    assert "RTP-Unicast" in body
    # Token with XML metachars is escaped (no raw injection).
    body2 = build_get_stream_uri("a<b&c").decode("utf-8")
    assert "a&lt;b&amp;c" in body2


_STREAM_URI_RESPONSE = """<?xml version="1.0"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema">
 <env:Body>
  <trt:GetStreamUriResponse>
   <trt:MediaUri>
    <tt:Uri>rtsp://10.0.0.5:554/Streaming/Channels/101</tt:Uri>
   </trt:MediaUri>
  </trt:GetStreamUriResponse>
 </env:Body>
</env:Envelope>"""


def test_parse_stream_uri():
    assert parse_stream_uri(_STREAM_URI_RESPONSE) == "rtsp://10.0.0.5:554/Streaming/Channels/101"
    assert parse_stream_uri("nope") is None
