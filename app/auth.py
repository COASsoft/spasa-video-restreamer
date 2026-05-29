"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Authentication module - Flask-Login based session auth + API key support

Credentials come from environment variables:
  ADMIN_USERNAME  (default: admin)
  ADMIN_PASSWORD  (default: changeme)

API keys are stored in DATA_DIR/api_keys.json
"""
import os
import logging
import functools
from datetime import datetime, timezone

from flask import request, jsonify, redirect, url_for, session, g
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required

from app.config import (DATA_DIR, MTLS_ENABLED, PROXY_SHARED_SECRET, CERT_ROLE_MAP_FILE,
                        CERT_DEFAULT_ROLE, API_KEY_DEFAULT_ROLE)
from app.utils.atomic_json import write_json_atomic, read_json
from app.utils.crypto import sha256_hex, constant_time_equals, generate_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Roles (RBAC)
# ---------------------------------------------------------------------------
# Hierarchical: a higher rank satisfies any lower requirement, so
# require_role('operator') admits operators AND admins.
ROLE_VIEWER = 'viewer'
ROLE_OPERATOR = 'operator'
ROLE_ADMIN = 'admin'
_ROLE_RANK = {ROLE_VIEWER: 10, ROLE_OPERATOR: 20, ROLE_ADMIN: 30}


def role_satisfies(have, need) -> bool:
    """True if role ``have`` meets or exceeds ``need``."""
    return _ROLE_RANK.get(have or '', -1) >= _ROLE_RANK.get(need, 99)


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class User(UserMixin):
    """Simple user model backed by environment variables."""
    def __init__(self, user_id, username, is_default_password=False, role=ROLE_ADMIN):
        self.id = user_id
        self.username = username
        self.is_default_password = is_default_password
        self.role = role


# Resolve admin credentials from env
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme')

_DEFAULT_CREDS = (ADMIN_PASSWORD == 'changeme')

# In-memory user store (single admin user)
_admin_user = User('1', ADMIN_USERNAME, is_default_password=_DEFAULT_CREDS)


def _get_user_by_id(user_id):
    if user_id == _admin_user.id:
        return _admin_user
    return None


def _check_credentials(username, password):
    """Return User or None. Uses constant-time comparison to resist timing attacks."""
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    user_ok = constant_time_equals(username, ADMIN_USERNAME)
    pass_ok = constant_time_equals(password, ADMIN_PASSWORD)
    if user_ok and pass_ok:
        return _admin_user
    return None


# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------

_API_KEYS_FILE = os.path.join(DATA_DIR, 'api_keys.json')

# In-memory cache of valid keys (hash → metadata), invalidated by the file's
# mtime so we don't read+parse the JSON on every authenticated request. The
# mtime check also makes the cache correct across multiple workers (each reloads
# when the file changes after a create/revoke).
_api_key_cache = {'mtime': None, 'keys': {}}


def _load_api_keys() -> dict:
    """Return {key_hash: {name, created, role}} dict."""
    return read_json(_API_KEYS_FILE, default={}) or {}


def _save_api_keys(keys: dict):
    write_json_atomic(_API_KEYS_FILE, keys)


def _hash_key(raw_key: str) -> str:
    return sha256_hex(raw_key)


def generate_api_key(name: str, role: str = ROLE_VIEWER) -> str:
    """Generate a new API key, persist the hash + role, return the raw key."""
    if role not in _ROLE_RANK:
        role = ROLE_VIEWER
    raw_key = generate_token(24, prefix='tvr_')
    keys = _load_api_keys()
    keys[_hash_key(raw_key)] = {
        'name': name,
        'created': datetime.now(timezone.utc).isoformat(),
        'role': role,
    }
    _save_api_keys(keys)
    logger.info(f"API key created: {name} (role={role})")
    return raw_key


def revoke_api_key(key_hash: str) -> bool:
    """Revoke an API key by its hash."""
    keys = _load_api_keys()
    if key_hash in keys:
        name = keys[key_hash]['name']
        del keys[key_hash]
        _save_api_keys(keys)
        logger.info(f"API key revoked: {name}")
        return True
    return False


def list_api_keys() -> list:
    """List API keys (hashes + metadata, never the raw key)."""
    keys = _load_api_keys()
    return [{'hash': h, **meta} for h, meta in keys.items()]


def _validate_api_key(raw_key: str):
    """Return a valid key's metadata dict (with 'role'), else None.

    Cached, refreshed on file change. Returns a dict (truthy) on success and
    None (falsy) on failure, so existing boolean call sites stay correct.
    Legacy entries without a 'role' field default to API_KEY_DEFAULT_ROLE.
    """
    if not raw_key:
        return None
    try:
        mtime = os.path.getmtime(_API_KEYS_FILE)
    except OSError:
        mtime = None
    if mtime != _api_key_cache['mtime']:
        _api_key_cache['keys'] = _load_api_keys()
        _api_key_cache['mtime'] = mtime
    meta = _api_key_cache['keys'].get(_hash_key(raw_key))
    if meta is None:
        return None
    if 'role' not in meta:
        meta = {**meta, 'role': API_KEY_DEFAULT_ROLE}
    return meta


# ---------------------------------------------------------------------------
# mTLS client-cert identity (via trusted reverse proxy)
# ---------------------------------------------------------------------------

def _proxy_trusted() -> bool:
    """True if the request carries the shared secret nginx attaches. Only then
    are the X-SSL-Client-* headers trustworthy (anti-spoof)."""
    if not PROXY_SHARED_SECRET:
        return False
    provided = request.headers.get('X-Proxy-Auth', '')
    return bool(provided) and constant_time_equals(provided, PROXY_SHARED_SECRET)


def _load_cert_role_map() -> dict:
    """Return the cert→role map: {cn_roles, dn_roles, default_role}."""
    return read_json(CERT_ROLE_MAP_FILE, default={}) or {}


def _resolve_mtls_identity():
    """Return (cn, dn, role) for a verified client cert presented via the trusted
    proxy, else None. ``role`` is None when the cert is authenticated but unmapped
    and no default is configured (fail-closed)."""
    if not MTLS_ENABLED or not _proxy_trusted():
        return None
    if request.headers.get('X-SSL-Client-Verify', '') != 'SUCCESS':
        return None
    cn = request.headers.get('X-SSL-Client-CN', '') or ''
    dn = request.headers.get('X-SSL-Client-DN', '') or ''
    if not cn and not dn:
        return None
    m = _load_cert_role_map()
    role = (m.get('cn_roles') or {}).get(cn)
    if role is None:
        role = (m.get('dn_roles') or {}).get(dn)
    if role is None:
        role = m.get('default_role', CERT_DEFAULT_ROLE)
    if role is not None and role not in _ROLE_RANK:
        role = None  # invalid mapping → fail-closed
    return (cn, dn, role)


# ---------------------------------------------------------------------------
# Unified identity resolution (session > mTLS > API key > Basic)
# ---------------------------------------------------------------------------

def resolve_identity() -> bool:
    """Resolve the request's identity + role from the highest-trust source present.

    Sets ``g.current_identity`` (str|None) and ``g.current_role`` (str|None), plus
    ``g.cert_cn``/``g.cert_dn`` for audit. Idempotent within a request (cached on
    g). Returns True iff a source authenticated AND yielded a role (an unmapped
    cert authenticates but yields no role → returns False, fail-closed)."""
    if getattr(g, '_identity_resolved', False):
        return g.current_role is not None
    g._identity_resolved = True
    g.current_identity = None
    g.current_role = None
    g.cert_cn = None
    g.cert_dn = None

    # 1. Session (Flask-Login)
    if current_user.is_authenticated:
        g.current_identity = current_user.username
        g.current_role = getattr(current_user, 'role', ROLE_ADMIN)
        return True

    # 2. mTLS client cert via the trusted proxy
    cert = _resolve_mtls_identity()
    if cert:
        cn, dn, role = cert
        g.current_identity = f'cert:{cn or dn}'
        g.cert_cn, g.cert_dn = cn, dn
        if role is not None:
            g.current_role = role
            return True
        return False  # authenticated cert, no role mapping → fail-closed

    # 3. API key (role-aware)
    api_key = request.headers.get('X-API-Key')
    if api_key:
        meta = _validate_api_key(api_key)
        if meta:
            g.current_identity = f'key:{meta.get("name", "?")}'
            g.current_role = meta.get('role', API_KEY_DEFAULT_ROLE)
            return True

    # 4. Basic auth → the admin password user
    auth = request.authorization
    if auth and _check_credentials(auth.username, auth.password):
        g.current_identity = auth.username
        g.current_role = ROLE_ADMIN
        return True

    return False


# ---------------------------------------------------------------------------
# Auth-required decorator that also accepts API keys, Basic auth & mTLS
# ---------------------------------------------------------------------------

def auth_required(f):
    """
    Protect a route. Accepts (in precedence order):
      1. Flask-Login session cookie
      2. mTLS client cert (via trusted proxy)
      3. X-API-Key header
      4. Basic auth header  (username:password)
    Unauthenticated browser requests redirect to /login.
    Unauthenticated API requests get 401.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if resolve_identity():
            return f(*args, **kwargs)
        if _wants_json():
            return jsonify({'error': 'Authentication required'}), 401
        return redirect(url_for('login_page'))

    return decorated


