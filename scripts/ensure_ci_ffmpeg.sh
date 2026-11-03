#!/usr/bin/env bash
# Limit package-index refresh to Ubuntu, and retry with a different official
# mirror rather than repeating the same stalled download on an Azure mirror.
set -euo pipefail

if ffmpeg -version >/dev/null 2>&1 && ffprobe -version >/dev/null 2>&1; then
  ffmpeg -version
  exit 0
fi

source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_CODENAME" != noble ]]; then
  echo "CI ffmpeg provisioning requires Ubuntu 24.04" >&2
  exit 1
fi

sources=$(mktemp "${RUNNER_TEMP:-/tmp}/ci-ubuntu-XXXXXX.sources")
trap 'rm -f "$sources"' EXIT
apt_options=(
  -o "Dir::Etc::sourcelist=$sources"
  -o Dir::Etc::sourceparts=-
  -o APT::Get::List-Cleanup=0
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=15
  -o Acquire::https::Timeout=15
  -o DPkg::Lock::Timeout=15
)

for mirror in archive.ubuntu.com azure.archive.ubuntu.com; do
  cat > "$sources" <<EOF
Types: deb
URIs: http://$mirror/ubuntu
Suites: noble noble-updates noble-security
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
  echo "Installing ffmpeg from $mirror"
  if sudo timeout --kill-after=5s 30s apt-get "${apt_options[@]}" update \
      && sudo timeout --kill-after=5s 70s apt-get "${apt_options[@]}" \
        install -y --no-install-recommends ffmpeg \
      && ffmpeg -version >/dev/null 2>&1 && ffprobe -version >/dev/null 2>&1; then
    ffmpeg -version
    exit 0
  fi
done

echo "ffmpeg provisioning failed on both official Ubuntu mirrors" >&2
exit 1
