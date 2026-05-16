#!/usr/bin/env bash
# build-tbs-clean.sh — Compilación limpia post-downgrade a kernel 6.8.
#
# Estrategia: allyesconfig + apagar SOLO los subárboles que rompen contra el
# kernel 6.8 (cámaras, V4L capture, USB tuners, RC, IR, radio, SDR, analog TV).
# Mantener TODOS los demods DVB encendidos para que saa716x_budget.c —
# que referencia cx24117/tas2101/etc. — compile sin implicit-declaration.
#
# Run with: sudo bash scripts/build-tbs-clean.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/media_build"
MEDIA_SRC="$PROJECT_ROOT/build/media"
LOG_DIR="$PROJECT_ROOT/logs"
LOG="$LOG_DIR/build-clean.log"
mkdir -p "$LOG_DIR"

[[ $EUID -eq 0 ]] || { echo "must run as root (use sudo)"; exit 1; }
[[ -d "$BUILD_DIR" ]] || { echo "no build at $BUILD_DIR"; exit 1; }
[[ -d "$MEDIA_SRC" ]] || { echo "no media tree at $MEDIA_SRC"; exit 1; }

INVOKING_USER="${SUDO_USER:-$USER}"
KVER="$(uname -r)"
JOBS="$(nproc)"

log()  { printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
step() { echo; log "===== $* ====="; }

log "kernel: $KVER  user: $INVOKING_USER  jobs: $JOBS"

# ---------- Patch defensivo: videodev2.h ----------
step "Patch fuente: <linux/time_types.h> en videodev2.h (idempotente)"
SRC_HDR="$MEDIA_SRC/include/uapi/linux/videodev2.h"
if grep -q "linux/time_types.h" "$SRC_HDR"; then
  log "ya parcheado"
else
  sudo -u "$INVOKING_USER" sed -i '/^#include <linux\/types.h>$/a\
#ifdef __KERNEL__\
#include <linux/time_types.h>\
#endif' "$SRC_HDR"
  log "parche aplicado"
fi

# ---------- Reset de árbol de build + regen .config ----------
step "make distclean + make dir DIR=../media (resync fuentes)"
cd "$BUILD_DIR"
sudo -u "$INVOKING_USER" make distclean 2>&1 | tee -a "$LOG" || true
sudo -u "$INVOKING_USER" make dir DIR=../media 2>&1 | tee -a "$LOG"

step "make allyesconfig (regenera contra $KVER, partiendo de cero)"
sudo -u "$INVOKING_USER" make allyesconfig 2>&1 | tee -a "$LOG"

# ---------- Apagar SOLO lo que rompe en 6.8 ----------
step "Apagar subárboles que no compilan en kernel 6.8"
CFG="$BUILD_DIR/v4l/.config"
[[ -f "$CFG" ]] || { log "FATAL: $CFG no existe tras allyesconfig"; exit 1; }

cp "$CFG" "$CFG.before-disables-$(date +%s)"

disable_top() {
  local key="$1"
  if grep -q "^${key}=[ym]" "$CFG"; then
    sed -i "s|^${key}=[ym]$|# ${key} is not set|" "$CFG"
    log "  disabled $key"
  fi
}

# Toggles de alto nivel (cortan subtrees enteros)
for k in \
  CONFIG_MEDIA_ANALOG_TV_SUPPORT \
  CONFIG_MEDIA_CAMERA_SUPPORT \
  CONFIG_MEDIA_RADIO_SUPPORT \
  CONFIG_MEDIA_SDR_SUPPORT \
  CONFIG_MEDIA_TEST_SUPPORT \
  CONFIG_MEDIA_PLATFORM_SUPPORT \
  CONFIG_MEDIA_USB_SUPPORT \
  CONFIG_VIDEO_DEV \
  CONFIG_RC_CORE \
  CONFIG_USB_VIDEO_CLASS \
  ; do
  disable_top "$k"
done

# Barrido de drivers V4L (cámaras individuales — ccs, hi556, etc.)
log "barriendo CONFIG_VIDEO_*=m → off"
sed -i -E 's|^(CONFIG_VIDEO_[A-Z0-9_]+)=[ym]$|# \1 is not set|' "$CFG"

# Tuners USB (defensivo — MEDIA_USB_SUPPORT off ya los corta)
log "barriendo CONFIG_DVB_USB_*=m → off"
sed -i -E 's|^(CONFIG_DVB_USB_[A-Z0-9_]+)=[ym]$|# \1 is not set|' "$CFG"

# RC / IR (control remoto)
log "barriendo *_RC_* y *_IR_* → off"
sed -i -E 's|^(CONFIG[A-Z0-9_]*_RC_[A-Z0-9_]+)=[ym]$|# \1 is not set|' "$CFG"
sed -i -E 's|^(CONFIG[A-Z0-9_]*_IR_[A-Z0-9_]+)=[ym]$|# \1 is not set|' "$CFG"

# Otros bridges PCI no-TBS (no rompen el build, pero ahorran tiempo)
for k in \
  CONFIG_DVB_FIREDTV \
  CONFIG_DVB_BT8XX \
  CONFIG_DVB_AV7110 \
  CONFIG_DVB_NGENE \
  CONFIG_DVB_DDBRIDGE \
  CONFIG_DVB_PT1 \
  CONFIG_DVB_PT3 \
  CONFIG_DVB_SMIPCIE \
  CONFIG_DVB_NETUP_UNIDVB \
  CONFIG_DVB_PLUTO2 \
  CONFIG_DVB_DM1105 \
  CONFIG_DVB_MANTIS \
  CONFIG_DVB_HOPPER \
  CONFIG_DVB_DUMMY_FE \
  CONFIG_DVB_AS102 \
  ; do
  disable_top "$k"
done

log "Resumen:"
log "  CONFIG_*=m activos: $(grep -c '^CONFIG_.*=m$' "$CFG")"
log "  CONFIG_*=y activos: $(grep -c '^CONFIG_.*=y$' "$CFG")"

# ---------- Build ----------
step "make -j$JOBS"
if ! sudo -u "$INVOKING_USER" make -j"$JOBS" 2>&1 | tee -a "$LOG"; then
  log "BUILD FAILED. Pega últimas 80 líneas de $LOG"
  exit 1
fi

# ---------- Install + firmware + udev ----------
step "make install + depmod"
make install 2>&1 | tee -a "$LOG"
depmod -a 2>&1 | tee -a "$LOG"

step "firmware"
FW_TAR="$PROJECT_ROOT/build/tbs-tuner-firmwares_v1.0.tar.bz2"
if [[ ! -f "$FW_TAR" ]]; then
  wget -O "$FW_TAR" http://www.tbsdtv.com/download/document/linux/tbs-tuner-firmwares_v1.0.tar.bz2 2>&1 | tee -a "$LOG"
fi
tar jxvf "$FW_TAR" -C /lib/firmware/ 2>&1 | tee -a "$LOG"

step "udev + grupo video"
cat >/etc/udev/rules.d/99-tbs-dvb.rules <<'EOF'
SUBSYSTEM=="dvb", GROUP="video", MODE="0660"
EOF
usermod -a -G video "$INVOKING_USER" 2>&1 | tee -a "$LOG" || true
udevadm control --reload-rules
udevadm trigger --subsystem-match=dvb || true

step "modprobe + verificación"
# El TBS6704 usa el driver tbsecp3 (familia ECP3 FPGA-based, vendor 544D).
# NO usa saa716x (vendor 1131 Philips). Verificar con:
#   modprobe --resolve-alias "pci:v0000544Dd00006178sv00006704sd00000001bc04sc80i00"
for mod in tbsecp3; do
  modprobe -v "$mod" 2>&1 | tee -a "$LOG" || true
done
sleep 2

if [[ -d /dev/dvb ]]; then
  log "SUCCESS: /dev/dvb existe"
  ls -la /dev/dvb/ | tee -a "$LOG"
  for a in /dev/dvb/adapter*; do
    [[ -d "$a" ]] || continue
    log "--- $a ---"
    ls -la "$a" | tee -a "$LOG"
  done
else
  log "Driver instaló pero /dev/dvb no aparece. dmesg:"
  dmesg | tail -40 | tee -a "$LOG"
  exit 2
fi

step "DONE"
log "Re-login (o 'newgrp video') y luego: bash scripts/verify-driver.sh"
