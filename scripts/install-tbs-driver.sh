#!/usr/bin/env bash
# install-tbs-driver.sh — Build and install TBS open-source drivers for TBS6704
# Run with: sudo bash scripts/install-tbs-driver.sh
#
# Phases:
#   1. preflight checks
#   2. apt install build dependencies
#   3. clone tbsdtv/media_build + tbsdtv/linux_media into ./build/
#   4. configure & compile out-of-tree media stack (RISKY on kernel 6.17)
#   5. install kernel modules to /lib/modules/$(uname -r)/extra/
#   6. install TBS firmware tarball to /lib/firmware/
#   7. udev rule + dvb group + add invoking user to video group
#   8. depmod + modprobe + verify /dev/dvb/adapter*
#
# Idempotent: re-running re-clones/pulls and rebuilds. Logs to logs/install.log.

set -euo pipefail

# ---- locate project root regardless of CWD ----
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build"
LOG_DIR="$PROJECT_ROOT/logs"
LOG="$LOG_DIR/install.log"
mkdir -p "$BUILD_DIR" "$LOG_DIR"

# ---- the user who invoked sudo (so we chown their stuff back) ----
INVOKING_USER="${SUDO_USER:-$USER}"
INVOKING_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"

# ---- helpers ----
log()  { printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
die()  { log "FATAL: $*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

step() { echo; log "===== $* ====="; }

# ---- 1. preflight ----
step "Phase 1: preflight"
[[ $EUID -eq 0 ]] || die "must run as root (use sudo)"
[[ -n "$INVOKING_USER" && "$INVOKING_USER" != "root" ]] || die "could not determine invoking non-root user (run via sudo, not as root directly)"

KVER="$(uname -r)"
log "kernel: $KVER"
log "user: $INVOKING_USER (home: $INVOKING_HOME)"
[[ -d "/lib/modules/$KVER/build" ]] || die "kernel headers missing for $KVER (install linux-headers-$KVER)"

if lspci -nn | grep -qi "TBS Technologies"; then
  log "TBS card detected on PCI bus"
else
  log "WARNING: no TBS card on PCI bus — continuing anyway"
fi

if [[ -d /dev/dvb ]]; then
  log "WARNING: /dev/dvb already exists; another driver may be loaded"
  ls /dev/dvb/ | tee -a "$LOG"
fi

need lspci
need git
need make
need gcc

# ---- 2. apt deps ----
step "Phase 2: apt install build dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq | tee -a "$LOG"
apt-get install -y --no-install-recommends \
  build-essential \
  "linux-headers-$KVER" \
  git \
  patchutils \
  libproc-processtable-perl \
  flex bison \
  libssl-dev libelf-dev \
  bc \
  wget \
  dvb-tools \
  v4l-utils \
  w-scan 2>&1 | tee -a "$LOG"

# ---- 3. clone ----
step "Phase 3: clone TBS sources"
cd "$BUILD_DIR"

if [[ -d media_build/.git ]]; then
  log "media_build already cloned; pulling"
  git -C media_build pull --ff-only 2>&1 | tee -a "$LOG"
else
  git clone --depth=10 https://github.com/tbsdtv/media_build.git 2>&1 | tee -a "$LOG"
fi

if [[ -d media/.git ]]; then
  log "media (linux_media) already cloned; pulling"
  git -C media pull --ff-only 2>&1 | tee -a "$LOG"
else
  git clone --depth=10 -b latest https://github.com/tbsdtv/linux_media.git media 2>&1 | tee -a "$LOG"
fi

# fix ownership so user can rebuild without sudo later
chown -R "$INVOKING_USER":"$INVOKING_USER" "$BUILD_DIR"

# ---- 4. configure & build ----
step "Phase 4: configure and build (this can take 5–15 min)"
cd "$BUILD_DIR/media_build"

# run as invoking user to avoid root-owned build artifacts
sudo -u "$INVOKING_USER" make dir DIR=../media 2>&1 | tee -a "$LOG"
sudo -u "$INVOKING_USER" make distclean 2>&1 | tee -a "$LOG" || true
sudo -u "$INVOKING_USER" make allyesconfig 2>&1 | tee -a "$LOG"

# disable IR/RC subsystems we don't need (faster build, fewer dep issues)
sudo -u "$INVOKING_USER" sed -i -r 's/(^CONFIG.*_RC.*=).*/\1n/g' v4l/.config
sudo -u "$INVOKING_USER" sed -i -r 's/(^CONFIG.*_IR.*=).*/\1n/g' v4l/.config

JOBS="$(nproc)"
log "compiling with -j$JOBS"
if ! sudo -u "$INVOKING_USER" make -j"$JOBS" 2>&1 | tee -a "$LOG"; then
  die "BUILD FAILED on kernel $KVER. Common cause: kernel too new for tbsdtv driver (officially supports up to 6.14). See logs/install.log. Aborting before install."
fi

# ---- 5. install modules ----
step "Phase 5: install modules"
make install 2>&1 | tee -a "$LOG"
depmod -a 2>&1 | tee -a "$LOG"

# ---- 6. firmware ----
step "Phase 6: install TBS firmware"
FW_TAR="$BUILD_DIR/tbs-tuner-firmwares_v1.0.tar.bz2"
if [[ ! -f "$FW_TAR" ]]; then
  wget -O "$FW_TAR" http://www.tbsdtv.com/download/document/linux/tbs-tuner-firmwares_v1.0.tar.bz2 2>&1 | tee -a "$LOG"
fi
tar -tjf "$FW_TAR" >/dev/null 2>&1 || die "firmware tarball is corrupted; delete $FW_TAR and re-run"
tar jxvf "$FW_TAR" -C /lib/firmware/ 2>&1 | tee -a "$LOG"

# ---- 7. udev + group ----
step "Phase 7: udev rule + group membership"
cat >/etc/udev/rules.d/99-tbs-dvb.rules <<'EOF'
# TBS DVB devices: owned by root:video, group rw — works with VIDEO group membership
SUBSYSTEM=="dvb", GROUP="video", MODE="0660"
EOF
log "wrote /etc/udev/rules.d/99-tbs-dvb.rules"

usermod -a -G video "$INVOKING_USER"
log "added $INVOKING_USER to video group (may require re-login to take effect)"

udevadm control --reload-rules
udevadm trigger --subsystem-match=dvb || true

# ---- 8. modprobe + verify ----
step "Phase 8: load modules and verify"
# Try several module names; saa716x_tbs_dvb is the bridge for TBS6704
for mod in saa716x_tbs_dvb tbs6704 cx23885; do
  modprobe -v "$mod" 2>&1 | tee -a "$LOG" || true
done

sleep 2
if [[ -d /dev/dvb ]]; then
  log "SUCCESS: /dev/dvb exists"
  ls -la /dev/dvb/ | tee -a "$LOG"
  for adapter in /dev/dvb/adapter*; do
    [[ -d "$adapter" ]] || continue
    log "--- $adapter ---"
    ls -la "$adapter" | tee -a "$LOG"
  done
else
  log "WARNING: /dev/dvb still does not exist after modprobe. Check 'dmesg | tail -50' for errors."
  dmesg | tail -30 | tee -a "$LOG"
  die "driver loaded but no devices appeared"
fi

step "DONE"
log "Next steps:"
log "  1. Re-login (or 'newgrp video') so $INVOKING_USER can read /dev/dvb/* without sudo"
log "  2. Run scripts/verify-driver.sh to double-check"
log "  3. Run scripts/scan-channels.sh to discover ATSC channels"
