"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

API blueprints initialization
"""
from .health import health_bp
from .streams import streams_bp
from .recordings import recordings_bp
from .settings import settings_bp
from .utils import utils_bp
from .test import test_bp
from .hls import hls_bp
from .auth_api import auth_bp
from .tls_api import tls_bp
from .metrics import metrics_bp

__all__ = [
    'health_bp', 'streams_bp', 'recordings_bp', 'settings_bp',
    'utils_bp', 'test_bp', 'hls_bp', 'auth_bp', 'tls_bp', 'metrics_bp',
]
