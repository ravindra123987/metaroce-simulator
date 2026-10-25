#!/usr/bin/env bash
# Build libsoftmetaroce + rdma_gen with Meta's getdeps, inside ubuntu:24.04.
#
# Runs in a throwaway container with /w mounted from the host, so the
# getdeps scratch tree (sources, objects, installed deps) survives a
# failed or interrupted build and the next run resumes where it stopped.
#
#   /w/src      metaroce checkout at $METAROCE_SHA
#   /w/scratch  getdeps scratch path
#   /w/out      final install tree: bin/ + lib/ with rpaths fixed up
set -euo pipefail

: "${METAROCE_SHA:?set METAROCE_SHA}"
JOBS="${JOBS:-2}"

export DEBIAN_FRONTEND=noninteractive
# getdeps runs `pip install pex`; Ubuntu 24.04 marks the system Python as
# externally managed, so allow it inside this throwaway build container.
export PIP_BREAK_SYSTEM_PACKAGES=1
if ! command -v git >/dev/null || ! command -v pip >/dev/null; then
  apt-get update -q
  apt-get install -yq --no-install-recommends git python3 python3-pip sudo ca-certificates build-essential
fi

if [ ! -d /w/src/.git ]; then
  git clone -q https://github.com/facebookincubator/metaroce.git /w/src
fi
cd /w/src
git fetch -q origin "$METAROCE_SHA" && git checkout -q "$METAROCE_SHA"

# Seed downloads whose upstream host is unreliable from a mirror. getdeps
# checks each file against the sha256 pinned in its manifest, so a bad
# mirror copy fails loudly rather than building the wrong thing.
seed() { # <getdeps download name> <mirror url>
  local dst="/w/scratch/downloads/$1"
  [ -s "$dst" ] && return 0
  mkdir -p /w/scratch/downloads
  curl -sSLf --retry 3 -o "$dst.tmp" "$2" && mv "$dst.tmp" "$dst"
}
command -v curl >/dev/null || apt-get install -yq --no-install-recommends curl
# tukaani.org refused connections on 2026-10-15.
seed xz-xz-5.2.5.tar.gz https://downloads.sourceforge.net/project/lzmautils/xz-5.2.5.tar.gz

GETDEPS="python3 build/fbcode_builder/getdeps.py --allow-system-packages --scratch-path /w/scratch"

$GETDEPS install-system-deps --recursive rdma_gen
$GETDEPS install-system-deps --recursive patchelf
$GETDEPS build --no-tests --num-jobs "$JOBS" --src-dir . rdma_gen
$GETDEPS fixup-dyn-deps --src-dir . rdma_gen /w/out

echo "BUILD OK: $(git rev-parse HEAD)"
find /w/out -maxdepth 2 -type f -perm -u+x | head -20
