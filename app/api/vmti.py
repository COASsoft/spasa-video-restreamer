"""Live VMTI (MISB ST 0903) endpoint for the SPASA W2-T4 integration.

Exposes ``GET /api/streams/<name>/vmti/latest`` returning the latest decoded VMTI
detections from the live relay, normalized into the flat contract the SPASA server's
VMTI consumer ingests as C2 tracks (one track per moving-target detection, reusing the
track-bridge funnel — no V1/V3/V8 duplication).

VMTI rides the same UAS Datalink (ST 0601) stream as KLV (nested in tag 74), so this
reuses the per-stream KLV reader (one ffmpeg per stream serves both ``/klv/latest`` and
``/vmti/latest``) and decodes tag 74 with :func:`shared.vmti.parse_vmti_local_set`.

Read access is viewer-gated by the app's fail-closed RBAC default (a GET on an unlisted
``/api/`` route requires viewer); no explicit rule is needed.
"""

import binascii

from flask import Blueprint, jsonify

from app.api.klv import _KlvReader, _readers, _readers_lock, _MAX_READERS, _STALE_MS
from app.utils.validation import is_valid_stream_name
from shared.vmti import build_vmti_response

vmti_bp = Blueprint('vmti', __name__)

# ST 0601 tag 74 carries the nested VMTI Local Set; the KLV parser stores its raw bytes
# (hex) under this decoded-tag name.
_VMTI_TAG_NAME = 'VMTI Local Set'


def _get_reader(name):
    """Returns a live KLV reader for the stream (shared with ``/klv/latest``), or
    ``(None, response)`` if the global reader cap is reached."""
    with _readers_lock:
        reader = _readers.get(name)
        # Re-check liveness: a reader may be mid-exit from its idle timeout.
        if reader is None or not reader._thread.is_alive():
            if reader is None and len(_readers) >= _MAX_READERS:
                return None, (jsonify({
                    'streamName': name, 'present': False, 'ageMs': 0,
                    'error': 'KLV reader limit reached',
                }), 503)
            reader = _KlvReader(name)
            _readers[name] = reader
    return reader, None


@vmti_bp.route('/api/streams/<name>/vmti/latest', methods=['GET'])
def vmti_latest(name):
    """Latest decoded live VMTI detections for the stream (normalized for SPASA)."""
    if not is_valid_stream_name(name):
        return jsonify({'error': 'Invalid stream name'}), 400
    reader, err = _get_reader(name)
    if err is not None:
        return err
    snap = reader.snapshot()
    if snap is None:
        return jsonify({'streamName': name, 'present': False, 'ageMs': 0}), 200
    tags, age_ms = snap
    if age_ms > _STALE_MS:
        return jsonify({'streamName': name, 'present': False, 'ageMs': age_ms}), 200
    entry = tags.get(_VMTI_TAG_NAME)
    raw_hex = entry.get('raw_value') if entry else None
    vmti_ls = None
    if isinstance(raw_hex, str):
        try:
            vmti_ls = binascii.unhexlify(raw_hex)
        except (binascii.Error, ValueError):
            vmti_ls = None
    return jsonify(build_vmti_response(vmti_ls, age_ms, name)), 200
