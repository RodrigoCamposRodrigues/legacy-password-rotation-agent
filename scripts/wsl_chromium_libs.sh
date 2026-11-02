#!/usr/bin/env bash
# Chromium needs libgbm, which minimal WSL images lack. Without sudo, unpack it under .tools/ instead;
# the browser code finds it there. With sudo, `sudo .venv/bin/playwright install-deps chromium` is simpler.
set -euo pipefail
mkdir -p .tools/debs .tools/libroot
cd .tools/debs
apt-get download libgbm1 libwayland-server0
for deb in *.deb; do dpkg-deb -x "$deb" ../libroot; done
echo "Unpacked to .tools/libroot/usr/lib/x86_64-linux-gnu"
