"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Centralized input validation.

A single, strict ``is_valid_stream_name`` is reused across the HLS, test,
streams and recordings APIs so the rules can't drift between modules. The
rules are deliberately conservative (defence-in-depth against path traversal):
names must start and end with an alphanumeric, may contain only
``[A-Za-z0-9._-]`` in between, must not contain ``..`` and are length-capped.
"""
import ipaddress
import re
import urllib.parse

# Start and end alphanumeric; interior may include . _ - (single char allowed).
_STREAM_NAME_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')

# Default cap; stream names are short identifiers, not paths.
DEFAULT_MAX_STREAM_NAME = 64

# Protocols we are willing to pull an external stream from.
ALLOWED_SOURCE_SCHEMES = ('rtsp', 'rtsps', 'srt', 'http', 'https')


def is_valid_stream_name(name, max_len: int = DEFAULT_MAX_STREAM_NAME) -> bool:
    """Return True if ``name`` is a safe stream identifier.

    Rejects empties, over-long names, ``..`` (path traversal), leading/trailing
    separators, path separators, and any character outside ``[A-Za-z0-9._-]``.
    """
    if not name or not isinstance(name, str):
        return False
    if len(name) > max_len:
        return False
    if '..' in name:
        return False
    return bool(_STREAM_NAME_RE.match(name))


def validate_source_url(url, allowed_schemes=ALLOWED_SOURCE_SCHEMES):
    """Validate an external pull-source URL (anti-SSRF).

    Returns ``(ok: bool, error: str)``. Enforces a scheme allow-list and a host,
    and blocks obvious SSRF targets — cloud-metadata link-local (169.254/16),
    the unspecified address, multicast and reserved ranges. Private/LAN and
    loopback addresses are intentionally permitted: cameras and the local
    MediaMTX are legitimate sources. DNS resolution of hostnames is out of scope.
    """
    if not url or not isinstance(url, str):
        return False, 'URL required'
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False, 'Malformed URL'
    scheme = (parsed.scheme or '').lower()
    if scheme not in allowed_schemes:
        return False, (f'Unsupported protocol: {parsed.scheme}. '
                       f'Allowed: {", ".join(allowed_schemes)}')
    host = parsed.hostname
    if not host:
        return False, 'URL must include a host'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # hostname, not a literal IP — accepted
    if ip is not None and (ip.is_link_local or ip.is_unspecified
                           or ip.is_multicast or ip.is_reserved):
        return False, 'URL host is not allowed'
    return True, ''
