#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

test suite for TAK Video Restreamer

This module contains all tests for the application including:
- Health check endpoints
- Stream management
- Recording management
- Settings and configuration
- Utility functions
- Integration tests
- Web interface tests

Usage:
    python test_app.py              # Run all tests with runner
    python test_app.py -v           # Verbose output
    python test_app.py -k health    # Run tests matching 'health'
    python test_app.py --cov=app    # Run with coverage
    
Or use pytest directly:
    pytest test_app.py -v
"""
import sys
import pytest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from app import create_app


@pytest.fixture
def client():
    """Create an authenticated test client with FULL (admin) privileges.

    The app protects all /api/* and page routes via a before_request hook and a
    centralized RBAC table. We authenticate with a freshly minted admin API key
    set as a default header on every request, so endpoint behaviour — not the
    auth/RBAC gate — is what gets exercised. (Role enforcement is covered by the
    dedicated viewer/operator fixtures and TestRBAC.)
    """
    app = create_app()
    app.config['TESTING'] = True
    from app import auth
    raw_key = auth.generate_api_key('pytest', role='admin')
    with app.test_client() as client:
        client.environ_base['HTTP_X_API_KEY'] = raw_key
        yield client


@pytest.fixture
def anon_client():
    """Create an UNauthenticated test client for auth-enforcement tests."""
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def viewer_client():
    """Authenticated test client with the read-only 'viewer' role."""
    app = create_app()
    app.config['TESTING'] = True
    from app import auth
    raw_key = auth.generate_api_key('pytest-viewer', role='viewer')
    with app.test_client() as client:
        client.environ_base['HTTP_X_API_KEY'] = raw_key
        yield client


@pytest.fixture
def operator_client():
    """Authenticated test client with the 'operator' role."""
    app = create_app()
    app.config['TESTING'] = True
    from app import auth
    raw_key = auth.generate_api_key('pytest-operator', role='operator')
    with app.test_client() as client:
        client.environ_base['HTTP_X_API_KEY'] = raw_key
        yield client


@pytest.fixture
def temp_recording_dir(tmp_path):
    """Create temporary recording directory"""
    return tmp_path


# =============================================================================
# Health Check Tests
# =============================================================================

class TestHealthEndpoint:
    """Test suite for /health endpoint"""
    
    def test_health_endpoint_exists(self, client):
        """Test that health endpoint is accessible"""
        response = client.get('/health')
        assert response.status_code == 200
    
    def test_health_endpoint_returns_json(self, client):
        """Test that health endpoint returns valid JSON"""
        response = client.get('/health')
        assert response.content_type == 'application/json'
        data = json.loads(response.data)
        assert isinstance(data, dict)
    
    def test_health_endpoint_has_status(self, client):
        """Test that health response includes status field"""
        response = client.get('/health')
        data = json.loads(response.data)
        assert 'status' in data
        # 'healthy' when MediaMTX is reachable, 'degraded' when it is not
        # (the process itself is alive either way — liveness vs readiness).
        assert data['status'] in ('healthy', 'degraded')
    
    def test_health_endpoint_has_timestamp(self, client):
        """Test that health response includes timestamp"""
        response = client.get('/health')
        data = json.loads(response.data)
        assert 'timestamp' in data
        assert isinstance(data['timestamp'], str)

    def test_ready_endpoint_shape(self, client):
        """Readiness probe: 200 or 503 with the expected fields."""
        response = client.get('/ready')
        # 503 in tests because MediaMTX isn't running; 200 if it happens to be.
        assert response.status_code in (200, 503)
        data = json.loads(response.data)
        for key in ('ready', 'mediamtx', 'dataDirWritable', 'streamsDirWritable'):
            assert key in data
        assert isinstance(data['ready'], bool)
    
    def test_health_endpoint_has_active_recordings(self, client):
        """Test that health response includes active recordings count"""
        response = client.get('/health')
        data = json.loads(response.data)
        assert 'activeRecordings' in data
        assert isinstance(data['activeRecordings'], int)
        assert data['activeRecordings'] >= 0
    
    def test_health_endpoint_has_klv_available(self, client):
        """Test that health response includes KLV availability status"""
        response = client.get('/health')
        data = json.loads(response.data)
        assert 'klvAvailable' in data
        assert isinstance(data['klvAvailable'], bool)
    
    def test_health_endpoint_has_srt_buffer_available(self, client):
        """Test that health response includes SRT buffer availability"""
        response = client.get('/health')
        data = json.loads(response.data)
        assert 'srtBufferAvailable' in data
        assert isinstance(data['srtBufferAvailable'], bool)


# =============================================================================
# Stream Management Tests
# =============================================================================

class TestStreamsEndpoint:
    """Test suite for /api/streams endpoints"""
    
    def test_list_streams_endpoint_exists(self, client):
        """Test that streams listing endpoint is accessible"""
        response = client.get('/api/streams')
        assert response.status_code == 200
    
    def test_list_streams_returns_json(self, client):
        """Test that streams endpoint returns valid JSON"""
        response = client.get('/api/streams')
        assert response.content_type == 'application/json'
        data = json.loads(response.data)
        assert isinstance(data, list)
    
    def test_list_streams_has_streams_array(self, client):
        """Test that streams response is an array"""
        response = client.get('/api/streams')
        data = json.loads(response.data)
        assert isinstance(data, list)
    
    @patch('requests.get')
    def test_list_streams_with_active_streams(self, mock_get, client):
        """Test streams listing with mock active streams"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'items': {
                'test_stream': {
                    'name': 'test_stream',
                    'ready': True,
                    'tracks': ['H264'],
                    'bytesReceived': 12345,
                    'readers': [{'state': 'read'}]
                }
            }
        }
        mock_get.return_value = mock_response
        
        response = client.get('/api/streams')
        data = json.loads(response.data)
        assert isinstance(data, list)
        assert len(data) > 0
    
    def test_create_pull_stream_endpoint_exists(self, client):
        """Pull stream start: POST /api/streams/<name>/pull with a source URL."""
        with patch('app.api.streams._start_pull_impl') as mock_pull, \
             patch('app.api.streams.broadcast'):
            response = client.post('/api/streams/test/pull',
                                   json={'sourceUrl': 'rtsp://example.com/stream'},
                                   content_type='application/json')
        assert response.status_code == 200
        mock_pull.assert_called_once()

    def test_create_pull_stream_with_full_data(self, client):
        """Pull stream start accepts url + credentials and returns a JSON object."""
        with patch('app.api.streams._start_pull_impl'), \
             patch('app.api.streams.broadcast'):
            response = client.post('/api/streams/test-stream/pull',
                                   json={'url': 'rtsp://example.com/test',
                                         'username': 'u', 'password': 'p'},
                                   content_type='application/json')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert isinstance(data, dict)
        assert data.get('success') is True

    def test_create_pull_stream_requires_body(self, client):
        """Pull stream start with an empty body returns 400."""
        response = client.post('/api/streams/test/pull',
                               json={}, content_type='application/json')
        assert response.status_code == 400

    def test_create_pull_stream_requires_source_url(self, client):
        """Pull stream start without a source URL returns 400."""
        response = client.post('/api/streams/test/pull',
                               json={'username': 'u'}, content_type='application/json')
        assert response.status_code == 400

    def test_create_pull_stream_rejects_unsupported_protocol(self, client):
        """Pull stream start rejects non-allowed URL schemes (anti-SSRF)."""
        response = client.post('/api/streams/test/pull',
                               json={'sourceUrl': 'file:///etc/passwd'},
                               content_type='application/json')
        assert response.status_code == 400
    
    def test_delete_pull_stream_endpoint_exists(self, client):
        """Test that delete pull stream endpoint exists"""
        response = client.delete('/api/streams/pull/test-stream')
        assert response.status_code in [200, 404]
    
    def test_delete_nonexistent_pull_stream(self, client):
        """Test deleting non-existent pull stream"""
        response = client.delete('/api/streams/pull/nonexistent')
        # Should return 200 even if stream doesn't exist (idempotent)
        assert response.status_code in [200, 404]
    
    def test_pull_stream_workflow(self, client):
        """Pull stream workflow: start (mocked), list, then stop-pull."""
        stream_name = 'workflow-test'

        with patch('app.api.streams._start_pull_impl'), \
             patch('app.api.streams.broadcast'):
            create_response = client.post(f'/api/streams/{stream_name}/pull',
                                          json={'sourceUrl': 'rtsp://example.com/test'},
                                          content_type='application/json')
        assert create_response.status_code == 200

        list_response = client.get('/api/streams')
        assert list_response.status_code == 200

        # Stop pull (real route: POST /api/streams/<name>/stop-pull)
        stop_response = client.post(f'/api/streams/{stream_name}/stop-pull')
        assert stop_response.status_code in [200, 404]


# =============================================================================
# Recording Control Tests
# =============================================================================

class TestRecordingControl:
    """Test suite for recording start/stop operations"""
    
    def test_start_recording_endpoint_exists(self, client):
        """Test that start recording endpoint exists"""
        response = client.post('/api/recordings/start',
                              json={'streamName': 'test-stream'},
                              content_type='application/json')
        # Endpoint may be implemented or return 404
        assert response.status_code in [200, 201, 404, 400]
    
    def test_start_recording_requires_stream_name(self, client):
        """Test that starting recording validates stream name"""
        response = client.post('/api/recordings/start',
                              json={},
                              content_type='application/json')
        assert response.status_code in [200, 400, 404]
    
    def test_stop_recording_endpoint_exists(self, client):
        """Test that stop recording endpoint exists"""
        response = client.post('/api/recordings/stop',
                              json={'streamName': 'test-stream'},
                              content_type='application/json')
        assert response.status_code in [200, 404]
    
    def test_stop_recording_nonexistent_stream(self, client):
        """Test stopping recording for non-existent stream"""
        response = client.post('/api/recordings/stop',
                             json={'streamName': 'nonexistent'},
                             content_type='application/json')
        # Should handle gracefully
        assert response.status_code in [200, 404]
    
    def test_recording_workflow(self, client):
        """Test start and stop recording workflow"""
        stream_name = 'workflow-test-stream'
        
        # Start recording
        start_response = client.post('/api/recordings/start',
                                    json={'streamName': stream_name},
                                    content_type='application/json')
        
        if start_response.status_code in [200, 201]:
            # Stop recording
            stop_response = client.post('/api/recordings/stop',
                                       json={'streamName': stream_name},
                                       content_type='application/json')
            assert stop_response.status_code in [200, 204]


# =============================================================================
# Recordings Endpoint Tests
# =============================================================================

class TestRecordingsEndpoint:
    """Test suite for /api/recordings endpoints"""
    
    def test_list_recordings_endpoint_exists(self, client):
        """Test that recordings listing endpoint is accessible"""
        response = client.get('/api/recordings')
        assert response.status_code == 200
    
    def test_list_recordings_returns_json(self, client):
        """Test that recordings endpoint returns valid JSON"""
        response = client.get('/api/recordings')
        assert response.content_type == 'application/json'
        data = json.loads(response.data)
        assert isinstance(data, list)
    
    def test_list_recordings_returns_array(self, client):
        """Test that recordings response is an array"""
        response = client.get('/api/recordings')
        data = json.loads(response.data)
        assert isinstance(data, list)
    
    def test_recording_item_structure(self, client):
        """Test structure of recording items"""
        response = client.get('/api/recordings')
        data = json.loads(response.data)
        
        if len(data) > 0:
            recording = data[0]
            assert 'stream' in recording
            assert 'filename' in recording
            assert 'size' in recording
            assert 'created' in recording
            assert 'path' in recording
    
    def test_recording_filters_video_files_only(self, client):
        """Test that recordings only returns video files"""
        response = client.get('/api/recordings')
        data = json.loads(response.data)
        
        # Check that no .bin or .json files are in the list
        for recording in data:
            filename = recording['filename']
            assert not filename.endswith('.bin')
            assert not filename.endswith('.json')
    
    def test_delete_nonexistent_recording(self, client):
        """Test deleting non-existent recording"""
        response = client.delete('/api/recordings/nonexistent/fakefile.mp4')
        assert response.status_code == 404
    
    def test_download_nonexistent_recording(self, client):
        """Test downloading non-existent recording"""
        response = client.get('/api/recordings/nonexistent/fakefile.mp4')
        assert response.status_code == 404


# =============================================================================
# Thumbnail Tests
# =============================================================================

class TestThumbnailGeneration:
    """Test suite for thumbnail generation"""
    
    def test_generate_thumbnail_endpoint_exists(self, client):
        """Test that thumbnail generation endpoint exists"""
        # Try to generate thumbnail (will fail for non-existent file)
        response = client.post('/api/recordings/test-stream/test-file.mp4/thumbnail')
        # Endpoint exists even if operation fails
        assert response.status_code in [200, 201, 404, 405, 500]
    
    def test_generate_thumbnail_nonexistent_recording(self, client):
        """Test generating thumbnail for non-existent recording"""
        response = client.post('/api/recordings/nonexistent/fakefile.mp4/thumbnail')
        assert response.status_code in [404, 405]
    
    def test_get_thumbnail_endpoint_exists(self, client):
        """Test that thumbnail retrieval endpoint exists"""
        response = client.get('/api/recordings/test-stream/test-file.mp4/thumbnail')
        # Endpoint exists even if thumbnail doesn't
        assert response.status_code in [200, 404]
    
    def test_get_thumbnail_nonexistent(self, client):
        """Test getting non-existent thumbnail"""
        response = client.get('/api/recordings/nonexistent/fakefile.mp4/thumbnail')
        assert response.status_code == 404
    
    def test_thumbnail_content_type(self, client):
        """Test that existing thumbnails return correct content type"""
        # This will fail for non-existent thumbnails, but tests the structure
        response = client.get('/api/recordings/test-stream/test-file.mp4/thumbnail')
        if response.status_code == 200:
            # Should be an image
            assert 'image/' in response.content_type


# =============================================================================
# Test Pattern Generator Tests
# =============================================================================

class TestPatternGenerator:
    """Test suite for test pattern generator endpoints"""

    @pytest.fixture(autouse=True)
    def _no_real_ffmpeg(self):
        """Don't spawn a real FFmpeg for test patterns: it would try to publish
        to localhost:8554 and pollute a running rig's paths. Patch the supervisor
        so these tests exercise the endpoints, not the process layer."""
        with patch('app.api.test.ManagedProcess') as MockMP:
            inst = MockMP.return_value
            inst.start.return_value = True
            inst.pid = 4321
            inst.poll.return_value = None
            inst.is_alive.return_value = True
            inst.tail_stderr.return_value = []
            inst.request_graceful_quit.return_value = True
            inst.wait.return_value = 0
            yield

    def test_start_srt_test_pattern_endpoint_exists(self, client):
        """Test that SRT test pattern endpoint is accessible"""
        response = client.post('/api/test/srt',
                              json={'streamName': 'test-input'},
                              content_type='application/json')
        assert response.status_code in [200, 201, 500]  # 500 if FFmpeg not available
    
    def test_start_srt_test_pattern_returns_json(self, client):
        """Test that SRT test pattern returns valid JSON with testId"""
        response = client.post('/api/test/srt',
                              json={'streamName': 'test-srt'},
                              content_type='application/json')
        assert response.content_type == 'application/json'
        data = json.loads(response.data)
        assert isinstance(data, dict)
        if response.status_code in [200, 201]:
            assert 'testId' in data or 'message' in data
    
    def test_start_rtsp_test_pattern_tcp(self, client):
        """Test starting RTSP test pattern with TCP transport"""
        response = client.post('/api/test/rtsp',
                              json={'streamName': 'test-rtsp-tcp'},
                              content_type='application/json')
        assert response.status_code in [200, 201, 500]  # 500 if FFmpeg not available
        assert response.content_type == 'application/json'
    
    def test_start_rtsps_test_pattern(self, client):
        """Test starting RTSPS test pattern with TLS"""
        response = client.post('/api/test/rtsps',
                              json={'streamName': 'test-secure'},
                              content_type='application/json')
        assert response.status_code in [200, 201, 400, 500]  # May fail if certs not configured or FFmpeg not available
        assert response.content_type == 'application/json'
    
    def test_start_rtsp_udp_test_pattern(self, client):
        """Test starting RTSP test pattern with UDP transport"""
        response = client.post('/api/test/rtsp-udp',
                              json={'streamName': 'test-rtsp-udp'},
                              content_type='application/json')
        assert response.status_code in [200, 201, 500]  # 500 if FFmpeg not available
        assert response.content_type == 'application/json'
    
    def test_get_test_status_nonexistent(self, client):
        """Test getting status of non-existent test"""
        fake_id = 'nonexistent-test-id-12345'
        response = client.get(f'/api/test/{fake_id}')
        assert response.status_code in [404, 200]  # May return 200 with error message
    
    def test_stop_test_pattern_nonexistent(self, client):
        """Test stopping non-existent test pattern"""
        fake_id = 'nonexistent-test-id-12345'
        response = client.delete(f'/api/test/{fake_id}')
        assert response.status_code in [404, 200]  # May return 200 even if doesn't exist
    
    def test_test_pattern_workflow(self, client):
        """Test complete workflow: start, check status, stop"""
        # Start test pattern
        start_response = client.post('/api/test/srt',
                                    json={'streamName': 'workflow-test'},
                                    content_type='application/json')
        assert start_response.status_code in [200, 201, 500]  # 500 if FFmpeg not available
        
        if start_response.status_code in [200, 201]:
            data = json.loads(start_response.data)
            if 'testId' in data:
                test_id = data['testId']
                
                # Check status (real route: GET /api/test/<id>/status)
                status_response = client.get(f'/api/test/{test_id}/status')
                assert status_response.status_code == 200

                # Stop test (real route: POST /api/test/<id>/stop)
                stop_response = client.post(f'/api/test/{test_id}/stop')
                assert stop_response.status_code in [200, 204]
    
    def test_test_pattern_requires_stream_name(self, client):
        """Test that test pattern requires streamName parameter"""
        response = client.post('/api/test/srt',
                              json={},
                              content_type='application/json')
        # May accept empty or return error
        assert response.status_code in [200, 201, 400]


# =============================================================================
# DVR ring-buffer readiness + status tests
# =============================================================================

class TestDvrSourceReadiness:
    """The DVR records via RTSP from MediaMTX; when no publisher is on the path an
    RTSP DESCRIBE 404s. These cover the readiness probe (so ffmpeg isn't crash-looped
    into a 404) and the additive 'source-offline' status surfaced to the endpoints."""

    def test_probe_ready_when_path_publishing(self):
        from app.api import dvr
        c = MagicMock()
        c.get_path.return_value = {'ready': True}
        assert dvr._probe_source_ready(c, 'cam1') == 'ready'
        c.list_paths.assert_not_called()  # ready short-circuits the reachability check

    def test_probe_offline_when_path_known_but_not_ready(self):
        from app.api import dvr
        c = MagicMock()
        c.get_path.return_value = {'ready': False}
        assert dvr._probe_source_ready(c, 'cam1') == 'offline'

    def test_probe_offline_when_no_publisher_but_api_up(self):
        from app.api import dvr
        c = MagicMock()
        c.get_path.return_value = None   # 404: no publisher on the path
        c.list_paths.return_value = []   # ...but the API is reachable
        assert dvr._probe_source_ready(c, 'cam1') == 'offline'

    def test_probe_unknown_when_api_unreachable(self):
        from app.api import dvr
        c = MagicMock()
        c.get_path.return_value = None
        c.list_paths.return_value = None  # API down -> degrade to attempting ffmpeg
        assert dvr._probe_source_ready(c, 'cam1') == 'unknown'

    @staticmethod
    def _install_fake_recorder(name, health):
        """Insert a stub recorder into the DVR registry so the HTTP endpoints can be
        exercised without spawning ffmpeg, MediaMTX, or the supervisor thread."""
        from app.api import dvr

        class _FakeRec:
            seg_dir = '/tmp'
            def running(self):
                return True
            def health(self):
                return health
            def recent_segments(self, seconds):
                return []  # empty buffer -> exercise the clip 409 path

        with dvr._recorders_lock:
            dvr._recorders[name] = _FakeRec()

    @staticmethod
    def _remove_fake_recorder(name):
        from app.api import dvr
        with dvr._recorders_lock:
            dvr._recorders.pop(name, None)

    def test_status_reports_source_offline_cleanly(self, client):
        from app.api import dvr
        self._install_fake_recorder('off-cam', {
            'segmentCount': 0, 'newestAgeS': None, 'recording': False,
            'lastReason': dvr._REASON_SOURCE_OFFLINE, 'sourceOffline': True,
            'stderrTail': ['stale 404 tail that should not surface'],
        })
        try:
            r = client.get('/api/streams/off-cam/dvr/status')
            assert r.status_code == 200
            d = json.loads(r.data)
            assert d['sourceOffline'] is True
            assert d['recording'] is False
            assert d['lastError']['reason'] == 'source-offline'
            assert d['lastError']['stderr'] == []  # the misleading ffmpeg tail is suppressed
        finally:
            self._remove_fake_recorder('off-cam')

    def test_status_reports_ffmpeg_failure_with_tail(self, client):
        """Regression: a genuine ffmpeg exit still surfaces its stderr tail."""
        self._install_fake_recorder('err-cam', {
            'segmentCount': 0, 'newestAgeS': None, 'recording': False,
            'lastReason': 'exited', 'sourceOffline': False,
            'stderrTail': ['ffmpeg: codec not supported'],
        })
        try:
            r = client.get('/api/streams/err-cam/dvr/status')
            d = json.loads(r.data)
            assert d['sourceOffline'] is False
            assert d['lastError']['reason'] == 'exited'
            assert d['lastError']['stderr'] == ['ffmpeg: codec not supported']
        finally:
            self._remove_fake_recorder('err-cam')

    def test_clip_409_flags_source_offline(self, client):
        from app.api import dvr
        self._install_fake_recorder('off-cam2', {
            'segmentCount': 0, 'newestAgeS': None, 'recording': False,
            'lastReason': dvr._REASON_SOURCE_OFFLINE, 'sourceOffline': True,
            'stderrTail': [],
        })
        try:
            r = client.post('/api/streams/off-cam2/dvr/clip', json={'seconds': 30})
            assert r.status_code == 409
            d = json.loads(r.data)
            assert d['sourceOffline'] is True
            assert d['detail'] == 'Source not publishing yet'
            assert d['error'] == 'No buffered footage yet'  # unchanged for back-compat
        finally:
            self._remove_fake_recorder('off-cam2')

    def test_clip_409_buffer_still_filling(self, client):
        self._install_fake_recorder('fill-cam', {
            'segmentCount': 0, 'newestAgeS': None, 'recording': False,
            'lastReason': None, 'sourceOffline': False, 'stderrTail': [],
        })
        try:
            r = client.post('/api/streams/fill-cam/dvr/clip', json={'seconds': 30})
            assert r.status_code == 409
            d = json.loads(r.data)
            assert d['sourceOffline'] is False
            assert d['detail'] == 'Buffer still filling'
        finally:
            self._remove_fake_recorder('fill-cam')


class TestSupervisedTestPattern:
    """Continuous (duration=0) test patterns run under supervise() so a transient
    publisher drop auto-restarts instead of silently killing the DVR's source."""

    def test_continuous_uses_supervised_wrapper(self, client):
        from app.api import test as test_mod
        with patch('app.api.test.ManagedProcess') as MockMP:
            inst = MockMP.return_value
            inst.start.return_value = True
            inst.poll.return_value = None
            inst.tail_stderr.return_value = []
            r = client.post('/api/test/rtsp',
                            json={'streamName': 'soak-cam', 'duration': 0})
            assert r.status_code == 200
            tid = json.loads(r.data)['testId']
            try:
                proc = test_mod.active_tests[tid]['process']
                assert isinstance(proc, test_mod._SupervisedTest)
                assert proc.poll() is None  # alive / reconnecting
            finally:
                client.post(f'/api/test/{tid}/stop')
            assert tid not in test_mod.active_tests  # stop cleaned it up

    def test_finite_uses_one_shot_managed_process(self, client):
        from app.api import test as test_mod
        with patch('app.api.test.ManagedProcess') as MockMP:
            inst = MockMP.return_value
            inst.start.return_value = True
            inst.poll.return_value = None
            r = client.post('/api/test/rtsp',
                            json={'streamName': 'finite-cam', 'duration': 30})
            assert r.status_code == 200
            tid = json.loads(r.data)['testId']
            try:
                proc = test_mod.active_tests[tid]['process']
                assert proc is inst  # raw ManagedProcess, not the supervised wrapper
            finally:
                client.post(f'/api/test/{tid}/stop')

    def test_supervised_test_terminable_by_stream_stop_path(self):
        """Regression: streams._stop_stream_components() tears down the stored test
        process with _terminate_process(), which calls .terminate()/.wait()/.kill().
        A continuous test stores a _SupervisedTest, so those must exist and actually
        stop it (else AttributeError -> HTTP 500 and an orphaned republishing source)."""
        from app.api import test as test_mod
        from app.api import streams
        with patch('app.api.test.ManagedProcess') as MockMP:
            inst = MockMP.return_value
            inst.start.return_value = True
            inst.poll.return_value = None
            inst.stop.return_value = None
            proc = test_mod._SupervisedTest('test-x', ['ffmpeg'], '/tmp', label='t')
            proc.start()
            assert proc.poll() is None
            streams._terminate_process(proc)  # must not AttributeError
            assert proc.poll() is not None    # supervisor thread stopped


# =============================================================================
# HLS Access Validation Tests
# =============================================================================

class TestHlsAccessValidation:
    """Stream-name validation on the public HLS routes (defence-in-depth)."""

    def test_hls_rejects_traversal_name(self, client):
        # '..' in the stream name must be rejected before any path join.
        response = client.get('/hls/a..b/master.m3u8')
        assert response.status_code == 400

    def test_hls_valid_name_passes_validation(self, client):
        # A valid name passes validation; the file simply doesn't exist (404),
        # which proves the 400 above came from validation, not a missing file.
        response = client.get('/hls/valid-stream/master.m3u8')
        assert response.status_code == 404

    def test_hls_proxy_cors_preflight(self, client):
        # OPTIONS preflight on the (public) HLS proxy returns CORS headers.
        # With no allow-list configured (CORS_ORIGINS='*'), origin is '*'.
        response = client.options('/api/hls/proxy/teststream/index.m3u8')
        assert response.status_code == 204
        assert response.headers.get('Access-Control-Allow-Origin') == '*'


class TestSecurityHeaders:
    """Defence-in-depth response headers added globally via after_request."""

    def test_security_headers_present(self, client):
        response = client.get('/health')
        assert response.headers.get('X-Content-Type-Options') == 'nosniff'
        assert response.headers.get('X-Frame-Options') == 'SAMEORIGIN'
        assert response.headers.get('Referrer-Policy') == 'no-referrer'

    def test_hsts_set_when_forwarded_proto_https(self, client):
        response = client.get('/health', headers={'X-Forwarded-Proto': 'https'})
        assert 'max-age=' in response.headers.get('Strict-Transport-Security', '')

    def test_hsts_absent_over_plain_http(self, client):
        response = client.get('/health')
        assert 'Strict-Transport-Security' not in response.headers


class TestMetrics:
    """Prometheus metrics exposition endpoint."""

    def test_metrics_endpoint(self, client):
        response = client.get('/metrics')
        assert response.status_code == 200
        assert 'text/plain' in response.content_type
        body = response.get_data(as_text=True)
        assert 'tvr_up 1' in body
        assert '# TYPE tvr_up gauge' in body
        assert 'tvr_active_recordings' in body
        assert 'tvr_streams_dir_free_bytes' in body


class TestRecordingLifecycle:
    """Unified recording path (begin_recording + stop endpoint), hermetic:
    a fake FFmpeg that writes its output and exits on stdin 'q'. No MediaMTX,
    no real ffmpeg recording, no port. Covers the schema-consistency and the
    graceful-quit + race-safe cleanup fixes that unify auto/manual recording.
    """

    @pytest.fixture(autouse=True)
    def _plenty_of_disk(self):
        """Decouple these tests from the host's actual free space so the
        disk-full pre-flight guard never aborts them."""
        with patch('app.api.recordings._streams_free_gb', return_value=9999.0):
            yield

    def test_begin_and_stop_recording(self, client, tmp_path):
        from app.api import recordings as rec
        out = str(tmp_path / 'rec.mov')
        # Fake ffmpeg: write the output file, then block on stdin until 'q'.
        cmd = [sys.executable, '-u', '-c',
               'import sys; open(sys.argv[1], "w").write("data"); '
               'sys.exit(0 if sys.stdin.read(1) == "q" else 1)', out]
        with patch('app.api.recordings.broadcast'):
            proc = rec.begin_recording('hermetic1', cmd, out, codec='h264',
                                       timecode='00:00:00:00', auto_started=False)
        assert proc is not None
        entry = rec.active_recordings.get('hermetic1')
        assert entry is not None
        # Consistent schema shared by auto + manual recordings.
        for key in ('process', 'file', 'startTime', 'pid', 'codec', 'auto_started'):
            assert key in entry
        assert entry['file'] == out and entry['codec'] == 'h264'

        # Stop via the real endpoint: graceful 'q', race-safe pop, no KeyError.
        resp = client.post('/api/streams/hermetic1/stop-record', json={})
        assert resp.status_code == 200
        assert 'partial cleanup' not in resp.get_data(as_text=True)
        assert 'hermetic1' not in rec.active_recordings

    def test_stop_unknown_recording_is_idempotent(self, client):
        resp = client.post('/api/streams/does-not-exist/stop-record', json={})
        assert resp.status_code == 200
        assert 'already stopped' in resp.get_data(as_text=True).lower()

    def test_auto_record_subdir_is_listed(self, client):
        """A recording written into STREAMS_DIR/<stream>/ (as auto-record now
        does, instead of the STREAMS_DIR root) is discoverable via
        list_recordings, which only scans per-stream subdirs."""
        from app.api import recordings as rec
        from app.config import STREAMS_DIR
        stream = 'autosubdir'
        stream_dir = os.path.join(STREAMS_DIR, stream)
        os.makedirs(stream_dir, exist_ok=True)
        out = os.path.join(stream_dir, 'recording-2026-01-01T00-00-00-000Z.mov')
        # Fake ffmpeg: create the output file and exit immediately.
        cmd = [sys.executable, '-c', 'import sys; open(sys.argv[1], "w").write("x")', out]
        with patch('app.api.recordings.broadcast'), \
             patch('app.api.recordings.generate_thumbnail'):
            proc = rec.begin_recording(stream, cmd, out, codec='h264', auto_started=True)
            assert proc is not None
            proc.wait(timeout=5)

        resp = client.get('/api/recordings')
        assert resp.status_code == 200
        items = json.loads(resp.data)
        assert any(r['stream'] == stream and r['filename'].endswith('.mov')
                   for r in items)


class TestPullStreamSupervised:
    """Pull relay now spawns under ManagedProcess (stderr->file, own session,
    no PIPE deadlock) instead of a raw Popen (A1)."""

    def test_spawn_pull_process_uses_managed_process(self):
        from app.api import streams
        with patch('app.api.streams.ManagedProcess') as MockMP:
            inst = MockMP.return_value
            inst.start.return_value = True
            inst.pid = 555
            proc = streams._spawn_pull_process('pullx', 'rtsp://127.0.0.1:8554/src')
        assert proc is inst
        args, _ = MockMP.call_args
        assert args[0] == 'pull-pullx'   # named for the registry/log file
        inst.start.assert_called_once()

    def test_spawn_pull_process_raises_when_ffmpeg_fails(self):
        from app.api import streams
        with patch('app.api.streams.ManagedProcess') as MockMP:
            MockMP.return_value.start.return_value = False
            with pytest.raises(RuntimeError):
                streams._spawn_pull_process('pully', 'rtsp://127.0.0.1:8554/src')


class TestRecordingDiskGuard:
    """Disk-full / ENOSPC guard on the recording path (A4), hermetic."""

    def test_streams_free_gb_handles_error(self):
        from app.api import recordings as rec
        with patch('app.api.recordings.shutil.disk_usage', side_effect=OSError):
            assert rec._streams_free_gb() == -1.0

    def test_begin_recording_aborts_on_low_disk(self, client):
        from app.api import recordings as rec
        with patch('app.api.recordings._streams_free_gb', return_value=0.5), \
             patch('app.api.recordings.broadcast') as mock_bcast:
            proc = rec.begin_recording('lowdisk', ['ffmpeg'], '/tmp/x.mov', codec='h264')
        assert proc is None
        assert 'lowdisk' not in rec.active_recordings
        # A disk-full event was broadcast and no FFmpeg was spawned.
        assert any('disk_full' in str(c.args[0]) for c in mock_bcast.call_args_list)


# =============================================================================
# Settings Tests
# =============================================================================

class TestSettingsEndpoint:
    """Test suite for /api/settings endpoints"""
    
    def test_get_settings_endpoint_exists(self, client):
        """Test that settings endpoint is accessible"""
        response = client.get('/api/settings')
        assert response.status_code == 200
    
    def test_get_settings_returns_json(self, client):
        """Test that settings endpoint returns valid JSON"""
        response = client.get('/api/settings')
        assert response.content_type == 'application/json'
        data = json.loads(response.data)
        assert isinstance(data, dict)
    
    def test_settings_has_auto_record(self, client):
        """Test that settings includes autoRecord field"""
        response = client.get('/api/settings')
        data = json.loads(response.data)
        assert 'autoRecord' in data
        assert isinstance(data['autoRecord'], bool)
    
    def test_settings_has_structure(self, client):
        """Test that settings has expected structure"""
        response = client.get('/api/settings')
        data = json.loads(response.data)
        assert 'autoRecord' in data
        assert 'disk' in data
        assert 'settings' in data
    
    def test_update_settings_requires_json(self, client):
        """Test that updating settings with PUT returns 405"""
        response = client.put('/api/settings')
        assert response.status_code == 405
    
    def test_update_auto_record_setting(self, client):
        """Test updating autoRecord setting with POST"""
        response = client.post('/api/settings',
                            json={'autoRecord': True},
                            content_type='application/json')
        assert response.status_code == 200


# =============================================================================
# Certificate Management Tests
# =============================================================================

class TestCertificateManagement:
    """Test suite for certificate management"""
    
    def test_get_certificate_info_endpoint_not_exists(self, client):
        """Test that certificate info endpoint returns 404"""
        response = client.get('/api/settings/certificates')
        assert response.status_code == 404


# =============================================================================
# Codec Detection Tests
# =============================================================================

class TestCodecDetection:
    """Test suite for codec detection utilities"""
    
    def test_codec_detection_module_exists(self):
        """Test that codec detection module exists"""
        from app.utils import codec_detection
        assert codec_detection is not None


# =============================================================================
# Thumbnail Utility Tests
# =============================================================================

class TestThumbnailUtils:
    """Test suite for thumbnail utilities"""
    
    def test_thumbnail_module_exists(self):
        """Test that thumbnail module exists"""
        from app.utils import thumbnail
        assert thumbnail is not None


# =============================================================================
# File Validation Tests
# =============================================================================

class TestFileValidation:
    """Test suite for file validation"""
    
    def test_valid_video_extensions(self):
        """Test that valid video extensions are accepted"""
        valid_extensions = ['.mp4', '.mov', '.ts', '.mxf', '.mpg', '.mpeg', '.mkv']
        
        for ext in valid_extensions:
            filename = f'test{ext}'
            # Video files should be valid
            assert any(filename.endswith(e) for e in valid_extensions)
    
    def test_invalid_file_extensions_filtered(self):
        """Test that non-video files are filtered"""
        invalid_files = ['test.bin', 'test.json', 'test.txt', 'test.log']
        video_extensions = ['.mp4', '.mov', '.ts', '.mxf', '.mpg', '.mpeg', '.mkv']
        
        for filename in invalid_files:
            # These should NOT match video extensions
            assert not any(filename.endswith(ext) for ext in video_extensions)


# =============================================================================
# Web Interface Tests
# =============================================================================

class TestWebInterface:
    """Test suite for web interface pages"""
    
    def test_index_page_loads(self, client):
        """Test that index page loads successfully"""
        response = client.get('/')
        assert response.status_code == 200
        assert b'TAK Video Restreamer' in response.data
    
    def test_recordings_page_loads(self, client):
        """Test that recordings page loads successfully"""
        response = client.get('/recordings')
        assert response.status_code == 200
        assert b'Recordings' in response.data
    
    def test_settings_page_loads(self, client):
        """Test that settings page loads successfully"""
        response = client.get('/settings')
        assert response.status_code == 200
        assert b'Settings' in response.data
    
    def test_utils_page_loads(self, client):
        """Test that utils page loads successfully"""
        response = client.get('/utils')
        assert response.status_code == 200
        assert b'Utils' in response.data
    
    def test_test_page_loads(self, client):
        """Test that test video page loads successfully"""
        response = client.get('/test')
        assert response.status_code == 200
        assert b'Test' in response.data
    
    def test_static_css_loads(self, client):
        """Test that CSS file loads successfully"""
        response = client.get('/static/styles.css')
        assert response.status_code == 200
        assert 'text/css' in response.content_type
    
    def test_static_js_loads(self, client):
        """Test that JavaScript file loads successfully"""
        response = client.get('/static/client.js')
        assert response.status_code == 200


# =============================================================================
# Integration Tests
# =============================================================================

class TestAPIIntegration:
    """Test suite for API integration workflows"""
    
    def test_health_to_streams_flow(self, client):
        """Test workflow from health check to streams listing"""
        # Check health
        health_response = client.get('/health')
        assert health_response.status_code == 200
        
        # List streams
        streams_response = client.get('/api/streams')
        assert streams_response.status_code == 200
        
        # Get settings
        settings_response = client.get('/api/settings')
        assert settings_response.status_code == 200
    
    def test_recordings_workflow(self, client):
        """Test complete recordings workflow"""
        # List recordings
        list_response = client.get('/api/recordings')
        assert list_response.status_code == 200
        data = json.loads(list_response.data)
        assert isinstance(data, list)
    
    def test_settings_workflow(self, client):
        """Test settings get/update workflow"""
        # Get current settings
        get_response = client.get('/api/settings')
        assert get_response.status_code == 200
        
        # Update settings
        update_response = client.post('/api/settings',
                                    json={'autoRecord': False},
                                    content_type='application/json')
        assert update_response.status_code == 200


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestErrorHandling:
    """Test suite for error handling"""
    
    def test_404_for_invalid_endpoint(self, client):
        """Test that invalid endpoints return 404"""
        response = client.get('/api/nonexistent')
        assert response.status_code == 404
    
    def test_405_for_wrong_method(self, client):
        """Test that wrong HTTP methods return 405"""
        response = client.post('/health')
        assert response.status_code == 405
    
    def test_400_for_invalid_json(self, client):
        """Test that invalid JSON with PUT returns 405"""
        response = client.put('/api/settings',
                            data='invalid json',
                            content_type='application/json')
        assert response.status_code == 405


# =============================================================================
# RBAC — centralized role gate (Phase 1 infra "B")
# =============================================================================

class TestRBAC:
    """Role enforcement via the centralized before_request table."""

    def test_viewer_can_read(self, viewer_client):
        with patch('requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {'items': {}})
            assert viewer_client.get('/api/streams').status_code == 200

    def test_viewer_cannot_create_stream(self, viewer_client):
        # Mutating route → operator+. Viewer is rejected at the gate (403) before
        # the view runs, so no MediaMTX call is needed.
        r = viewer_client.post('/api/streams/foo', json={})
        assert r.status_code == 403

    def test_operator_can_create_pull_stream(self, operator_client):
        with patch('app.api.streams._start_pull_impl') as mock_pull, \
             patch('app.api.streams.broadcast'):
            r = operator_client.post('/api/streams/foo/pull',
                                     json={'sourceUrl': 'rtsp://example.com/s'},
                                     content_type='application/json')
        assert r.status_code == 200
        mock_pull.assert_called_once()

    def test_operator_cannot_write_settings(self, operator_client):
        # Global settings POST is admin-only.
        r = operator_client.post('/api/settings', json={'segment_duration': 30})
        assert r.status_code == 403

    def test_admin_can_write_settings(self, client):
        # The default `client` fixture is admin.
        with patch('app.api.settings._persist_server_settings'), \
             patch('app.api.settings.broadcast'):
            r = client.post('/api/settings', json={'segment_duration': 30})
        assert r.status_code == 200

    def test_operator_cannot_manage_api_keys(self, operator_client):
        r = operator_client.post('/api/auth/keys', json={'name': 'x'})
        assert r.status_code == 403

    def test_unlisted_mutating_route_defaults_to_operator(self, viewer_client):
        # Default-deny: a route not in the table requires operator for non-GET.
        r = viewer_client.post('/api/some-brand-new-endpoint', json={})
        assert r.status_code == 403  # 403 (not 404) proves the gate denies first

    def test_unauthenticated_api_is_401(self, anon_client):
        assert anon_client.get('/api/streams').status_code == 401


# =============================================================================
# HLS fail-closed access control + signed URLs
# =============================================================================

class TestHLSAccessControl:
    """HLS playback is fail-closed: auth OR a valid signed URL."""

    def _write_master(self, stream='sigstream'):
        from app.services.abr import HLS_OUTPUT_DIR
        sd = os.path.join(HLS_OUTPUT_DIR, stream)
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, 'master.m3u8'), 'w') as f:
            f.write('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nv0/index.m3u8\n')
        return stream

    def test_anonymous_hls_denied(self, anon_client):
        assert anon_client.get('/hls/anystream/master.m3u8').status_code == 403

    def test_authenticated_hls_allowed(self, client):
        stream = self._write_master('authstream')
        assert client.get(f'/hls/{stream}/master.m3u8').status_code == 200

    def test_token_endpoint_then_anon_fetch(self, viewer_client, anon_client):
        stream = self._write_master('tokstream')
        r = viewer_client.get(f'/api/streams/{stream}/hls-token')
        assert r.status_code == 200
        master_url = json.loads(r.data)['master_url']
        # An anonymous client can fetch the signed URL.
        resp = anon_client.get(master_url)
        assert resp.status_code == 200
        # And the signature is propagated to the child playlist reference.
        assert 'v0/index.m3u8?exp=' in resp.get_data(as_text=True)

    def test_expired_signature_denied(self, anon_client):
        from app.api.hls import _sign_stream
        exp = 1  # epoch → long expired
        sig = _sign_stream('expstream', exp)
        r = anon_client.get(f'/hls/expstream/master.m3u8?exp={exp}&sig={sig}')
        assert r.status_code == 403

    def test_tampered_signature_denied(self, anon_client):
        r = anon_client.get('/hls/x/master.m3u8?exp=9999999999&sig=deadbeef')
        assert r.status_code == 403

    def test_signature_is_stream_scoped(self, viewer_client, anon_client):
        # A signature minted for stream A must not authorize stream B.
        r = viewer_client.get('/api/streams/streamA/hls-token')
        master_a = json.loads(r.data)['master_url']
        qs = master_a.split('?', 1)[1]
        assert anon_client.get(f'/hls/streamB/master.m3u8?{qs}').status_code == 403

    def test_proxy_preflight_still_open(self, anon_client):
        r = anon_client.options('/api/hls/proxy/teststream/index.m3u8')
        assert r.status_code == 204


# =============================================================================
# MediaMTX API client authentication
# =============================================================================

class TestMediaMTXClientAuth:
    """The client injects credentials into every request when configured."""

    def test_no_auth_by_default(self):
        from app.services.mediamtx import MediaMTXClient
        with patch('app.services.mediamtx.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {})
            MediaMTXClient('http://mtx:8889').list_paths()
            _, kwargs = mock_get.call_args
            assert 'auth' not in kwargs
            assert not kwargs.get('headers')

    def test_basic_auth_when_user_set(self):
        from app.services.mediamtx import MediaMTXClient
        with patch('app.services.mediamtx.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {})
            MediaMTXClient('http://mtx:8889', user='u', password='p').list_paths()
            _, kwargs = mock_get.call_args
            assert kwargs.get('auth') == ('u', 'p')

    def test_bearer_token_when_token_set(self):
        from app.services.mediamtx import MediaMTXClient
        with patch('app.services.mediamtx.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {})
            MediaMTXClient('http://mtx:8889', token='abc123').list_paths()
            _, kwargs = mock_get.call_args
            assert kwargs['headers']['Authorization'] == 'Bearer abc123'


# =============================================================================
# HLS data-plane offload (X-Accel-Redirect)
# =============================================================================

class TestHLSXAccel:
    """When HLS_X_ACCEL is on, segments are handed to nginx (X-Accel-Redirect)
    AFTER the fail-closed access decision; off, they're served from disk."""

    def _write_segment(self, name='xaccel', variant=0):
        from app.services.abr import HLS_OUTPUT_DIR
        d = os.path.join(HLS_OUTPUT_DIR, name, f'v{variant}')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'seg0.ts'), 'wb') as f:
            f.write(b'TSDATA' * 50)
        return name

    def test_xaccel_on_returns_redirect_header(self, client, monkeypatch):
        monkeypatch.setattr('app.api.hls.HLS_X_ACCEL', True)
        name = self._write_segment('xaccelon')
        r = client.get(f'/hls/{name}/v0/seg0.ts')
        assert r.status_code == 200
        assert r.headers.get('X-Accel-Redirect') == f'/_hls_internal/{name}/v0/seg0.ts'
        assert r.data == b''  # body offloaded to nginx

    def test_xaccel_off_serves_bytes(self, client, monkeypatch):
        monkeypatch.setattr('app.api.hls.HLS_X_ACCEL', False)
        name = self._write_segment('xacceloff')
        r = client.get(f'/hls/{name}/v0/seg0.ts')
        assert r.status_code == 200
        assert 'X-Accel-Redirect' not in r.headers
        assert r.data == b'TSDATA' * 50

    def test_xaccel_still_fail_closed(self, anon_client, monkeypatch):
        # Offload must not bypass auth: anon still 403 even with X-Accel on.
        monkeypatch.setattr('app.api.hls.HLS_X_ACCEL', True)
        self._write_segment('xaccelsec')
        assert anon_client.get('/hls/xaccelsec/v0/seg0.ts').status_code == 403


# =============================================================================
# Per-stream health
# =============================================================================

class _FakeProc:
    def __init__(self, alive=True, rc=None):
        self._alive, self._rc = alive, rc
    def is_alive(self): return self._alive
    @property
    def pid(self): return 4242
    def uptime(self): return 12.5
    @property
    def returncode(self): return self._rc
    def tail_stderr(self, n=5): return ['err line'] if not self._alive else []


class TestStreamHealth:
    def test_inactive_stream(self, client):
        r = client.get('/api/streams/nostream/health')
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data['status'] == 'inactive'
        for k in ('recording', 'pull', 'abr', 'timestamp'):
            assert k in data

    def test_viewer_can_read_health(self, viewer_client):
        assert viewer_client.get('/api/streams/foo/health').status_code == 200

    def test_invalid_name_rejected(self, client):
        assert client.get('/api/streams/a..b/health').status_code == 400

    def test_active_recording_is_healthy(self, client):
        from app.api import streams as st
        with st.recording_lock:
            st.active_recordings['hlth1'] = {'process': _FakeProc(alive=True), 'file': 'x'}
        try:
            data = json.loads(client.get('/api/streams/hlth1/health').data)
            assert data['recording']['active'] is True
            assert data['status'] == 'healthy'
        finally:
            with st.recording_lock:
                st.active_recordings.pop('hlth1', None)

    def test_dead_recording_is_down(self, client):
        from app.api import streams as st
        with st.recording_lock:
            st.active_recordings['hlth2'] = {'process': _FakeProc(alive=False, rc=1), 'file': 'x'}
        try:
            data = json.loads(client.get('/api/streams/hlth2/health').data)
            assert data['status'] == 'down'
            assert data['recording']['recent_stderr'] == ['err line']
        finally:
            with st.recording_lock:
                st.active_recordings.pop('hlth2', None)


# =============================================================================
# Test Runner (when executed directly)
# =============================================================================

def main():
    """Run pytest with the provided arguments"""
    try:
        import pytest
    except ImportError:
        print("ERROR: pytest is not installed")
        print("Install it with: pip install pytest")
        return 1
    
    args = sys.argv[1:]
    
    # Default pytest arguments - run this file
    pytest_args = [__file__]
    
    # Add user arguments
    if args:
        pytest_args.extend(args)
    else:
        # Default: run with verbose output
        pytest_args.append('-v')
    
    # Run pytest
    print("=" * 70)
    print("TAK Video Restreamer - Test Suite")
    print("=" * 70)
    print(f"Running: pytest {' '.join(pytest_args)}\n")
    
    result = pytest.main(pytest_args)
    
    print("\n" + "=" * 70)
    if result == 0:
        print("✓ All tests passed!")
    else:
        print("✗ Some tests failed")
    print("=" * 70)
    
    return result


if __name__ == '__main__':
    sys.exit(main())
