"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

TLS Certificate Management Service

Handles:
- Let's Encrypt certificate acquisition via certbot
- Self-signed certificate generation
- Certificate status checking
- RTSPS + HTTPS configuration
"""
import os
import subprocess
import shutil
import logging
from datetime import datetime, timezone

from app.config import DATA_DIR
from app.utils.atomic_json import write_json_atomic, read_json

logger = logging.getLogger(__name__)

# Paths
CERTS_DIR = os.environ.get('ACTIVE_CERTS_DIR', '/opt/app/certs')
LETSENCRYPT_DIR = '/etc/letsencrypt'
TLS_SETTINGS_FILE = os.path.join(DATA_DIR, 'tls_settings.json')

# Defaults
DEFAULT_TLS_SETTINGS = {
    'rtsps_enabled': False,
    'https_enabled': False,
    'cert_type': 'none',     # none | self-signed | letsencrypt
    'letsencrypt_domain': '',
    'letsencrypt_email': '',
}


def _load_tls_settings() -> dict:
    settings = dict(DEFAULT_TLS_SETTINGS)
    saved = read_json(TLS_SETTINGS_FILE, default=None)
    if isinstance(saved, dict):
        settings.update(saved)
    return settings


def _save_tls_settings(settings: dict):
    write_json_atomic(TLS_SETTINGS_FILE, settings)


def get_tls_settings() -> dict:
    return _load_tls_settings()


def update_tls_settings(data: dict) -> dict:
    settings = _load_tls_settings()
    for key in DEFAULT_TLS_SETTINGS:
        if key in data:
            settings[key] = data[key]
    _save_tls_settings(settings)
    return settings


# ------------------------------------------------------------------
# Certificate status
# ------------------------------------------------------------------

def get_cert_status() -> dict:
    """Check current certificate status."""
    cert_path = os.path.join(CERTS_DIR, 'server.crt')
    key_path = os.path.join(CERTS_DIR, 'server.key')

    status = {
        'cert_exists': os.path.exists(cert_path),
        'key_exists': os.path.exists(key_path),
        'cert_type': 'none',
        'subject': None,
        'issuer': None,
        'expires': None,
        'valid': False,
    }

    settings = _load_tls_settings()
    status['cert_type'] = settings.get('cert_type', 'none')
    status['rtsps_enabled'] = settings.get('rtsps_enabled', False)
    status['https_enabled'] = settings.get('https_enabled', False)

    if status['cert_exists']:
        try:
            result = subprocess.run(
                ['openssl', 'x509', '-in', cert_path, '-noout',
                 '-subject', '-issuer', '-enddate', '-checkend', '0'],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().split('\n'):
                if line.startswith('subject='):
                    status['subject'] = line.replace('subject=', '').strip()
                elif line.startswith('issuer='):
                    status['issuer'] = line.replace('issuer=', '').strip()
                elif line.startswith('notAfter='):
                    status['expires'] = line.replace('notAfter=', '').strip()
            status['valid'] = (result.returncode == 0)
        except Exception as e:
            logger.warning(f"Could not read cert details: {e}")

    return status


# ------------------------------------------------------------------
# Self-signed certificate generation
# ------------------------------------------------------------------

def generate_self_signed(common_name: str = 'localhost', days: int = 3650) -> dict:
    """Generate a self-signed certificate."""
    os.makedirs(CERTS_DIR, exist_ok=True)
    cert_path = os.path.join(CERTS_DIR, 'server.crt')
    key_path = os.path.join(CERTS_DIR, 'server.key')

    # Backup existing
    _backup_cert(cert_path, key_path)

    result = subprocess.run([
        'openssl', 'req', '-x509',
        '-newkey', 'rsa:4096',
        '-keyout', key_path,
        '-out', cert_path,
        '-days', str(days),
        '-nodes',
        '-subj', f'/CN={common_name}',
    ], capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        return {'success': False, 'error': f'openssl error: {result.stderr}'}

    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)

    settings = _load_tls_settings()
    settings['cert_type'] = 'self-signed'
    _save_tls_settings(settings)

    logger.info(f"Self-signed certificate generated: CN={common_name}")
    return {'success': True, 'message': 'Self-signed certificate generated. Restart container to apply.'}


# ------------------------------------------------------------------
# Let's Encrypt
# ------------------------------------------------------------------

def request_letsencrypt(domain: str, email: str) -> dict:
    """
    Request a Let's Encrypt certificate using certbot standalone mode.
    Port 80 must be accessible from the internet.
    """
    if not domain:
        return {'success': False, 'error': 'Domain is required'}
    if not email:
        return {'success': False, 'error': 'Email is required for Let\'s Encrypt'}

    # Check certbot is available
    try:
        subprocess.run(['certbot', '--version'], capture_output=True, timeout=5, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {'success': False, 'error': 'certbot is not installed. Install it in the Docker image.'}

    logger.info(f"Requesting Let's Encrypt certificate for {domain}")

    result = subprocess.run([
        'certbot', 'certonly',
        '--standalone',
        '--non-interactive',
        '--agree-tos',
        '--email', email,
        '-d', domain,
        '--cert-path', os.path.join(CERTS_DIR, 'server.crt'),
        '--key-path', os.path.join(CERTS_DIR, 'server.key'),
    ], capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        logger.error(f"certbot error: {result.stderr}")
        return {'success': False, 'error': f'certbot failed: {result.stderr[-500:]}'}

    # Copy LE certs to our certs dir
    le_live = os.path.join(LETSENCRYPT_DIR, 'live', domain)
    if os.path.isdir(le_live):
        _backup_cert(
            os.path.join(CERTS_DIR, 'server.crt'),
            os.path.join(CERTS_DIR, 'server.key'),
        )
        shutil.copy2(os.path.join(le_live, 'fullchain.pem'), os.path.join(CERTS_DIR, 'server.crt'))
        shutil.copy2(os.path.join(le_live, 'privkey.pem'), os.path.join(CERTS_DIR, 'server.key'))
        os.chmod(os.path.join(CERTS_DIR, 'server.key'), 0o600)

    settings = _load_tls_settings()
    settings['cert_type'] = 'letsencrypt'
    settings['letsencrypt_domain'] = domain
    settings['letsencrypt_email'] = email
    _save_tls_settings(settings)

    logger.info(f"Let's Encrypt certificate obtained for {domain}")
    return {
        'success': True,
        'message': f'Certificate obtained for {domain}. Restart container to apply to RTSPS/HTTPS.',
    }


def renew_letsencrypt() -> dict:
    """Renew Let's Encrypt certificates."""
    try:
        subprocess.run(['certbot', '--version'], capture_output=True, timeout=5, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {'success': False, 'error': 'certbot not installed'}

    settings = _load_tls_settings()
    domain = settings.get('letsencrypt_domain', '')
    if not domain:
        return {'success': False, 'error': 'No Let\'s Encrypt domain configured'}

    result = subprocess.run(
        ['certbot', 'renew', '--non-interactive'],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        return {'success': False, 'error': f'Renewal failed: {result.stderr[-500:]}'}

    # Copy renewed certs
    le_live = os.path.join(LETSENCRYPT_DIR, 'live', domain)
    if os.path.isdir(le_live):
        shutil.copy2(os.path.join(le_live, 'fullchain.pem'), os.path.join(CERTS_DIR, 'server.crt'))
        shutil.copy2(os.path.join(le_live, 'privkey.pem'), os.path.join(CERTS_DIR, 'server.key'))
        os.chmod(os.path.join(CERTS_DIR, 'server.key'), 0o600)

    logger.info("Let's Encrypt certificate renewed")
    return {'success': True, 'message': 'Certificates renewed successfully.'}


# ------------------------------------------------------------------
# MediaMTX RTSPS config management
# ------------------------------------------------------------------

def get_mediamtx_rtsps_snippet(enabled: bool) -> str:
    """Return the RTSPS section for mediamtx.yml."""
    if enabled:
        return (
            f"rtspsAddress: 0.0.0.0:8555\n"
            f"rtspEncryption: optional\n"
            f"rtspServerCert: {os.path.join(CERTS_DIR, 'server.crt')}\n"
            f"rtspServerKey: {os.path.join(CERTS_DIR, 'server.key')}\n"
        )
    return (
        "# rtspsAddress: 0.0.0.0:8555\n"
        "# rtspEncryption: optional\n"
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _backup_cert(cert_path: str, key_path: str):
    """Backup existing cert/key files."""
    ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    for path in (cert_path, key_path):
        if os.path.exists(path):
            try:
                shutil.move(path, f'{path}.backup.{ts}')
            except Exception as e:
                logger.warning(f"Could not backup {path}: {e}")
