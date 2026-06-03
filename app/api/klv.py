"""Live KLV (STANAG 4609 / MISB 0601) endpoint for the SPASA F1 integration.

Exposes ``GET /api/streams/<name>/klv/latest`` returning the latest decoded KLV sample
from the live relay, normalized into the flat numeric contract the SPASA server's
``klv_cot`` module consumes (see ``docs/video-restreamer-full-feature-integration-plan.md``
in the SPASA repo). SPASA polls this once per reconcile cycle and broadcasts SPI /
sensor / footprint CoT to the feed's groups, so the whole team sees where the sensor is
looking without opening the video.

A per-stream background reader runs ``ffmpeg -map 0:d`` against
``rtsp://localhost:8554/<name>`` (the KLV survives the relay's ``-c copy``), frames the
raw data track on the UAS Datalink Local Set universal key, and decodes each packet with
the existing :class:`shared.klv.UnifiedKLVParser`, caching the most recent decode. Readers
self-stop after an idle period with no polls, so they cost nothing when unused.

Read access is viewer-gated by the app's fail-closed RBAC default (a GET on an
unlisted ``/api/`` route requires viewer); no explicit rule is needed.
"""

import math
import os
import subprocess
import threading
import time

from flask import Blueprint, jsonify

from shared.klv import UnifiedKLVParser
from app.utils.validation import is_valid_stream_name

klv_bp = Blueprint('klv', __name__)

# UAS Datalink Local Set universal key — start of each MISB 0601 packet.
_UAS_LS_KEY = (
    b'\x06\x0e\x2b\x34\x02\x0b\x01\x01\x0e\x01\x03\x01\x01\x00\x00\x00'
)

_MEDIAMTX_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', 'rtsp://127.0.0.1:8554')
_IDLE_TIMEOUT_S = 60      # stop the reader if nobody polls for this long
_STALE_MS = 5000          # a sample older than this is reported present=false
_MAX_BUF = 1_000_000      # cap the framing buffer
_MAX_PACKET = 256_000     # sane upper bound on one KLV packet (resync past garbage)
# ffmpeg input timeout (microseconds) so a silent-but-open RTSP socket cannot block
# the reader thread forever (defeating idle-stop / shutdown). Uses `-timeout` (the
# option the recordings path uses and this ffmpeg build accepts); `-rw_timeout` is
# rejected as "Option not found" by some builds for the RTSP demuxer.
_INPUT_TIMEOUT_US = '10000000'  # 10 s
_MAX_READERS = 64         # global cap on concurrent KLV reader processes

_readers = {}
_readers_lock = threading.Lock()


class _KlvReader:
    """Background ffmpeg reader + KLV decoder for one stream."""

    def __init__(self, name):
        self.name = name
        self.parser = UnifiedKLVParser(stream_name=name)
        self.latest = None       # dict of decoded tags
        self.latest_ts = 0.0     # monotonic seconds
        self.last_poll = time.monotonic()
        self._stop = threading.Event()
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _idle(self):
        return time.monotonic() - self.last_poll > _IDLE_TIMEOUT_S

    def _run(self):
        url = f"{_MEDIAMTX_RTSP_URL.rstrip('/')}/{self.name}"
        cmd = [
            'ffmpeg', '-rtsp_transport', 'tcp', '-timeout', _INPUT_TIMEOUT_US,
            '-i', url, '-map', '0:d', '-c', 'copy', '-f', 'data', '-',
        ]
        while not self._stop.is_set() and not self._idle():
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
            except Exception:
                time.sleep(2)
                continue
            buf = b''
            try:
                while not self._stop.is_set() and not self._idle():
                    chunk = self._proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    buf = self._consume(buf)
                    if len(buf) > _MAX_BUF:
                        buf = buf[-65536:]
            except Exception:
                pass
            finally:
                self._terminate_proc()
            if not self._stop.is_set():
                time.sleep(1)  # reconnect backoff
        self._terminate_proc()
        with _readers_lock:
            _readers.pop(self.name, None)

    def _consume(self, buf):
        """Frame complete KLV packets by key + BER length and decode each.

        Framing on the BER length (not key-to-next-key) is correct even when a tag
        value happens to contain the 16-byte universal key, and decodes the freshest
        packet as soon as it is fully buffered rather than waiting for the next key.
        """
        while True:
            start = buf.find(_UAS_LS_KEY)
            if start < 0:
                # Keep a 15-byte tail so a key split across reads isn't lost.
                return buf[-15:] if len(buf) > 15 else buf
            length_off = start + len(_UAS_LS_KEY)
            if length_off >= len(buf):
                return buf[start:]  # need the length byte
            first = buf[length_off]
            if first < 0x80:
                val_len = first
                hdr_end = length_off + 1
            else:
                n = first & 0x7F
                if length_off + 1 + n > len(buf):
                    return buf[start:]  # need the full long-form length
                val_len = int.from_bytes(buf[length_off + 1:length_off + 1 + n], 'big')
                hdr_end = length_off + 1 + n
            if val_len > _MAX_PACKET:
                # Corrupt/implausible length — skip this key and resync.
                buf = buf[length_off:]
                continue
            packet_end = hdr_end + val_len
            if packet_end > len(buf):
                return buf[start:]  # incomplete; wait for more bytes
            self._decode(buf[start:packet_end])
            buf = buf[packet_end:]

    def _decode(self, packet):
        try:
            res = self.parser.parse_klv_packet(packet)
            tags = res.get('tags') or {}
            if tags:
                self.latest = tags
                self.latest_ts = time.monotonic()
        except Exception:
            pass

    def _terminate_proc(self):
        p = self._proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._proc = None

    def snapshot(self):
        self.last_poll = time.monotonic()
        if self.latest is None:
            return None
        age_ms = int((time.monotonic() - self.latest_ts) * 1000)
        return self.latest, age_ms

    def stop(self):
        self._stop.set()
        self._terminate_proc()


