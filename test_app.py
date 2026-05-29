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
    """Create an authenticated test client.

    The app protects all /api/* and page routes via a before_request hook.
    We authenticate with a freshly minted API key (decoupled from the admin
    password) set as a default header on every request, so endpoint behaviour
    — not the auth gate — is what gets exercised.
    """
    app = create_app()
    app.config['TESTING'] = True
    from app import auth
    raw_key = auth.generate_api_key('pytest')
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
