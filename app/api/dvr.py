"""DVR ring-buffer for the SPASA F4 "clip the last N seconds" feature.

The base sidecar records forward-only, so "instant replay" was not possible. This adds an
opt-in continuous segmented ring recorder per stream: ffmpeg writes short rolling
segments (``-f segment -segment_wrap``) into ``data/dvr/<name>/``, keeping a bounded
window of the most recent footage. ``POST .../dvr/clip`` then concatenates the most
recent segments covering the requested duration into a normal recording under
``STREAMS_DIR/<name>/`` (so it shows up in the recordings list and is attachable to a
mission by the SPASA server).

Endpoints (under the streams path; mutating ones are operator-gated by the app's
fail-closed RBAC default, status is viewer):
  POST   /api/streams/<name>/dvr          start the ring recorder
  DELETE /api/streams/<name>/dvr          stop it
  GET    /api/streams/<name>/dvr/status   running + window seconds
  POST   /api/streams/<name>/dvr/clip     {seconds: N} -> concat last ~N s into a clip

The clip is approximate to within one segment duration at the leading edge (segment
granularity); it never exceeds the buffered window.
"""

import os
import subprocess
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from app.config import FFMPEG_LOG_DIR
from app.services.process import ManagedProcess, supervise
from app.utils.validation import is_valid_stream_name

dvr_bp = Blueprint('dvr', __name__)