def _num(tags, name):
    """Returns a finite numeric decoded tag value, or None.

    Non-finite values (NaN/Inf from a corrupt decode) are dropped — they are not valid
    JSON for the strict parser on the SPASA side and would fail the whole sample.
    """
    entry = tags.get(name)
    if not entry:
        return None
    value = entry.get('value')
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _corners(tags, fc_lat, fc_lon):
    """Footprint corners as [[lat,lon],...]; prefers absolute (82-89) then offsets."""
    full = [
        (_num(tags, f'Corner Latitude Point {i} (Full)'),
         _num(tags, f'Corner Longitude Point {i} (Full)'))
        for i in (1, 2, 3, 4)
    ]
    if all(a is not None and b is not None for a, b in full):
        return [[a, b] for a, b in full]
    if fc_lat is None or fc_lon is None:
        return None
    offs = [
        (_num(tags, f'Offset Corner Latitude Point {i}'),
         _num(tags, f'Offset Corner Longitude Point {i}'))
        for i in (1, 2, 3, 4)
    ]
    if all(a is not None and b is not None for a, b in offs):
        return [[fc_lat + a, fc_lon + b] for a, b in offs]
    return None


def _normalize(tags, name, age_ms):
    fc_lat = _num(tags, 'Frame Center Latitude')
    fc_lon = _num(tags, 'Frame Center Longitude')
    return {
        'streamName': name,
        'present': True,
        'ageMs': age_ms,
        'sensorLat': _num(tags, 'Sensor Latitude'),
        'sensorLon': _num(tags, 'Sensor Longitude'),
        'sensorAltM': _num(tags, 'Sensor True Altitude'),
        'frameCenterLat': fc_lat,
        'frameCenterLon': fc_lon,
        'frameCenterElevM': _num(tags, 'Frame Center Elevation'),
        'platformHeadingDeg': _num(tags, 'Platform Heading Angle'),
        'sensorRelAzDeg': _num(tags, 'Sensor Relative Azimuth Angle'),
        'sensorRelElDeg': _num(tags, 'Sensor Relative Elevation Angle'),
        'hfovDeg': _num(tags, 'Sensor Horizontal Field of View'),
        'vfovDeg': _num(tags, 'Sensor Vertical Field of View'),
        'slantRangeM': _num(tags, 'Slant Range'),
        'corners': _corners(tags, fc_lat, fc_lon),
    }


@klv_bp.route('/api/streams/<name>/klv/latest', methods=['GET'])
def klv_latest(name):
    """Latest decoded live-KLV sample for the stream (normalized for SPASA)."""
    if not is_valid_stream_name(name):
        return jsonify({'error': 'Invalid stream name'}), 400
    with _readers_lock:
        reader = _readers.get(name)
        # Re-check liveness: a reader may be mid-exit from its idle timeout.
        if reader is None or not reader._thread.is_alive():
            if reader is None and len(_readers) >= _MAX_READERS:
                return jsonify({
                    'streamName': name, 'present': False, 'ageMs': 0,
                    'error': 'KLV reader limit reached',
                }), 503
            reader = _KlvReader(name)
            _readers[name] = reader
    snap = reader.snapshot()
    if snap is None:
        return jsonify({'streamName': name, 'present': False, 'ageMs': 0}), 200
    tags, age_ms = snap
    if age_ms > _STALE_MS:
        return jsonify({'streamName': name, 'present': False, 'ageMs': age_ms}), 200
    return jsonify(_normalize(tags, name, age_ms)), 200
