#!/usr/bin/env sh
# Unduh library frontend dengan versi di-pin ke app/static/js/vendor/ (commit hasilnya ke repo).
# Alpine memakai build CSP agar CSP bisa tanpa 'unsafe-eval'.
# Checksum SHA-256 dicek: file dari CDN yang berubah/diganti tidak ikut masuk repo.
# Naik versi: ubah VERSION, jalankan dengan UPDATE_CHECKSUMS=1, review diff-nya, lalu salin hash baru ke sini.
set -eu
HTMX_VERSION=2.0.4
HTMX_SHA256=e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447
ALPINE_VERSION=3.14.8
ALPINE_SHA256=c98f46291087f0f93c9cdefb682d120fe1d2b3dcb649de83cfc1ad5b1fac58e4
DEST="$(dirname "$0")/../app/static/js/vendor"
mkdir -p "$DEST"

fetch() {   # url, file, sha256
    curl -fsSL "$1" -o "$DEST/$2.tmp"
    actual=$(sha256sum "$DEST/$2.tmp" | cut -d' ' -f1)
    if [ "${UPDATE_CHECKSUMS:-0}" = 1 ]; then
        echo "$2 sha256: $actual"
    elif [ "$actual" != "$3" ]; then
        rm -f "$DEST/$2.tmp"
        echo "CHECKSUM TIDAK COCOK: $2 (diharapkan $3, didapat $actual)" >&2
        exit 1
    fi
    mv "$DEST/$2.tmp" "$DEST/$2"
}

fetch "https://cdn.jsdelivr.net/npm/htmx.org@${HTMX_VERSION}/dist/htmx.min.js" htmx.min.js "$HTMX_SHA256"
fetch "https://cdn.jsdelivr.net/npm/@alpinejs/csp@${ALPINE_VERSION}/dist/cdn.min.js" alpine-csp.min.js "$ALPINE_SHA256"
echo "htmx ${HTMX_VERSION}, alpine-csp ${ALPINE_VERSION} -> $DEST"
