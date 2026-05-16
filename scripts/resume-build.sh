#!/usr/bin/env bash
# resume-build.sh — Retry build con dos correcciones aplicadas:
#   (a) Patch fuente videodev2.h (#include <linux/time_types.h>) en build/media/
#       El sync de media_build copia desde ahí, así sobrevive a apply_patches.
#   (b) Trim agresivo de v4l/.config: solo dejar el camino TBS6704
#       (MEDIA_DIGITAL_TV + MEDIA_PCI + SAA716X + LGDT3306A) y desactivar
#       todo lo demás (analog TV, cámaras, USB, SDR, radio, plataformas).
#
# Run with: sudo bash scripts/resume-build.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/build/media_build"
MEDIA_SRC="$PROJECT_ROOT/build/media"
LOG_DIR="$PROJECT_ROOT/logs"
LOG="$LOG_DIR/resume-build.log"
mkdir -p "$LOG_DIR"

[[ $EUID -eq 0 ]] || { echo "must run as root"; exit 1; }
[[ -d "$BUILD_DIR" ]] || { echo "no build at $BUILD_DIR — corre install-tbs-driver.sh primero"; exit 1; }
[[ -d "$MEDIA_SRC" ]] || { echo "no media tree at $MEDIA_SRC"; exit 1; }

INVOKING_USER="${SUDO_USER:-$USER}"
KVER="$(uname -r)"
JOBS="$(nproc)"

log()  { printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
step() { echo; log "===== $* ====="; }

# ---------- (a) parchear fuente videodev2.h ----------
step "Patch fuente: añadir <linux/time_types.h> a videodev2.h"
SRC_HDR="$MEDIA_SRC/include/uapi/linux/videodev2.h"
if grep -q "linux/time_types.h" "$SRC_HDR"; then
  log "ya parcheado"
else
  # Insertar después del bloque #include <linux/types.h>
  sudo -u "$INVOKING_USER" sed -i '/^#include <linux\/types.h>$/a\
#ifdef __KERNEL__\
#include <linux/time_types.h>\t/* struct __kernel_timespec — kernel >= 6.x */\
#endif' "$SRC_HDR"
  log "parche aplicado a $SRC_HDR"
  grep -n "time_types.h" "$SRC_HDR" | tee -a "$LOG"
fi

# ---------- (b) trim agresivo de .config ----------
step "Trim v4l/.config: deshabilitar todo lo no-DVB-PCI"
CFG="$BUILD_DIR/v4l/.config"
[[ -f "$CFG" ]] || { log "FATAL: $CFG no existe — algo borró el config"; exit 1; }

# Backup primero
cp "$CFG" "$CFG.before-trim-$(date +%s)"

# Toggles de alto nivel que cortan subtrees enteros
disable_top_level() {
  local key="$1"
  if grep -q "^$key=y" "$CFG" || grep -q "^$key=m" "$CFG"; then
    sed -i "s|^$key=[ym]$|# $key is not set|" "$CFG"
    log "  disabled $key"
  fi
}
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
  disable_top_level "$k"
done

# Barrido amplio: TODA entrada CONFIG_VIDEO_* y CONFIG_DVB_USB_* se apaga.
# La TBS6704 es DVB-PCIe, no necesita ningún driver V4L2 capture (CONFIG_VIDEO_*)
# ni ningún tuner USB (CONFIG_DVB_USB_*).
log "barriendo CONFIG_VIDEO_*=m → off"
sed -i -E 's|^(CONFIG_VIDEO_[A-Z0-9_]+)=[ym]$|# \1 is not set|' "$CFG"
log "barriendo CONFIG_DVB_USB_*=m → off"
sed -i -E 's|^(CONFIG_DVB_USB_[A-Z0-9_]+)=[ym]$|# \1 is not set|' "$CFG"

# Otros bridges/tuners PCI que no son TBS6704
for k in \
  CONFIG_DVB_FIREDTV \
  CONFIG_DVB_PLATFORM_DRIVERS \
  CONFIG_DVB_BT8XX \
  CONFIG_DVB_AV7110 \
  CONFIG_DVB_BUDGET_CORE \
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
  disable_top_level "$k"
done

# --- Blanket disable + whitelist re-enable ---
# Apago TODA entrada CONFIG_*=m. Después rehabilito únicamente el camino TBS6704.
# Esto es la única forma de evitar el campo de minas de drivers DVB/V4L de otros
# fabricantes que tienen redefinitions, BIN_ATTR_RO, etc. en kernel 6.17.
log "blanket disable: TODO CONFIG_*=m → off (lo re-encendemos selectivo después)"
sed -i -E 's|^(CONFIG_[A-Z0-9_]+)=m$|# \1 is not set|' "$CFG"

log "whitelist re-enable (solo lo que TBS6704 ATSC necesita):"
WHITELIST=(
  DVB_CORE
  DVB_NET
  DVB_LGDT3306A
  SAA716X_CORE
  DVB_SAA716X_TBS
  DVB_SAA716X_HYBRID
  VIDEOBUF2_CORE
  VIDEOBUF2_VMALLOC
  VIDEOBUF2_DMA_CONTIG
  VIDEOBUF2_DMA_SG
  VIDEOBUF2_MEMOPS
  MEDIA_SUPPORT
)
for k in "${WHITELIST[@]}"; do
  if sed -i "s|^# CONFIG_${k} is not set$|CONFIG_${k}=m|" "$CFG" && \
     grep -q "^CONFIG_${k}=m$" "$CFG"; then
    log "  re-enabled CONFIG_${k}=m"
  else
    log "  NOTE: CONFIG_${k} no se pudo re-habilitar (puede ser =y bool, OK)"
  fi
done

log "Resumen post-trim:"
log "  CONFIG_*=m activos: $(grep -c '^CONFIG_.*=m$' "$CFG")"
log "  CONFIG_*=y activos: $(grep -c '^CONFIG_.*=y$' "$CFG")"

# Asegurar que SI están habilitados los que TBS6704 sí necesita
ensure_module() {
  local key="$1"
  if grep -q "^# $key is not set" "$CFG"; then
    sed -i "s|^# $key is not set|$key=m|" "$CFG"
    log "  re-enabled $key=m"
  fi
}
ensure_module CONFIG_DVB_CORE
ensure_module CONFIG_SAA716X_CORE
ensure_module CONFIG_DVB_SAA716X_TBS
ensure_module CONFIG_DVB_LGDT3306A

step "Limpiar artefactos rotos previos (.tmp_versions, etc.)"
cd "$BUILD_DIR/v4l"
sudo -u "$INVOKING_USER" find . -name "*.o" -delete 2>/dev/null || true
sudo -u "$INVOKING_USER" find . -name "*.o.cmd" -delete 2>/dev/null || true
sudo -u "$INVOKING_USER" rm -rf .tmp_versions 2>/dev/null || true

step "make -j$JOBS (con config recortado)"
cd "$BUILD_DIR"
if ! sudo -u "$INVOKING_USER" make -j"$JOBS" 2>&1 | tee -a "$LOG"; then
  log "BUILD FAILED. Pega últimas 60 líneas de $LOG"
  exit 1
fi

step "make install"
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
for mod in saa716x_tbs_dvb tbs6704 saa716x_core; do
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
