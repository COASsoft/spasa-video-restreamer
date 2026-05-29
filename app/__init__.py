"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Flask application factory
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, send_from_directory, redirect, url_for, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO

# Import configuration and state
from app.config import SECRET_KEY, PORT, CORS_ORIGINS, LOG_LEVEL, LOGS_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT, validate_runtime_config

# Import blueprints
from app.api import health_bp, streams_bp, recordings_bp, settings_bp, utils_bp, test_bp, hls_bp, auth_bp, tls_bp, metrics_bp

# Import websocket handlers
from app.websocket import set_socketio, register_handlers

# Import auth module
from app.auth import init_auth, auth_required, _validate_api_key, _check_credentials


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

    # Apply rate limiting to login endpoint
    if app.limiter:
        login_view = app.view_functions.get('auth.login')
        if login_view:
            app.limiter.limit("5 per minute")(login_view)

    # Protect ALL API routes except public ones (health, auth login/status, HLS segments).
    # NOTE: /metrics is intentionally outside this gate (root path, for Prometheus
    # scraping); it has its own optional bearer guard (METRICS_TOKEN) and should be
    # restricted at the nginx edge in production.
    _PUBLIC_PREFIXES = ('/api/health', '/api/auth/login', '/api/auth/status',
                        '/login', '/static/', '/hls/', '/api/hls/proxy/')
    from flask_login import current_user as _cu

    @app.before_request
    def _enforce_auth():
        path = request.path
        # Allow public paths
        for prefix in _PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return None
        # Protect /api/* and page routes
        if path.startswith('/api/') or path in ('/', '/recordings', '/settings', '/utils', '/test'):
            # Already checked by @auth_required on pages, but belt-and-suspenders for API
            if _cu.is_authenticated:
                return None
            # API key
            api_key = request.headers.get('X-API-Key')
            if api_key and _validate_api_key(api_key):
                return None
            # Basic auth
            auth = request.authorization
            if auth and _check_credentials(auth.username, auth.password):
                return None
            # Reject
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login_page'))
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


def setup_logging():
    """Configure application logging with rotation"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    
    # Create rotating file handler
    log_file = os.path.join(LOGS_DIR, 'app.log')
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),  # Console output
            file_handler  # Rotating file output
        ]
    )
    
    # Set specific loggers to WARNING to reduce noise
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)
    logging.getLogger('socketio').setLevel(logging.WARNING)