_MEDIAMTX_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', 'rtsp://127.0.0.1:8554')
_STREAMS_DIR = os.environ.get('STREAMS_DIR', '/opt/app/streams')
_DVR_DIR = os.environ.get('DVR_DIR', '/opt/app/data/dvr')
_SEG_SECONDS = int(os.environ.get('DVR_SEGMENT_SECONDS', '2'))
_WINDOW_SECONDS = int(os.environ.get('DVR_WINDOW_SECONDS', '240'))
_SEG_WRAP = max(2, _WINDOW_SECONDS // max(1, _SEG_SECONDS))
_RW_TIMEOUT_US = '10000000'  # 10 s input timeout so a dead RTSP socket can't hang
# No fresh segment data for this long (after startup grace) means ffmpeg is alive but
# producing nothing — supervise() restarts it. Floored at 20 s so a long source GOP
# (sparse keyframes → infrequent segment cuts) is never mistaken for a stall.
_STALL_SECONDS = max(3 * _SEG_SECONDS, 20)
_MAX_CLIP_SECONDS = _WINDOW_SECONDS
_MAX_RECORDERS = 32

_recorders = {}
_recorders_lock = threading.Lock()


class _DvrRecorder:
    """Continuous segmented ring recorder for one stream.

    ffmpeg runs under the shared ManagedProcess/supervise infra (app.services.process),
    so its stderr is captured to ``FFMPEG_LOG_DIR/dvr-<name>.log`` (never DEVNULL) and it
    is restarted with capped backoff on exit or stall. ``health()`` lets callers tell
    "buffer still filling" from "ffmpeg keeps failing" and surface the real reason instead
    of an opaque 409.
    """

    def __init__(self, name):
        self.name = name
        self.seg_dir = os.path.join(_DVR_DIR, name)
        os.makedirs(self.seg_dir, exist_ok=True)
        self._stop = threading.Event()
        self._last_reason = None    # why ffmpeg last exited/stalled ('exited'|'stall')
        self._last_stderr = []      # tail of ffmpeg stderr captured at that moment
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _make_process(self):
        url = f"{_MEDIAMTX_RTSP_URL.rstrip('/')}/{self.name}"
        out = os.path.join(self.seg_dir, 'seg%05d.ts')
        cmd = [
            'ffmpeg', '-rtsp_transport', 'tcp', '-rw_timeout', _RW_TIMEOUT_US, '-i', url,
            '-map', '0', '-c', 'copy', '-f', 'segment',
            '-segment_time', str(_SEG_SECONDS), '-segment_wrap', str(_SEG_WRAP),
            '-segment_format', 'mpegts', '-reset_timestamps', '1', out,
        ]
        return ManagedProcess(f'dvr-{self.name}', cmd, FFMPEG_LOG_DIR,
                              label=f'dvr:{self.name}')

    def _is_stalled(self, _mp):
        # Past the startup grace, no fresh segment data means ffmpeg connected but is
        # producing nothing (or is wedged) — restart it so the buffer can recover.
        age = self._newest_age()
        return age is None or age > _STALL_SECONDS

    def _on_restart(self, reason, stderr_tail):
        # supervise() calls this with the dying process's stderr tail before each restart.
        self._last_reason = reason
        self._last_stderr = stderr_tail or []

    def _run(self):
        supervise(
            self._make_process,
            self._stop.is_set,
            is_stalled=self._is_stalled,
            on_restart=self._on_restart,
            startup_grace=_STALL_SECONDS,
        )

    def running(self):
        return self._thread.is_alive() and not self._stop.is_set()

    def stop(self):
        # supervise() polls this and tears the ffmpeg process down within ~0.25 s.
        self._stop.set()

    def _segments(self):
        """(mtime, path) for each buffered .ts segment, sorted oldest→newest."""
        try:
            entries = [
                (os.path.getmtime(os.path.join(self.seg_dir, f)),
                 os.path.join(self.seg_dir, f))
                for f in os.listdir(self.seg_dir) if f.endswith('.ts')
            ]
        except OSError:
            return []
        entries.sort()  # by mtime ascending
        return entries

    def _newest_age(self):
        """Seconds since the newest segment was last written, or None if none yet."""
        entries = self._segments()
        if not entries:
            return None
        return max(0.0, time.time() - entries[-1][0])

    def health(self):
        """Whether footage is actually being produced, plus the last failure reason."""
        entries = self._segments()  # one directory scan; derive age inline below
        age = max(0.0, time.time() - entries[-1][0]) if entries else None
        return {
            'segmentCount': len(entries),
            'newestAgeS': round(age, 1) if age is not None else None,
            'recording': age is not None and age <= _STALL_SECONDS,
            'lastReason': self._last_reason,
            'stderrTail': self._last_stderr[-5:],
        }

    def recent_segments(self, seconds):
        """Returns the most recent .ts segments (oldest→newest) covering `seconds`."""
        entries = self._segments()
        # Exclude the segment ffmpeg is currently writing (newest, may be partial).
        if len(entries) >= 2:
            entries = entries[:-1]
        count = max(1, (seconds + _SEG_SECONDS - 1) // _SEG_SECONDS + 1)
        return [path for _, path in entries[-count:]]


def _get_or_start(name):
    with _recorders_lock:
        rec = _recorders.get(name)
        if rec is None or not rec.running():
            if rec is None and len(_recorders) >= _MAX_RECORDERS:
                return None
            rec = _DvrRecorder(name)
            _recorders[name] = rec
        return rec


@dvr_bp.route('/api/streams/<name>/dvr', methods=['POST'])
def dvr_start(name):
    if not is_valid_stream_name(name):
        return jsonify({'error': 'Invalid stream name'}), 400
    rec = _get_or_start(name)
    if rec is None:
        return jsonify({'success': False, 'error': 'DVR recorder limit reached'}), 503
    return jsonify({'success': True, 'streamName': name,
                    'windowSeconds': _WINDOW_SECONDS}), 200


@dvr_bp.route('/api/streams/<name>/dvr', methods=['DELETE'])
def dvr_stop(name):
    if not is_valid_stream_name(name):
        return jsonify({'error': 'Invalid stream name'}), 400
    with _recorders_lock:
        rec = _recorders.pop(name, None)
    if rec is not None:
        rec.stop()
    return jsonify({'success': True}), 200


@dvr_bp.route('/api/streams/<name>/dvr/status', methods=['GET'])
def dvr_status(name):
    if not is_valid_stream_name(name):
        return jsonify({'error': 'Invalid stream name'}), 400
    with _recorders_lock:
        rec = _recorders.get(name)
    body = {
        'streamName': name,
        'running': bool(rec and rec.running()),
        'recording': False,
        'segmentCount': 0,
        'newestAgeS': None,
        'windowSeconds': _WINDOW_SECONDS,
        'segmentSeconds': _SEG_SECONDS,
    }
    if rec is not None:
        h = rec.health()
        body['recording'] = h['recording']
        body['segmentCount'] = h['segmentCount']
        body['newestAgeS'] = h['newestAgeS']
        # Running thread but no footage being produced — expose ffmpeg's real failure.
        if not h['recording'] and h['lastReason']:
            body['lastError'] = {'reason': h['lastReason'], 'stderr': h['stderrTail']}
    return jsonify(body), 200


@dvr_bp.route('/api/streams/<name>/dvr/clip', methods=['POST'])
def dvr_clip(name):
    if not is_valid_stream_name(name):
        return jsonify({'error': 'Invalid stream name'}), 400
    body = request.get_json(silent=True) or {}
    try:
        seconds = int(body.get('seconds', 30))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid seconds'}), 400
    if seconds < 1 or seconds > _MAX_CLIP_SECONDS:
        return jsonify({'error': f'seconds must be 1..{_MAX_CLIP_SECONDS}'}), 400

    with _recorders_lock:
        rec = _recorders.get(name)
    if rec is None or not rec.running():
        return jsonify({'success': False, 'error': 'DVR not running for this stream'}), 409
    segments = rec.recent_segments(seconds)
    if not segments:
        # Buffer empty: distinguish "still filling" from "ffmpeg is failing" so the
        # operator sees the real cause (RTSP refused, 404, codec, …) instead of an
        # opaque 409. The captured stderr tail is the actual ffmpeg diagnostic.
        h = rec.health()
        resp = {'success': False, 'error': 'No buffered footage yet', 'health': h}
        if h['lastReason']:
            resp['lastError'] = {'reason': h['lastReason'], 'stderr': h['stderrTail']}
        return jsonify(resp), 409

    out_dir = os.path.join(_STREAMS_DIR, name)
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    # Include a short random suffix so two clips in the same second never collide
    # (which would let `-y` overwrite one and a concurrent attach fetch wrong bytes).
    filename = f'clip-{ts}-{uuid.uuid4().hex[:8]}.mp4'
    out_path = os.path.join(out_dir, filename)

    # Concat the selected segments (stream-copy) and remux to MP4.
    list_path = os.path.join(rec.seg_dir, f'.concat-{ts}.txt')
    try:
        with open(list_path, 'w') as fh:
            for seg in segments:
                fh.write(f"file '{seg}'\n")
        cmd = [
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', list_path,
            '-c', 'copy', '-movflags', '+faststart', out_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=120)
    except Exception as e:
        return jsonify({'success': False, 'error': f'clip failed: {e}'}), 500
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass

    if proc.returncode != 0 or not os.path.exists(out_path):
        return jsonify({'success': False, 'error': 'ffmpeg concat failed'}), 500

    return jsonify({
        'success': True,
        'file': f'{name}/{filename}',
        'streamName': name,
        'requestedSeconds': seconds,
        'segments': len(segments),
    }), 200
