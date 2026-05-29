#!/usr/bin/env bash
# ===========================================================================
# Generate a THROWAWAY development PKI for local mTLS testing.
#
#   *** DEV ONLY — DO NOT USE IN PRODUCTION ***
#
# Production uses the real SPASA PKI: drop the SPASA-issued ca.crt, crl.pem and a
# server.crt/server.key (for this proxy's hostname) into this directory instead.
# Everything this script emits (*.crt/*.key/*.pem/*.srl) is git-ignored.
#
# Produces (in this directory):
#   ca.crt / ca.key            — throwaway ECDSA P-256 CA
#   server.crt / server.key    — nginx TLS server cert (SAN: localhost, 127.0.0.1, $HOST)
#   admin.crt / admin.key      — client cert, CN=admin-dev
#   operator.crt / operator.key— client cert, CN=operator-dev
#   viewer.crt / viewer.key    — client cert, CN=viewer-dev
#   crl.pem                    — (initially empty) certificate revocation list
#
# Map the client CNs to roles by writing the app's cert_roles.json, e.g.:
#   {"cn_roles": {"admin-dev":"admin","operator-dev":"operator","viewer-dev":"viewer"},
#    "dn_roles": {}, "default_role": null}
#
# Validate the handshake once the stack is up:
#   curl --cert admin.crt --key admin.key --cacert ca.crt https://localhost/api/streams
# ===========================================================================
set -euo pipefail
cd "$(dirname "$0")"

HOST="${1:-$(hostname)}"
DAYS=825

echo "==> Generating throwaway dev CA (ECDSA P-256)"
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -x509 -new -key ca.key -sha256 -days 3650 \
    -subj "/CN=SPASA-DEV-CA/O=SPASA-DEV" -out ca.crt

gen_cert() {  # $1=name  $2=subject  $3=extfile
    local name="$1" subj="$2" ext="$3"
    openssl ecparam -name prime256v1 -genkey -noout -out "${name}.key"
    openssl req -new -key "${name}.key" -subj "${subj}" -out "${name}.csr"
    openssl x509 -req -in "${name}.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days "${DAYS}" -sha256 -extfile "${ext}" -out "${name}.crt"
    rm -f "${name}.csr"
}

echo "==> Server cert (CN=${HOST})"
cat > server.ext <<EOF
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost, DNS:${HOST}, IP:127.0.0.1
EOF
gen_cert server "/CN=${HOST}/O=SPASA-DEV" server.ext

echo "==> Client certs (admin-dev / operator-dev / viewer-dev)"
cat > client.ext <<EOF
basicConstraints = CA:FALSE
keyUsage = digitalSignature
extendedKeyUsage = clientAuth
EOF
gen_cert admin    "/CN=admin-dev/O=SPASA-DEV"    client.ext
gen_cert operator "/CN=operator-dev/O=SPASA-DEV" client.ext
gen_cert viewer   "/CN=viewer-dev/O=SPASA-DEV"   client.ext
rm -f server.ext client.ext

echo "==> Empty CRL"
# A minimal CA database so openssl can emit (and later update) a CRL.
touch index.txt
[ -f crlnumber ] || echo 1000 > crlnumber
cat > crl.cnf <<EOF
[ ca ]
default_ca = CA_dev
[ CA_dev ]
database         = index.txt
crlnumber        = crlnumber
default_md       = sha256
default_crl_days = 30
EOF
openssl ca -config crl.cnf -gencrl -keyfile ca.key -cert ca.crt -out crl.pem

echo ""
echo "Done. Files written to $(pwd)"
echo "To REVOKE a client cert later, e.g. viewer:"
echo "  openssl ca -config crl.cnf -revoke viewer.crt -keyfile ca.key -cert ca.crt"
echo "  openssl ca -config crl.cnf -gencrl -keyfile ca.key -cert ca.crt -out crl.pem"
echo "  docker compose restart proxy   # reload the CRL"
