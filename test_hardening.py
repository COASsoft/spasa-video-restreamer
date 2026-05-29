"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Unit tests for the security/robustness hardening utilities (Phase 0+).
These are hermetic — no app context, no MediaMTX, no FFmpeg.
"""
import os
import stat
import sys
import time

import pytest

from app import config
from app.config import ConfigError, _env_bool, _env_int
from app.utils.atomic_json import write_json_atomic, read_json
from app.utils.validation import is_valid_stream_name, validate_source_url
from app.utils import crypto
from app.services.process import ManagedProcess, supervise, tail_file


class TestAtomicJson:
    def test_write_then_read_roundtrip(self, tmp_path):
        path = str(tmp_path / 'state.json')
        data = {'a': 1, 'b': ['x', 'y'], 'c': {'nested': True}}
        write_json_atomic(path, data)
        assert read_json(path) == data

    def test_overwrite_replaces_content(self, tmp_path):
        path = str(tmp_path / 'state.json')
        write_json_atomic(path, {'v': 1})
        write_json_atomic(path, {'v': 2})
        assert read_json(path) == {'v': 2}

    def test_default_mode_is_0600(self, tmp_path):
        path = str(tmp_path / 'secret.json')
        write_json_atomic(path, {'k': 'v'})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_no_tmp_files_left_behind(self, tmp_path):
        path = str(tmp_path / 'state.json')
        write_json_atomic(path, {'k': 'v'})
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith('.tmp-')]
        assert leftovers == []

    def test_read_missing_returns_default(self, tmp_path):
        path = str(tmp_path / 'does-not-exist.json')
        assert read_json(path, default={'fallback': True}) == {'fallback': True}
        assert read_json(path) is None

    def test_read_corrupt_returns_default(self, tmp_path):
        path = tmp_path / 'corrupt.json'
        path.write_text('{ this is not json ')
        assert read_json(str(path), default=[]) == []

    def test_creates_parent_directory(self, tmp_path):
        path = str(tmp_path / 'sub' / 'dir' / 'state.json')
        write_json_atomic(path, {'ok': 1})
        assert read_json(path) == {'ok': 1}

    def test_failed_write_does_not_clobber_existing(self, tmp_path):
        path = str(tmp_path / 'state.json')
        write_json_atomic(path, {'good': 1})
        # A non-serialisable value raises; the original file must survive intact.
        with pytest.raises(TypeError):
            write_json_atomic(path, {'bad': object()})
        assert read_json(path) == {'good': 1}
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith('.tmp-')]
        assert leftovers == []


class TestEnvHelpers:
    def test_env_bool_truthy_values(self, monkeypatch):
        for v in ('1', 'true', 'TRUE', 'yes', 'on'):
            monkeypatch.setenv('X_BOOL', v)
            assert _env_bool('X_BOOL') is True

    def test_env_bool_falsy_and_default(self, monkeypatch):
        monkeypatch.setenv('X_BOOL', 'no')
        assert _env_bool('X_BOOL') is False
        monkeypatch.delenv('X_BOOL', raising=False)
        assert _env_bool('X_BOOL', True) is True

    def test_env_int_valid(self, monkeypatch):
        monkeypatch.setenv('X_INT', '42')
        assert _env_int('X_INT', 0) == 42

    def test_env_int_invalid_raises(self, monkeypatch):
        monkeypatch.setenv('X_INT', 'not-a-number')
        with pytest.raises(ConfigError):
            _env_int('X_INT', 0)

    def test_env_int_range_enforced(self, monkeypatch):
        monkeypatch.setenv('X_INT', '5')
        with pytest.raises(ConfigError):
            _env_int('X_INT', 0, minimum=10)
        with pytest.raises(ConfigError):
            _env_int('X_INT', 0, maximum=1)


class TestFailClosedPassword:
    def test_default_password_aborts_startup(self, monkeypatch):
        monkeypatch.setenv('ADMIN_PASSWORD', 'changeme')
        monkeypatch.setattr(config, 'DEV_MODE', False)
        monkeypatch.setattr(config, 'ALLOW_DEFAULT_PASSWORD', False)
        with pytest.raises(ConfigError):
            config.validate_runtime_config()

    def test_strong_password_passes(self, monkeypatch):
        monkeypatch.setenv('ADMIN_PASSWORD', 'a-strong-secret-passphrase')
        monkeypatch.setattr(config, 'DEV_MODE', False)
        monkeypatch.setattr(config, 'ALLOW_DEFAULT_PASSWORD', False)
        config.validate_runtime_config()  # must not raise

    def test_explicit_override_allows_default(self, monkeypatch):
        monkeypatch.setenv('ADMIN_PASSWORD', 'changeme')
        monkeypatch.setattr(config, 'DEV_MODE', False)
        monkeypatch.setattr(config, 'ALLOW_DEFAULT_PASSWORD', True)
        config.validate_runtime_config()  # must not raise


class TestSecretKeyPersistence:
    def test_env_secret_takes_priority(self, monkeypatch):
        monkeypatch.setenv('SECRET_KEY', 'explicit-env-key')
        assert config._resolve_secret_key() == 'explicit-env-key'

    def test_placeholder_env_secret_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv('SECRET_KEY', 'dev-secret-key-change-in-production')
        monkeypatch.setattr(config, '_SECRET_KEY_FILE', str(tmp_path / 'secret_key'))
        monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
        key = config._resolve_secret_key()
        assert key != 'dev-secret-key-change-in-production'
        assert len(key) >= 32

    def test_generated_key_persisted_stable_and_0600(self, monkeypatch, tmp_path):
        monkeypatch.delenv('SECRET_KEY', raising=False)
        key_file = tmp_path / 'secret_key'
        monkeypatch.setattr(config, '_SECRET_KEY_FILE', str(key_file))
        monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
        k1 = config._resolve_secret_key()
        assert key_file.exists()
        assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
        k2 = config._resolve_secret_key()
        assert k1 == k2 and len(k1) >= 32


class TestStreamNameValidation:
    @pytest.mark.parametrize('name', [
        'tak-eagle', 'drone_01', 'uas.cam1', 'a', 'Stream123', 'a1', 'x_y-z.w',
    ])
    def test_valid_names(self, name):
        assert is_valid_stream_name(name) is True

    @pytest.mark.parametrize('name', [
        '', '..', '../etc/passwd', 'a/b', 'a..b', '.hidden', '-lead', 'trail-',
        'trail_', 'has space', 'café', 'a\\b', '/abs', 'x' * 65, None, 123,
    ])
    def test_invalid_names(self, name):
        assert is_valid_stream_name(name) is False

    def test_custom_max_len(self):
        assert is_valid_stream_name('a' * 10, max_len=10) is True
        assert is_valid_stream_name('a' * 11, max_len=10) is False


class TestCrypto:
    def test_sha256_known_vector(self):
        # NIST/RFC test vector for "abc"
        assert crypto.sha256_hex('abc') == (
            'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
        )

    def test_sha256_accepts_bytes_and_str_equivalently(self):
        assert crypto.sha256_hex('x') == crypto.sha256_hex(b'x')

    def test_hmac_matches_stdlib(self):
        import hmac
        import hashlib
        expected = hmac.new(b'key', b'msg', hashlib.sha256).hexdigest()
        assert crypto.hmac_sha256_hex('key', 'msg') == expected

    def test_constant_time_equals(self):
        assert crypto.constant_time_equals('abc', 'abc') is True
        assert crypto.constant_time_equals('abc', 'abd') is False

    def test_generate_token_prefix_and_uniqueness(self):
        t1 = crypto.generate_token(16, prefix='tvr_')
        t2 = crypto.generate_token(16, prefix='tvr_')
        assert t1.startswith('tvr_') and t2.startswith('tvr_')
        assert t1 != t2
        assert len(t1) == len('tvr_') + 32  # 16 bytes -> 32 hex chars


class TestSourceUrlValidation:
    @pytest.mark.parametrize('url', [
        'rtsp://192.168.1.50:554/stream',     # LAN camera — allowed
        'rtsps://cam.local:8555/feed',
        'srt://10.0.0.5:8890',
        'http://127.0.0.1:8888/x.m3u8',       # loopback (local MediaMTX) — allowed
        'https://example.com/live.m3u8',
    ])
    def test_valid_sources(self, url):
        ok, err = validate_source_url(url)
        assert ok is True, err

    @pytest.mark.parametrize('url', [
        'file:///etc/passwd',
        'pipe:0',
        'gopher://evil/',
        'http://169.254.169.254/latest/meta-data/',   # cloud-metadata SSRF
        'http://0.0.0.0/',
        'rtsp://',                                     # no host
        'not-a-url',                                   # no scheme
        '',
        None,
        123,
    ])
    def test_rejected_sources(self, url):
        ok, err = validate_source_url(url)
        assert ok is False
        assert err


class TestApiKeyCache:
    def _patch_keyfile(self, monkeypatch, tmp_path):
        from app import auth
        monkeypatch.setattr(auth, '_API_KEYS_FILE', str(tmp_path / 'api_keys.json'))
        auth._api_key_cache['mtime'] = None
        auth._api_key_cache['hashes'] = frozenset()
        return auth

    def test_generated_key_validates(self, tmp_path, monkeypatch):
        auth = self._patch_keyfile(monkeypatch, tmp_path)
        raw = auth.generate_api_key('test-key')
        assert auth._validate_api_key(raw) is True
        assert auth._validate_api_key('tvr_does_not_exist') is False

    def test_revoked_key_rejected(self, tmp_path, monkeypatch):
        auth = self._patch_keyfile(monkeypatch, tmp_path)
        raw = auth.generate_api_key('test-key')
        assert auth.revoke_api_key(auth._hash_key(raw)) is True
        auth._api_key_cache['mtime'] = None  # a fresh worker would reload too
        assert auth._validate_api_key(raw) is False

    def test_cache_consulted_without_rereading_file(self, tmp_path, monkeypatch):
        auth = self._patch_keyfile(monkeypatch, tmp_path)
        raw = auth.generate_api_key('test-key')
        assert auth._validate_api_key(raw) is True   # populates cache
        # Tamper the cache only (file unchanged). Unchanged mtime => the cached
        # (now-empty) set is trusted, proving we don't hit disk every request.
        auth._api_key_cache['hashes'] = frozenset()
        assert auth._validate_api_key(raw) is False


# Short-lived / long-lived helper commands for process tests (portable).
_EXIT_NOW = [sys.executable, '-c', 'pass']
_SLEEP_LONG = [sys.executable, '-c', 'import time; time.sleep(30)']
_WRITE_STDERR = [sys.executable, '-c', 'import sys; sys.stderr.write("boom-on-stderr\\n"); sys.stderr.flush()']


class TestManagedProcess:
    def test_start_alive_then_exits(self, tmp_path):
        mp = ManagedProcess('p', _SLEEP_LONG, str(tmp_path))
        assert mp.start() is True
        assert mp.is_alive() is True
        assert isinstance(mp.pid, int)
        mp.stop(term_timeout=3)
        assert mp.is_alive() is False
        assert mp.poll() is not None

    def test_stderr_captured_and_tailed(self, tmp_path):
        mp = ManagedProcess('errp', _WRITE_STDERR, str(tmp_path))
        assert mp.start() is True
        mp.wait(timeout=5)
        assert any('boom-on-stderr' in line for line in mp.tail_stderr())
        assert os.path.isfile(mp.stderr_path)

    def test_start_failure_returns_false(self, tmp_path):
        mp = ManagedProcess('bad', ['/nonexistent/binary-xyz-123'], str(tmp_path))
        assert mp.start() is False
        assert mp.is_alive() is False

    def test_kill_is_fire_and_forget(self, tmp_path):
        mp = ManagedProcess('k', _SLEEP_LONG, str(tmp_path))
        assert mp.start() is True
        mp.kill()
        # Give the OS a moment to reap via poll()
        for _ in range(20):
            if mp.poll() is not None:
                break
            time.sleep(0.05)
        assert mp.poll() is not None

    def test_graceful_quit_via_stdin(self, tmp_path):
        # Child exits 0 iff it reads 'q' from stdin (models FFmpeg's q-to-quit).
        cmd = [sys.executable, '-c', 'import sys; sys.exit(0 if sys.stdin.read(1) == "q" else 3)']
        mp = ManagedProcess('q', cmd, str(tmp_path), stdin_pipe=True)
        assert mp.start() is True
        assert mp.request_graceful_quit() is True
        assert mp.wait(timeout=5) == 0
        assert mp.returncode == 0

    def test_graceful_quit_noop_without_pipe(self, tmp_path):
        mp = ManagedProcess('np', _SLEEP_LONG, str(tmp_path))
        assert mp.start() is True
        assert mp.request_graceful_quit() is False  # no stdin pipe configured
        mp.stop(term_timeout=3)

    def test_close_releases_log_fd_idempotently(self, tmp_path):
        mp = ManagedProcess('c', _EXIT_NOW, str(tmp_path))
        assert mp.start() is True
        mp.wait(timeout=5)
        mp.close()
        mp.close()  # idempotent — must not raise
        assert isinstance(mp.tail_stderr(), list)

    def test_registry_hooks_fire_on_spawn_and_exit(self, tmp_path):
        from app.services import process as proc_mod
        spawned, exited = [], []
        proc_mod.set_registry_hooks(
            on_spawn=lambda pid, label, cmd: spawned.append(pid),
            on_exit=lambda pid: exited.append(pid))
        try:
            mp = ManagedProcess('h', _EXIT_NOW, str(tmp_path))
            assert mp.start() is True
            pid = mp.pid
            mp.wait(timeout=5)
            mp.close()
            assert spawned == [pid]
            assert exited == [pid]
        finally:
            proc_mod.set_registry_hooks(None, None)  # don't leak into other tests


class TestSupervise:
    def test_restarts_until_stop(self, tmp_path):
        counter = {'n': 0}
        reasons = []

        def make():
            return ManagedProcess(f'p{counter["n"]}', _EXIT_NOW, str(tmp_path))

        def should_stop():
            return counter['n'] >= 3

        def on_restart(reason, tail):
            counter['n'] += 1
            reasons.append(reason)

        supervise(make, should_stop, on_restart=on_restart,
                  check_interval=0.05, base_backoff=0.01, max_backoff=0.05,
                  startup_grace=0.0, healthy_after=999)
        assert counter['n'] >= 3
        assert reasons[:3] == ['exited', 'exited', 'exited']

    def test_detects_stall_and_restarts(self, tmp_path):
        counter = {'n': 0}
        events = []

        def make():
            return ManagedProcess('sleeper', _SLEEP_LONG, str(tmp_path))

        def should_stop():
            return counter['n'] >= 1

        def always_stalled(mp):
            return True

        def on_restart(reason, tail):
            counter['n'] += 1
            events.append(reason)

        supervise(make, should_stop, is_stalled=always_stalled, on_restart=on_restart,
                  check_interval=0.05, startup_grace=0.0,
                  base_backoff=0.01, max_backoff=0.02)
        assert events == ['stall']

    def test_stops_promptly_when_requested(self, tmp_path):
        stop_flag = {'v': True}  # stop immediately

        def make():
            return ManagedProcess('p', _SLEEP_LONG, str(tmp_path))

        # should_stop True from the start -> loop returns without spawning.
        supervise(lambda: make(), lambda: stop_flag['v'], check_interval=0.05)
        # Nothing to assert beyond "it returned"; reaching here means no hang.


class TestMetricsCache:
    """The /metrics MediaMTX probe is cached so a high scrape rate doesn't issue
    one (2s-blocking) network call per scrape (F4)."""

    def test_mediamtx_probe_is_cached(self, monkeypatch):
        from app.api import metrics
        metrics._mediamtx_up_cache['ts'] = 0.0  # force a cold cache
        calls = {'n': 0}

        class _Resp:
            status_code = 200

        def fake_get(*a, **k):
            calls['n'] += 1
            return _Resp()

        monkeypatch.setattr(metrics.http_requests, 'get', fake_get)
        assert metrics._mediamtx_up() == 1
        assert metrics._mediamtx_up() == 1  # served from cache
        assert calls['n'] == 1


class TestReconcile:
    """Startup reconciliation of orphaned FFmpeg via the PID registry (A3)."""

    def test_register_unregister_roundtrip(self, tmp_path, monkeypatch):
        from app.services import reconcile
        monkeypatch.setattr(reconcile, '_REGISTRY_FILE', str(tmp_path / 'reg.json'))
        reconcile.register(111, 'rec:a', ['ffmpeg', '-i', 'x', 'out.mov'])
        reconcile.register(222, 'pull:b', ['ffmpeg', 'rtsp://localhost:8554/b'])
        assert set(reconcile._load().keys()) == {'111', '222'}
        reconcile.unregister(111)
        assert set(reconcile._load().keys()) == {'222'}

    def test_kills_alive_ffmpeg_orphan_only(self, tmp_path, monkeypatch):
        from app.services import reconcile
        monkeypatch.setattr(reconcile, '_REGISTRY_FILE', str(tmp_path / 'reg.json'))
        registry = {
            '101': {'label': 'rec:a', 'cmd': 'ffmpeg -i rtsp://x /data/a/recording-1.mov'},
            '102': {'label': 'pull:b', 'cmd': 'ffmpeg -i rtsp://y rtsp://localhost:8554/b'},
            '103': {'label': 'rec:c', 'cmd': 'ffmpeg -i rtsp://z /data/c/recording-2.mov'},
        }
        killed = []
        # 101 alive + still ffmpeg + matches -> killed.
        # 102 dead -> skipped. 103 alive but PID reused by non-ffmpeg -> skipped.
        reaped = reconcile.reconcile_orphans(
            registry=registry,
            _is_alive=lambda pid: pid in (101, 103),
            _cmdline=lambda pid: {
                101: 'ffmpeg -i rtsp://x /data/a/recording-1.mov',
                103: 'python unrelated-process',
            }.get(pid, ''),
            _kill=killed.append)
        assert reaped == [101]
        assert killed == [101]
        assert reconcile._load() == {}  # registry reset for the fresh instance

    def test_pid_reuse_guard_does_not_kill_mismatched_cmdline(self, tmp_path, monkeypatch):
        from app.services import reconcile
        monkeypatch.setattr(reconcile, '_REGISTRY_FILE', str(tmp_path / 'reg.json'))
        registry = {'200': {'label': 'rec:a', 'cmd': 'ffmpeg -i x /data/a/recording-9.mov'}}

        def _must_not_kill(pid):
            raise AssertionError('reused PID must not be killed')

        reaped = reconcile.reconcile_orphans(
            registry=registry,
            _is_alive=lambda pid: True,
            _cmdline=lambda pid: 'ffmpeg -i other /data/z/recording-other.mov',
            _kill=_must_not_kill)
        assert reaped == []


class TestServerSettingsPersistence:
    def test_persist_then_load_roundtrip(self, tmp_path, monkeypatch):
        from app.api import settings as settings_mod
        monkeypatch.setattr(settings_mod, '_SERVER_SETTINGS_FILE',
                            str(tmp_path / 'server_settings.json'))
        orig = settings_mod.server_settings.get('stall_threshold_seconds')
        try:
            settings_mod.server_settings['stall_threshold_seconds'] = 99
            settings_mod._persist_server_settings()
            settings_mod.server_settings['stall_threshold_seconds'] = 30  # simulate restart
            settings_mod._load_server_settings()
            assert settings_mod.server_settings['stall_threshold_seconds'] == 99
        finally:
            settings_mod.server_settings['stall_threshold_seconds'] = orig

    def test_invalid_and_unknown_persisted_values_ignored(self, tmp_path, monkeypatch):
        from app.api import settings as settings_mod
        f = str(tmp_path / 'server_settings.json')
        monkeypatch.setattr(settings_mod, '_SERVER_SETTINGS_FILE', f)
        write_json_atomic(f, {
            'stall_threshold_seconds': 999999,  # out of range -> ignored
            'reconnect_delay': 7,               # valid -> applied
            'bogus_key': 1,                     # unknown -> ignored
        })
        orig_stall = settings_mod.server_settings['stall_threshold_seconds']
        orig_delay = settings_mod.server_settings['reconnect_delay']
        try:
            settings_mod.server_settings['stall_threshold_seconds'] = 30
            settings_mod.server_settings['reconnect_delay'] = 5
            settings_mod._load_server_settings()
            assert settings_mod.server_settings['stall_threshold_seconds'] == 30
            assert settings_mod.server_settings['reconnect_delay'] == 7
            assert 'bogus_key' not in settings_mod.server_settings
        finally:
            settings_mod.server_settings['stall_threshold_seconds'] = orig_stall
            settings_mod.server_settings['reconnect_delay'] = orig_delay
