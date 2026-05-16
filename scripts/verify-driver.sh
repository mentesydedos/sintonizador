#!/usr/bin/env bash
# verify-driver.sh — Read-only sanity check after driver install
# Safe to run anytime, no sudo needed.

set -uo pipefail

PASS=0
FAIL=0
ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "  [info] $*"; }

echo "== PCI =="
if lspci -nn | grep -qi "TBS Technologies"; then
  ok "TBS card detected on PCI bus"
  lspci -nn | grep -i "TBS Technologies" | sed 's/^/         /'
else
  bad "no TBS card on PCI"
fi

echo
echo "== Kernel modules =="
# Driver real para el TBS6704: tbsecp3 (bridge PCI) + lgdt3306a (demod ATSC).
# El resto son módulos de otras cards TBS — útiles si la máquina tiene más de una.
if lsmod | awk '{print $1}' | grep -qx "tbsecp3"; then
  ok "tbsecp3 loaded (TBS6704 bridge driver)"
else
  bad "tbsecp3 NOT loaded — corré 'sudo modprobe tbsecp3' o reinstalá con scripts/build-tbs-clean.sh"
fi
for m in lgdt3306a si2168 cx24117 tas2101 av201x m88ds3103; do
  if lsmod | awk '{print $1}' | grep -qx "$m"; then
    info "$m loaded"
  fi
done

echo
echo "== /dev/dvb =="
if [[ -d /dev/dvb ]]; then
  ok "/dev/dvb exists"
  for a in /dev/dvb/adapter*; do
    [[ -d "$a" ]] || continue
    info "$(basename "$a"): $(ls "$a" | tr '\n' ' ')"
  done
else
  bad "/dev/dvb missing"
fi

echo
echo "== Frontend capabilities =="
if command -v dvb-fe-tool >/dev/null; then
  for a in /dev/dvb/adapter*; do
    [[ -d "$a" ]] || continue
    n="${a##*adapter}"
    out=$(dvb-fe-tool -a "$n" 2>&1 || true)
    name=$(echo "$out" | grep -i "frontend.*name" | head -1)
    delsys=$(echo "$out" | grep -i "delivery system" | head -1)
    if echo "$out" | grep -qi "ATSC"; then
      ok "adapter$n: $name ${delsys}"
    else
      info "adapter$n: $name ${delsys}"
    fi
  done
else
  bad "dvb-fe-tool not installed (apt install dvb-tools)"
fi

echo
echo "== Permissions =="
if id -nG | tr ' ' '\n' | grep -qx video; then
  ok "user $(whoami) is in 'video' group"
else
  bad "user $(whoami) NOT in 'video' group — re-login or run 'newgrp video'"
fi

if [[ -e /dev/dvb/adapter0/frontend0 ]]; then
  if [[ -r /dev/dvb/adapter0/frontend0 ]]; then
    ok "/dev/dvb/adapter0/frontend0 readable"
  else
    bad "/dev/dvb/adapter0/frontend0 NOT readable by $(whoami)"
  fi
fi

echo
echo "== Summary =="
echo "  pass: $PASS"
echo "  fail: $FAIL"
exit $((FAIL > 0 ? 1 : 0))
