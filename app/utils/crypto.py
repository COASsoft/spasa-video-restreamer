"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Centralized cryptographic helpers (FIPS-ready).

Every hash, HMAC, token and constant-time comparison in the app routes through
this module. This keeps the cryptographic surface small and auditable, and
makes enabling a FIPS-validated provider later a single contained change
rather than a hunt across the codebase. Only FIPS 140-approved primitives are
used here — SHA-256, HMAC-SHA-256, and the OS CSPRNG (``secrets``/``os.urandom``).
No MD5 or SHA-1-for-security anywhere.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Union

Bytesish = Union[str, bytes]

# TLS posture surfaced to reverse-proxy / MediaMTX config and any in-process
# ssl context. TLS 1.2 is the floor; 1.3 preferred. (FIPS-approved suites.)
TLS_MIN_VERSION = 'TLSv1.2'
APPROVED_TLS_CIPHERS = (
    'ECDHE-ECDSA-AES256-GCM-SHA384:'
    'ECDHE-RSA-AES256-GCM-SHA384:'
    'ECDHE-ECDSA-AES128-GCM-SHA256:'
    'ECDHE-RSA-AES128-GCM-SHA256'
)


def _as_bytes(data: Bytesish) -> bytes:
    return data.encode('utf-8') if isinstance(data, str) else data


def sha256_hex(data: Bytesish) -> str:
    """FIPS-approved SHA-256 hex digest."""
    return hashlib.sha256(_as_bytes(data)).hexdigest()


def hmac_sha256_hex(key: Bytesish, message: Bytesish) -> str:
    """FIPS-approved HMAC-SHA-256 hex digest."""
    return hmac.new(_as_bytes(key), _as_bytes(message), hashlib.sha256).hexdigest()


def constant_time_equals(a: Bytesish, b: Bytesish) -> bool:
    """Timing-attack-resistant comparison."""
    return hmac.compare_digest(_as_bytes(a), _as_bytes(b))


def generate_token(nbytes: int = 24, prefix: str = '') -> str:
    """Generate a cryptographically secure random token (hex), optional prefix."""
    return f"{prefix}{secrets.token_hex(nbytes)}"
