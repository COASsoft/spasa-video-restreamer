"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Auth API Blueprint — login/logout, API key management, audit log viewer
"""
import logging
from flask import Blueprint, request, jsonify

from flask_login import login_user, logout_user, current_user

from app.auth import (
    _check_credentials, auth_required, audit_log, read_audit_log,
    generate_api_key, revoke_api_key, list_api_keys,
    _DEFAULT_CREDS, _ROLE_RANK, ROLE_VIEWER,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


# ------------------------------------------------------------------
# Login / logout
# ------------------------------------------------------------------

@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    """Authenticate and create session."""
    data = request.get_json() or {}
    username = data.get('username', '')
    password = data.get('password', '')

    user = _check_credentials(username, password)
    if user:
        login_user(user, remember=True)
        audit_log('login', f'User logged in', user=username)
        return jsonify({
            'success': True,
            'redirect': '/',
            'default_password': _DEFAULT_CREDS,
        })

    audit_log('login_failed', f'Failed attempt for user: {username}', user=request.remote_addr)
    logger.warning(f"Failed login attempt for user '{username}' from {request.remote_addr}")
    return jsonify({'error': 'Invalid username or password'}), 401


@auth_bp.route('/api/auth/logout', methods=['POST'])
@auth_required
def logout():
    """End session."""
    audit_log('logout', 'User logged out')
    logout_user()
    return jsonify({'success': True})


@auth_bp.route('/api/auth/status', methods=['GET'])
def auth_status():
    """Public endpoint — returns whether auth is available and if default pw is set."""
    return jsonify({
        'authenticated': current_user.is_authenticated,
        'default_password': _DEFAULT_CREDS,
        'username': current_user.username if current_user.is_authenticated else None,
    })


@auth_bp.route('/api/auth/me', methods=['GET'])
@auth_required
def auth_me():
    """Return current user info."""
    return jsonify({
        'username': current_user.username,
        'default_password': current_user.is_default_password,
    })


# ------------------------------------------------------------------
# API key management
# ------------------------------------------------------------------

@auth_bp.route('/api/auth/keys', methods=['GET'])
@auth_required
def get_api_keys():
    """List all API keys (hash + name + created, never the raw key)."""
    return jsonify({'keys': list_api_keys()})


@auth_bp.route('/api/auth/keys', methods=['POST'])
@auth_required
def create_api_key():
    """Generate a new API key. Returns the raw key ONCE."""
    data = request.get_json() or {}
    name = data.get('name', 'unnamed')
    if not name or len(name) > 64:
        return jsonify({'error': 'Key name required (max 64 chars)'}), 400
    role = data.get('role', ROLE_VIEWER)
    if role not in _ROLE_RANK:
        return jsonify({'error': f'Invalid role: {role!r} (expected viewer/operator/admin)'}), 400

    raw_key = generate_api_key(name, role=role)
    audit_log('api_key_created', f'Key created: {name} (role={role})')
    return jsonify({
        'success': True,
        'key': raw_key,
        'name': name,
        'role': role,
        'note': 'Save this key — it cannot be retrieved again.',
    })


@auth_bp.route('/api/auth/keys/<key_hash>', methods=['DELETE'])
@auth_required
def delete_api_key(key_hash):
    """Revoke an API key by hash."""
    if revoke_api_key(key_hash):
        audit_log('api_key_revoked', f'Key revoked: {key_hash[:12]}...')
        return jsonify({'success': True})
    return jsonify({'error': 'Key not found'}), 404


# ------------------------------------------------------------------
# Audit log viewer
# ------------------------------------------------------------------

@auth_bp.route('/api/audit', methods=['GET'])
@auth_required
def get_audit_log():
    """Return recent audit log entries."""
    lines = int(request.args.get('lines', 200))
    return jsonify({'entries': read_audit_log(lines)})