def require_role(min_role):
    """Decorator enforced AFTER authentication (the gate/auth_required already
    resolved identity). 401 if unauthenticated, 403 if the role is insufficient."""
    def wrap(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            resolve_identity()  # idempotent — cheap if the gate already ran it
            if g.current_role is None:
                return jsonify({'error': 'Authentication required'}), 401
            if not role_satisfies(g.current_role, min_role):
                audit_log('rbac_denied',
                          f'{request.method} {request.path} need={min_role} have={g.current_role}')
                return jsonify({'error': 'Insufficient privileges'}), 403
            return f(*args, **kwargs)
        return decorated
    return wrap


def _wants_json():
    """Heuristic: is this an API/XHR call?"""
    if request.path.startswith('/api/'):
        return True
    accept = request.headers.get('Accept', '')
    if 'application/json' in accept:
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    return False


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

_AUDIT_LOG_FILE = os.path.join(DATA_DIR, 'audit.log')


def audit_log(action: str, detail: str = '', user: str = ''):
    """Append a line to the audit log file."""
    if not user:
        try:
            if current_user.is_authenticated:
                user = current_user.username
            # Prefer the resolved identity (carries cert:<cn> / key:<name>) when set.
            if not user:
                user = getattr(g, 'current_identity', None) or ''
        except Exception:
            pass
        if not user:
            user = request.remote_addr if request else 'system'
    # For mTLS requests, attach the full cert DN for traceability.
    try:
        dn = getattr(g, 'cert_dn', None)
        if dn:
            detail = f'{detail} | dn={dn}' if detail else f'dn={dn}'
    except Exception:
        pass
    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} | {user} | {action} | {detail}\n"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_AUDIT_LOG_FILE, 'a') as f:
            f.write(line)
    except Exception as e:
        logger.error(f"Audit log write error: {e}")


def read_audit_log(lines: int = 200) -> list:
    """Return the last N lines of the audit log."""
    try:
        if not os.path.exists(_AUDIT_LOG_FILE):
            return []
        with open(_AUDIT_LOG_FILE, 'r') as f:
            all_lines = f.readlines()
        return [l.strip() for l in all_lines[-lines:]]
    except Exception as e:
        logger.error(f"Audit log read error: {e}")
        return []


# ---------------------------------------------------------------------------
# Init function called from create_app
# ---------------------------------------------------------------------------

login_manager = LoginManager()


def init_auth(app):
    """Initialize Flask-Login on the app."""
    login_manager.init_app(app)
    login_manager.login_view = 'login_page'

    @login_manager.user_loader
    def load_user(user_id):
        return _get_user_by_id(user_id)

    if _DEFAULT_CREDS:
        logger.warning("=" * 60)
        logger.warning("DEFAULT ADMIN PASSWORD IN USE — CHANGE IMMEDIATELY")
        logger.warning("Set ADMIN_PASSWORD environment variable in docker-compose.yml")
        logger.warning("=" * 60)
