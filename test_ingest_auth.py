"""Tests for the MediaMTX→SPASA ingest authorization proxy (``POST /auth/mediamtx``).

Hermetic: the SPASA call is monkeypatched, so nothing here needs a server, MediaMTX or
the network. What they pin down is the behaviour that would otherwise fail silently in
production — a 2xx where SPASA said no would let anyone publish.
"""
import pytest

from app.api import ingest_auth


HOOK_BODY = {
    'action': 'publish',
    'id': '011e16bb-9de4-4735-b42b-76c68e417d73',
    'ip': '10.20.30.40',
    'password': 'hunter2',
    'path': 'feed-abc123',
    'protocol': 'rtsp',
    'query': '',
    'token': '',
    'user': 'dron-1',
}


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def client():
    """Unauthenticated client: `/auth/mediamtx` sits outside the /api auth gate on
    purpose — MediaMTX has no identity in this app, and the route is protected by
    loopback + the shared secret with SPASA instead."""
    from app import create_app
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _configure(monkeypatch, **overrides):
    """Points the proxy at a fake SPASA. Both modules matter: the blueprint imported
    the values by name at import time, so patching only `app.config` would be a no-op."""
    defaults = {
        'SPASA_INGEST_AUTH_URL': 'https://spasa.test/video/ingest-auth',
        'SPASA_VIDEO_INGEST_TOKEN': 'shared-secret',
        'SPASA_INGEST_FAIL_OPEN': False,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(ingest_auth, name, value, raising=False)


def test_allows_when_spasa_allows(client, monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(ingest_auth.http_requests, 'post', lambda *a, **k: _Resp(200))
    resp = client.post('/auth/mediamtx', json=HOOK_BODY)
    assert resp.status_code == 200


@pytest.mark.parametrize('spasa_status', [401, 403, 404, 500])
def test_any_non_2xx_from_spasa_denies(client, monkeypatch, spasa_status):
    """MediaMTX only distinguishes 2xx from non-2xx: turning a denial into a 2xx here
    would silently allow the publish."""
    _configure(monkeypatch)
    monkeypatch.setattr(ingest_auth.http_requests, 'post',
                        lambda *a, **k: _Resp(spasa_status))
    assert client.post('/auth/mediamtx', json=HOOK_BODY).status_code == 401


def test_unreachable_spasa_denies_by_default(client, monkeypatch):
    _configure(monkeypatch)

    def _boom(*a, **k):
        raise OSError('connection refused')

    monkeypatch.setattr(ingest_auth.http_requests, 'post', _boom)
    assert client.post('/auth/mediamtx', json=HOOK_BODY).status_code == 401


def test_unreachable_spasa_can_fail_open_when_opted_in(client, monkeypatch):
    _configure(monkeypatch, SPASA_INGEST_FAIL_OPEN=True)

    def _boom(*a, **k):
        raise OSError('connection refused')

    monkeypatch.setattr(ingest_auth.http_requests, 'post', _boom)
    resp = client.post('/auth/mediamtx', json=HOOK_BODY)
    assert resp.status_code == 200
    assert resp.get_json()['degraded'] is True


def test_denies_when_not_configured(client, monkeypatch):
    """`authMethod: http` without the SPASA URL/token is a misconfiguration, and it
    must deny rather than let everything through."""
    _configure(monkeypatch, SPASA_INGEST_AUTH_URL='', SPASA_VIDEO_INGEST_TOKEN='')
    assert client.post('/auth/mediamtx', json=HOOK_BODY).status_code == 401


def test_forwards_the_shared_secret_and_only_the_hook_fields(client, monkeypatch):
    _configure(monkeypatch)
    seen = {}

    def _capture(url, json=None, headers=None, **kwargs):
        seen['url'] = url
        seen['json'] = json
        seen['headers'] = headers
        return _Resp(200)

    monkeypatch.setattr(ingest_auth.http_requests, 'post', _capture)
    client.post('/auth/mediamtx', json={**HOOK_BODY, 'evil': '../../etc/passwd'})

    assert seen['url'] == 'https://spasa.test/video/ingest-auth'
    assert seen['headers']['X-SPASA-Ingest-Token'] == 'shared-secret'
    assert set(seen['json']) == set(ingest_auth._HOOK_FIELDS), 'campos ajenos reenviados'
    assert seen['json']['user'] == 'dron-1'


def test_missing_fields_are_forwarded_as_empty_strings(client, monkeypatch):
    """MediaMTX always sends the 9 fields; a future version that drops one must not
    make this crash into a 500 (which MediaMTX would read as a denial, but silently)."""
    _configure(monkeypatch)
    seen = {}
    monkeypatch.setattr(ingest_auth.http_requests, 'post',
                        lambda url, json=None, **k: (seen.update(json=json), _Resp(200))[1])
    resp = client.post('/auth/mediamtx', json={'action': 'publish'})
    assert resp.status_code == 200
    assert seen['json']['password'] == ''
    assert seen['json']['path'] == ''
