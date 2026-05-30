"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Flask application factory
"""
import os
import re
import json
import uuid
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, send_from_directory, redirect, url_for, request, jsonify, g
from flask_cors import CORS
from flask_socketio import SocketIO

# Import configuration and state
from app.config import (SECRET_KEY, PORT, CORS_ORIGINS, LOG_LEVEL, LOGS_DIR, LOG_MAX_BYTES,
                        LOG_BACKUP_COUNT, LOG_JSON, validate_runtime_config)

# Import blueprints
from app.api import health_bp, streams_bp, recordings_bp, settings_bp, utils_bp, test_bp, hls_bp, auth_bp, tls_bp, metrics_bp, klv_bp, dvr_bp

# Import websocket handlers
from app.websocket import set_socketio, register_handlers

# Import auth module
from app.auth import (init_auth, auth_required, resolve_identity, role_satisfies,
                      audit_log, ROLE_VIEWER, ROLE_OPERATOR, ROLE_ADMIN)


# ---------------------------------------------------------------------------
# Centralized RBAC table (single source of truth for route → minimum role)
# ---------------------------------------------------------------------------
# (compiled-regex, allowed-methods | None, min-role). First match wins; matching
# is on request.path. Keeping this in ONE place makes the authz model auditable
# and gives a fail-closed default: any /api/* route not listed below requires
# operator for mutating methods and viewer for reads — so a newly-added mutating
# endpoint is operator-gated even if the author forgets to register it here.
_M = None  # "any method"
_RBAC_RULES = [
    # --- admin-only ---
    (re.compile(r'^/api/auth/keys'), _M, ROLE_ADMIN),
    (re.compile(r'^/api/audit'), _M, ROLE_ADMIN),
    (re.compile(r'^/api/tls/'), {'POST'}, ROLE_ADMIN),
    (re.compile(r'^/api/settings/certificates/'), {'POST'}, ROLE_ADMIN),
    (re.compile(r'^/api/settings$'), {'POST'}, ROLE_ADMIN),
    # --- operator (mutating ops) ---
    (re.compile(r'^/api/streams/[^/]+/abr'), {'POST', 'DELETE'}, ROLE_OPERATOR),
    (re.compile(r'^/api/streams/[^/]+/(stop|pull|stop-pull|standby|record|stop-record|buffer)'), _M, ROLE_OPERATOR),
    (re.compile(r'^/api/streams/[^/]+/viewers/.+/block'), {'POST'}, ROLE_OPERATOR),
    (re.compile(r'^/api/streams/[^/]+$'), {'POST', 'DELETE'}, ROLE_OPERATOR),
    (re.compile(r'^/api/recordings/.+/(keywords|generate-thumbnail)'), {'POST'}, ROLE_OPERATOR),
    (re.compile(r'^/api/recordings/bulk-delete'), {'POST'}, ROLE_OPERATOR),
    (re.compile(r'^/api/recordings/.+'), {'DELETE'}, ROLE_OPERATOR),
    (re.compile(r'^/api/blocked-ips/'), {'DELETE'}, ROLE_OPERATOR),
    (re.compile(r'^/api/transcode($|/)'), {'POST', 'DELETE'}, ROLE_OPERATOR),
    (re.compile(r'^/api/klv/'), {'POST'}, ROLE_OPERATOR),
    (re.compile(r'^/api/test/.+'), {'POST'}, ROLE_OPERATOR),
    (re.compile(r'^/api/auto-record-toggle'), {'POST'}, ROLE_OPERATOR),
    (re.compile(r'^/api/settings/(srt|abr|auto-record)'), {'POST'}, ROLE_OPERATOR),
]

# Methods that never mutate state — the read tier of the catch-all default.
_READ_METHODS = ('GET', 'HEAD', 'OPTIONS')


def _required_role(path, method):
    """Minimum role for (path, method). Fail-closed default for unlisted routes."""
    for rx, methods, role in _RBAC_RULES:
        if rx.match(path) and (methods is None or method in methods):
            return role
    return ROLE_VIEWER if method in _READ_METHODS else ROLE_OPERATOR


def create_app():
    """
    Create and configure the Flask application
    """
    # Get absolute paths for static and template folders
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_folder = os.path.join(base_dir, 'web', 'static')
    template_folder = os.path.join(base_dir, 'web')
    
    # Initialize Flask app
    app = Flask(__name__, 
                static_folder=static_folder,
                static_url_path='/static',
                template_folder=template_folder)
    
    # Configure Flask
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max upload
    app.config['REMEMBER_COOKIE_DURATION'] = 86400 * 7      # 7 days
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting TAK Video Restreamer")

    # Fail-closed validation of security-critical configuration (default
    # password, etc.). Raises ConfigError and aborts startup on misconfig.
    validate_runtime_config()
    
    # Initialize authentication
    init_auth(app)

    # Initialize rate limiter
    try:
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address
        limiter = Limiter(
            get_remote_address,
            app=app,
            default_limits=[],  # No global limit, applied per-route
            storage_uri="memory://",
        )
        app.limiter = limiter
        logger.info("Rate limiter initialized")
    except ImportError:
        logger.warning("Flask-Limiter not installed — rate limiting disabled")
        app.limiter = None
    
    # Configure CORS
    if CORS_ORIGINS == '*':
        logger.warning("⚠️  CORS is allowing ALL origins (*) - This is insecure for production!")
        logger.warning("⚠️  Set CORS_ORIGINS environment variable to restrict access")
        CORS(app, resources={r"/api/*": {"origins": "*"}})
    else:
        allowed_origins = [origin.strip() for origin in CORS_ORIGINS.split(',')]
        logger.info(f"CORS restricted to origins: {allowed_origins}")
        CORS(app, resources={r"/api/*": {"origins": allowed_origins}})
    
    # Initialize SocketIO
    # async_mode='eventlet' matches the Gunicorn --worker-class eventlet deployment.
    # In direct socketio.run() (dev mode) eventlet is also used, staying consistent.
    # On Python 3.13+, eventlet is incompatible; set SOCKETIO_ASYNC_MODE=threading to override.
    socketio_async_mode = os.environ.get('SOCKETIO_ASYNC_MODE', 'eventlet')
    socketio = SocketIO(app,
                       cors_allowed_origins=CORS_ORIGINS,
                       async_mode=socketio_async_mode,
                       ping_timeout=60,
                       ping_interval=25)
    
    # Inject SocketIO into broadcast module
    set_socketio(socketio)
    
    # Register WebSocket event handlers
    register_handlers(socketio)
    
    # Register blueprints
    app.register_blueprint(health_bp)
    app.register_blueprint(streams_bp)
    app.register_blueprint(recordings_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(utils_bp)
    app.register_blueprint(test_bp)
    app.register_blueprint(hls_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(tls_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(klv_bp)
    app.register_blueprint(dvr_bp)

    # Apply rate limiting to login endpoint
    if app.limiter:
        login_view = app.view_functions.get('auth.login')
        if login_view:
            app.limiter.limit("5 per minute")(login_view)

    # Protect ALL API routes except public ones (health, auth login/status, HLS).
    # NOTE: /metrics is intentionally outside this gate (root path, for Prometheus
    # scraping); it has its own optional bearer guard (METRICS_TOKEN) and should be
    # restricted at the nginx edge in production.
    # NOTE: /hls/ and /api/hls/proxy/ stay public HERE but are NOT open — each HLS
    # view is fail-closed via _hls_access_allowed (auth OR a valid signed URL), so
    # the gate does not have to special-case signature logic.
    _PUBLIC_PREFIXES = ('/api/health', '/api/auth/login', '/api/auth/status',
                        '/login', '/static/', '/hls/', '/api/hls/proxy/')
    _PAGE_ROUTES = ('/', '/recordings', '/settings', '/utils', '/test', '/videowall')

    # Correlation id for every request (registered before the auth gate so it runs
    # first and covers public paths too). Prefer the id nginx forwards (X-Request-ID)
    # for cross-tier correlation; otherwise originate one.
    @app.before_request
    def _assign_request_id():
        g.request_id = request.headers.get('X-Request-ID') or uuid.uuid4().hex

    @app.before_request
    def _enforce_auth():
        path, method = request.path, request.method
        # Allow public paths
        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return None
        is_api = path.startswith('/api/')
        if not (is_api or path in _PAGE_ROUTES):
            return None
        # Authenticate (session > mTLS > API key > Basic), populating g.current_role.
        if not resolve_identity():
            if is_api:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login_page'))
        # Page routes: any authenticated role may load the SPA shell; the data
        # calls it makes are individually role-gated below.
        if not is_api:
            return None
        # Enforce the minimum role for this API route.
        need = _required_role(path, method)
        if not role_satisfies(g.current_role, need):
            audit_log('rbac_denied', f'{method} {path} need={need} have={g.current_role}')
            return jsonify({'error': 'Insufficient privileges'}), 403
        return None
    
    # Security response headers (defence-in-depth; safe for media fetches).
    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        response.headers.setdefault('Referrer-Policy', 'no-referrer')
        # HSTS is only honoured by browsers over HTTPS; set it when the edge
        # (nginx) terminated TLS or the request is otherwise secure.
        if request.headers.get('X-Forwarded-Proto', '').lower() == 'https' or request.is_secure:
            response.headers.setdefault(
                'Strict-Transport-Security', 'max-age=31536000; includeSubDomains'
            )
        # Echo the correlation id so clients/log collectors can stitch a request together.
        rid = getattr(g, 'request_id', None)
        if rid:
            response.headers.setdefault('X-Request-ID', rid)
        return response

    # Static file routes (protected by auth)
    @app.route('/login')
    def login_page():
        return send_from_directory(static_folder, 'login.html')

    @app.route('/')
    @auth_required
    def index():
        return send_from_directory(static_folder, 'index.html')
    
    @app.route('/recordings')
    @auth_required
    def recordings_page():
        return send_from_directory(static_folder, 'recordings.html')
    
    @app.route('/settings')
    @auth_required
    def settings_page():
        return send_from_directory(static_folder, 'settings.html')
    
    @app.route('/utils')
    @auth_required
    def utils_page():
        return send_from_directory(static_folder, 'utils.html')
    
    @app.route('/test')
    @auth_required
    def test_page():
        return send_from_directory(static_folder, 'test_video_input.html')
    
    @app.route('/videowall')
    @auth_required
    def videowall_page():
        return send_from_directory(static_folder, 'videowall.html')
    
    logger.info(f"Flask app initialized with {len(app.blueprints)} blueprints")
    
    # Store socketio instance on app for access in main
    app.socketio = socketio

    # Restore ABR state from previous run (streams that had ABR enabled)
    from app.services.abr import abr_manager as _abr
    _abr.restore_state()
    
    return app


class _CorrelationFilter(logging.Filter):
    """Attach the per-request correlation id to every record (``-`` when there is
    no request context, e.g. background threads/startup)."""
    def filter(self, record):
        rid = '-'
        try:
            rid = getattr(g, 'request_id', '-') or '-'
        except Exception:
            rid = '-'
        record.correlation_id = rid
        return True


class JsonFormatter(logging.Formatter):
    """Dependency-free structured JSON log formatter (one object per line)."""
    def format(self, record):
        payload = {
            'ts': self.formatTime(record),
            'level': record.levelname,
            'logger': record.name,
            'correlation_id': getattr(record, 'correlation_id', '-'),
            'msg': record.getMessage(),
        }
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging():
    """Configure application logging with rotation.

    LOG_JSON=true emits structured JSON with a correlation id (for SIEM/log
    collectors); otherwise the human-readable format is kept (dev default).
    """
    os.makedirs(LOGS_DIR, exist_ok=True)

    if LOG_JSON:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - [%(correlation_id)s] - %(message)s'
        )
    corr_filter = _CorrelationFilter()

    log_file = os.path.join(LOGS_DIR, 'app.log')
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(corr_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(corr_filter)

    # Configure root logger (force=True so re-init in tests/factory takes effect).
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        handlers=[console_handler, file_handler],
        force=True,
    )

    # Set specific loggers to WARNING to reduce noise
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)
    logging.getLogger('socketio').setLevel(logging.WARNING)
