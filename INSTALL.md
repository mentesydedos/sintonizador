# INSTALL — Sintonizador TBS6704

Ruta feliz de instalación desde cero en una máquina nueva.
Para el detalle de qué se probó, los callejones sin salida y por qué este es el camino que funciona, ver [HISTORICO.md](HISTORICO.md).

## Requisitos

- **Hardware:** TBS6704 PCIe (Quad ATSC/Clear-QAM). PCI ID `544d:6178`, subsys `6704:0001`.
- **SO:** Ubuntu 24.04 (probado en 24.04.4).
- **Kernel:** un kernel del rango 4.19–6.14. El driver `tbsdtv/linux_media` **no compila** contra 6.15+ (cambios en `pm_runtime_get_if_active`, `pci_enable_msix`, `V4L2_VERSION`, etc.). Recomendado: `6.8.0-107-generic` (Ubuntu 24.04 stock).
- **PCIe slot** libre.
- **Antena UHF/VHF** conectada al puerto F-type de la tarjeta (o cable QAM).

## 1. Verificar kernel

```bash
uname -r
```

Debe devolver algo entre `4.19.x` y `6.14.x`. Si estás en un kernel OEM más nuevo (p. ej. `6.17.0-1020-oem`):

```bash
# Instalar el genérico stock de Ubuntu 24.04 y reiniciar eligiendo esa entrada en GRUB
sudo apt install linux-image-generic linux-headers-generic
sudo update-grub
# reiniciar y en GRUB → Advanced options → elegir 6.8.0-107-generic
```

## 2. Verificar que la tarjeta está visible en PCI

```bash
lspci -nn | grep -i tbs
# Esperás: 02:00.0 ... [544d:6178] ... TBS Technologies DVB Tuner PCIe Card
```

Si no aparece: la tarjeta no está bien insertada en el slot o la fuente no le da PCIe. No tiene sentido seguir hasta resolver eso.

## 3. Clonar este repo

```bash
sudo mkdir /home/sintonizador
sudo chown $USER:$USER /home/sintonizador
cd /home/sintonizador
git clone <url-del-repo> .   # o copiar los scripts/ y src/ a mano
```

## 4. Instalar build deps y clonar fuentes upstream

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  linux-headers-$(uname -r) \
  git patchutils libproc-processtable-perl \
  flex bison libssl-dev libelf-dev bc wget \
  dvb-tools dtv-scan-tables v4l-utils w-scan

mkdir -p build && cd build
git clone --depth=10 https://github.com/tbsdtv/media_build.git
git clone --depth=10 -b latest https://github.com/tbsdtv/linux_media.git media
cd ..
```

## 5. Compilar e instalar el driver

```bash
sudo bash scripts/build-tbs-clean.sh
```

Qué hace, en orden:

1. Parche idempotente a `media/include/uapi/linux/videodev2.h` para incluir `<linux/time_types.h>` bajo `#ifdef __KERNEL__`.
2. `make distclean` + `make dir DIR=../media` + `make allyesconfig` (regenera `.config` contra el kernel actual).
3. Apaga **solo** los subárboles que rompen contra kernel 6.8:
   - `CONFIG_MEDIA_CAMERA_SUPPORT`, `CONFIG_VIDEO_*=m` → cámaras (rompe ccs-core.c en 6.8)
   - `CONFIG_MEDIA_USB_SUPPORT`, `CONFIG_DVB_USB_*=m` → tuners USB
   - `CONFIG_RC_CORE`, `*_RC_*`, `*_IR_*` → remoto e infrarrojo
   - `CONFIG_MEDIA_ANALOG_TV_SUPPORT`, `RADIO`, `SDR`, `TEST`, `PLATFORM` → no aplican
   - Otros bridges PCI no-TBS (DDBRIDGE, NGENE, etc.) por velocidad de build
4. Mantiene **todos** los demods DVB encendidos (cx24117, tas2101, m88ds3103, lgdt3306a, etc.) — son necesarios para que `saa716x_budget.c` compile aunque para 6704 solo usemos lgdt3306a.
5. `make -j$(nproc)` → 5–15 min.
6. `make install` + `depmod -a`.
7. Descarga y extrae el tarball de firmwares (`tbs-tuner-firmwares_v1.0.tar.bz2`) en `/lib/firmware/`.
8. Crea udev rule `99-tbs-dvb.rules` (group video, mode 0660).
9. Agrega el usuario invocador al grupo `video`.
10. `modprobe tbsecp3` y verifica `/dev/dvb/adapter[0-3]`.

## 6. Verificar

```bash
ls /dev/dvb/
# Esperás: adapter0 adapter1 adapter2 adapter3

dmesg | grep -i tbsecp3
# Esperás algo como:
#   TBSECP3 driver 0000:02:00.0: TurboSight TBS 6704(Quad ATSC/QAMB)
#   DVB: registering adapter 0 frontend 0 ...
#   ... adapter 1, 2, 3 ...
```

Y para acceder a los devices sin sudo en la misma sesión:

```bash
newgrp video
```

(En sesiones futuras esto ya está aplicado.)

## 7. Test rápido de un tuner (opcional)

```bash
# Scan completo de adapter 0
bash scripts/scan-channels.sh 0
# Output: ./channels.conf en formato DVBv5

# Sintonizar un canal específico
dvbv5-zap -a 0 -c channels.conf "<channel-name>"
```

Si `scan-channels.sh` se queja con `ATSC frequency table not found`, falta el paquete `dtv-scan-tables` (verificalo en el paso 4).

## Persistencia

- El módulo `tbsecp3` se autocarga en boot vía `modules.alias` (lo genera `depmod -a` que el script ya corrió).
- Los `/dev/dvb/adapter*` aparecen automáticamente cuando el kernel termina de inicializar PCI.
- Si actualizás el kernel a una versión que sigue en el rango soportado (4.19–6.14), hay que recompilar el driver para esa versión: `sudo bash scripts/build-tbs-clean.sh` de nuevo.
- Si actualizás a kernel >6.14, **no va a compilar**. Hay que pinear el kernel o esperar a que tbsdtv suba soporte upstream.

## Si algo falla

| Síntoma | Causa probable | Mirar |
|---|---|---|
| Build muere en `ccs-core.c` | Kernel >6.14 o se está usando allyesconfig sin trim | `uname -r`, [HISTORICO.md](HISTORICO.md) §2 |
| Build muere en `saa716x_budget.c` (`cx24117_get_i2c_adapter` undeclared) | El trim fue demasiado agresivo (whitelist apagó demods que `saa716x_budget.c` referencia) | [HISTORICO.md](HISTORICO.md) §3 |
| `tbsecp3` carga pero `/dev/dvb/` no aparece | El kernel cargó el módulo pero no hubo match PCI — chequear PCI ID con `modprobe --resolve-alias` | [HISTORICO.md](HISTORICO.md) §4 |
| `dvbv5-zap` da "no signal" | Antena, conexiones, frecuencia incorrecta para tu mercado | revisar antena/conexión física |

Logs del build: `/home/sintonizador/logs/build-clean.log`.
