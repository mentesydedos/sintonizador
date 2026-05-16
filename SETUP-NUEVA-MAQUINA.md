# Setup en una computadora nueva

Runbook para dejar **sintonizador** corriendo desde cero en otra máquina. Pensado para ejecutar de arriba hacia abajo. Si tenés Claude Code en la máquina destino, podés pasarle este archivo y pedirle que vaya ejecutando los pasos uno por uno; cada bloque incluye su comando de verificación.

> Para el detalle de **por qué** la receta es esta y no otra, ver [INSTALL.md](INSTALL.md) (driver) y [HISTORICO.md](HISTORICO.md) (callejones sin salida descartados). Este archivo es la versión “qué hacer”, no “por qué”.

---

## 0. Lo que tenés que tener antes de empezar

**Hardware:**
- Tarjeta **TBS6704** (Quad ATSC/Clear-QAM PCIe) instalada en un slot PCIe libre. PCI ID esperado `544d:6178`, subsystem `6704:0001`.
- **Antena UHF/VHF** conectada al puerto F-type de la tarjeta (o cable QAM).
- Recomendado: la máquina destino debe estar en la **misma área de cobertura ATSC** que la actual si querés reusar el `channels.conf`. Si está en otra ciudad, las frecuencias son distintas y hay que rescanear (paso 7).

**Software base:**
- **Ubuntu 24.04** (probado en 24.04.4 LTS).
- Acceso `sudo`.
- Conexión a internet para `apt`, `pip`, y los clones de upstream del driver.

**Restricción dura:** el driver `tbsdtv/linux_media` para esta tarjeta **solo compila contra kernel 4.19 – 6.14**. La TBS6704 usa el chip puente SAA716x, que **nunca entró al kernel mainline**, así que no hay alternativa: o bootéas un kernel ≤6.14, o la tarjeta no funciona. En Ubuntu 24.04 el genérico stock `6.8.0-107-generic` es la opción natural.

---

## 1. Bootear un kernel ≤6.14

Verificar el kernel actual:

```bash
uname -r
```

Si es ≤6.14 (ej. `6.8.x-generic`), saltá al paso 2.

Si es >6.14 (típicamente OEM, p. ej. `6.17.0-1020-oem`):

```bash
sudo apt update
sudo apt install -y linux-image-generic linux-headers-generic
sudo update-grub
```

Luego reiniciar y en el menú de GRUB → **Advanced options for Ubuntu** → seleccionar la entrada `6.8.0-107-generic` (o la versión genérica que se instaló).

Para que arranque ese kernel **siempre** sin pasar por el menú, editar `/etc/default/grub` y poner algo como:

```
GRUB_DEFAULT="Advanced options for Ubuntu>Ubuntu, with Linux 6.8.0-107-generic"
GRUB_TIMEOUT=5
```

…y volver a `sudo update-grub`. Confirmar después del reboot:

```bash
uname -r        # debe devolver 6.8.0-107-generic (o similar ≤6.14)
```

---

## 2. Confirmar que la tarjeta es visible en PCI

```bash
lspci -nn | grep -i -E 'tbs|544d'
# Esperado: 02:00.0 ... [544d:6178] ... TBS Technologies DVB Tuner PCIe Card
```

Si no aparece nada: la tarjeta no está bien conectada o el slot no le da PCIe. Resolver eso antes de seguir, no tiene sentido continuar.

---

## 3. Traer el código a `/home/sintonizador/`

El proyecto vive convencionalmente en `/home/sintonizador/` (no en el home del usuario). Crear el directorio y copiar el repo desde la máquina origen.

```bash
sudo mkdir -p /home/sintonizador
sudo chown $USER:$USER /home/sintonizador
```

Opciones para traer el código:

- **rsync desde la máquina origen** (lo más simple, conserva permisos):

  ```bash
  # Desde la máquina origen:
  rsync -avh --exclude='.venv' --exclude='build' --exclude='archive' \
        --exclude='logs' --exclude='__pycache__' \
        /home/sintonizador/ usuario@maquina-destino:/home/sintonizador/
  ```

  Excluir `archive/`, `build/`, `.venv/` y `logs/` evita arrastrar gigabytes innecesarios — todo eso se regenera en la nueva máquina.

- **git clone** si tenés el repo en un remoto:

  ```bash
  cd /home/sintonizador
  git clone <url-del-repo> .
  ```

- **Tarball manual** como fallback. Lo mínimo que necesitás es: `src/`, `scripts/`, `pyproject.toml`, `README.md`, `INSTALL.md`, `HISTORICO.md`, este archivo, y opcionalmente `channels.conf` (ver paso 7).

Verificar:

```bash
ls /home/sintonizador
# Esperado al menos: src/  scripts/  pyproject.toml  INSTALL.md
```

---

## 4. Compilar e instalar el driver TBS

Seguir **íntegro** el procedimiento de [INSTALL.md](INSTALL.md) secciones 4–6. Resumen de los comandos clave:

```bash
sudo apt update
sudo apt install -y \
  build-essential linux-headers-$(uname -r) \
  git patchutils libproc-processtable-perl \
  flex bison libssl-dev libelf-dev bc wget \
  dvb-tools dtv-scan-tables v4l-utils w-scan

mkdir -p /home/sintonizador/build && cd /home/sintonizador/build
git clone --depth=10 https://github.com/tbsdtv/media_build.git
git clone --depth=10 -b latest https://github.com/tbsdtv/linux_media.git media

cd /home/sintonizador
sudo bash scripts/build-tbs-clean.sh   # 5–15 min, log en logs/build-clean.log
```

> ⚠️ **No usar** `install-tbs-driver.sh` ni `resume-build.sh` directamente — tienen problemas conocidos en kernel 6.8 (ver HISTORICO.md). El único script que funciona es `build-tbs-clean.sh`.

Después del build, dar acceso sin sudo a `/dev/dvb/*` en la sesión actual:

```bash
newgrp video
```

(En sesiones futuras ya está aplicado porque el script agregó al usuario al grupo `video`.)

Verificar:

```bash
bash scripts/verify-driver.sh
ls /dev/dvb/
# Esperado: adapter0  adapter1  adapter2  adapter3
dmesg | grep -i tbsecp3 | tail
# Esperado: "TurboSight TBS 6704(Quad ATSC/QAMB)" + 4 frontends registrados
```

Si esto no da los 4 adapters, **parar acá** y revisar la tabla de troubleshooting en INSTALL.md §“Si algo falla”. La app no va a funcionar sin esto.

---

## 5. Dependencias de runtime (no-Python)

La app usa dos binarios externos:

```bash
sudo apt install -y ffmpeg ccextractor
```

- **ffmpeg** → `ffprobe` para inspección de TS y captura corta del endpoint `/tuners/{n}/info`.
- **ccextractor** → extracción de Closed Captions en vivo (`/ws/cc/{n}`) y en el archiver 24×7. Sin esto los endpoints de CC devuelven `{"error":"ccextractor no instalado"}` pero la app sigue arrancando.

Verificar:

```bash
ffprobe -version | head -1
ccextractor --version 2>&1 | head -1
```

---

## 6. Entorno Python e instalación del paquete

Requiere **Python ≥3.12** (viene de fábrica en Ubuntu 24.04).

```bash
cd /home/sintonizador
sudo apt install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Verificar:

```bash
python -c "import sintonizador; print(sintonizador.__file__)"
# Esperado: /home/sintonizador/src/sintonizador/__init__.py
```

---

## 7. `channels.conf`

El catálogo de canales depende del **mercado ATSC** (ciudad/región). Dos casos:

**Caso A — la nueva máquina está en la misma área de cobertura que la original.**
Copiar el `channels.conf` que viene con el repo (ya rsynqueado en el paso 3). Listo.

**Caso B — está en otra ciudad / otro país / no estás seguro.**
Re-escanear desde cero (toma 5–15 min, va probando todas las frecuencias del bandplan ATSC):

```bash
cd /home/sintonizador
bash scripts/scan-channels.sh 0     # 0 = adapter a usar para el scan
# Genera channels.conf en el directorio actual (formato DVBv5)
```

Si el scan se queja con `ATSC frequency table not found`, falta `dtv-scan-tables` (paso 4 lo instala — verificá).

Verificar el catálogo:

```bash
wc -l channels.conf
head -30 channels.conf
```

---

## 8. Configuración (variables de entorno)

Todas opcionales, los defaults funcionan para la layout estándar:

| Variable | Default | Para qué |
|---|---|---|
| `SINTONIZADOR_CHANNELS` | `/home/sintonizador/channels.conf` | Path al catálogo |
| `SINTONIZADOR_ARCHIVE` | `/home/sintonizador/archive` | Raíz del archive 24×7 (los TS y los `.txt` de captions van acá; necesita espacio en disco) |
| `SINTONIZADOR_ROTATION_MINUTES` | `30` | Minutos por archivo del archiver |
| `SINTONIZADOR_AUTOSTART` / `SINTONIZADOR_ARCHIVE_AUTOSTART` | `true` | Si el archiver arranca solo con el server. Poné `false` si querés controlarlo a mano vía `POST /archive` |

Ejemplo, si querés desactivar autostart del archiver mientras probás:

```bash
export SINTONIZADOR_ARCHIVE_AUTOSTART=false
```

Y asegurá espacio en `SINTONIZADOR_ARCHIVE`: el archiver 24×7 graba TS continuo de los 4 mux, son varios GB/hora.

---

## 9. Arrancar la app

Con el venv activo:

```bash
cd /home/sintonizador
source .venv/bin/activate
python -m sintonizador.main
```

Esperás ver al final del log:

```
INFO sintonizador.api.app: catálogo cargado: NN canales desde /home/sintonizador/channels.conf
INFO sintonizador.monitor.poller: monitor poller started: adapters=[0, 1, 2, 3] interval=0.50s
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

Smoke test desde otra terminal:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/         # 200
curl -s http://127.0.0.1:8000/tuners | head -c 500                      # JSON con 4 tuners, available:true
```

En el browser: `http://<host>:8000/` para el dashboard, `http://<host>:8000/docs` para Swagger.

Si los 4 tuners reportan `available: false` con `ENOENT en /dev/dvb/...`, el driver no está cargado o no se aplicaron permisos del grupo `video` — volver al paso 4.

---

## 10. (Opcional) Autostart con systemd

Para que la app arranque sola al boot, crear `/etc/systemd/system/sintonizador.service`:

```ini
[Unit]
Description=Sintonizador TBS6704 (FastAPI + archiver)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=transcriber
Group=video
WorkingDirectory=/home/sintonizador
Environment=PATH=/home/sintonizador/.venv/bin:/usr/bin:/bin
ExecStart=/home/sintonizador/.venv/bin/python -m sintonizador.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Cambiá `User=transcriber` por el usuario real de la máquina destino (el dueño de `/home/sintonizador/` y miembro del grupo `video`).

Activar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sintonizador
sudo systemctl status sintonizador
journalctl -u sintonizador -f
```

---

## 11. Checklist final

- [ ] `uname -r` devuelve algo ≤6.14
- [ ] `lspci -nn | grep 544d` muestra la tarjeta
- [ ] `ls /dev/dvb/` muestra `adapter0..3`
- [ ] `bash scripts/verify-driver.sh` pasa sin errores
- [ ] `ffprobe -version` y `ccextractor --version` funcionan
- [ ] `python -c "import sintonizador"` no falla
- [ ] `channels.conf` existe y tiene canales (no vacío)
- [ ] `curl http://127.0.0.1:8000/tuners` devuelve 200 con `available: true` en los 4 tuners
- [ ] (Opcional) `systemctl status sintonizador` activo

Cuando todos estos OK, la app está corriendo igual que en la máquina original.

---

## Si algo falla

Errores típicos y dónde mirar:

| Síntoma | Mirar |
|---|---|
| Build del driver muere | [INSTALL.md](INSTALL.md) §“Si algo falla” + `logs/build-clean.log` + [HISTORICO.md](HISTORICO.md) |
| `tbsecp3` carga pero no hay `/dev/dvb/` | [HISTORICO.md](HISTORICO.md) §4 (PCI ID matching) |
| App arranca pero los 4 tuners `available:false` | Driver no cargó o falta `newgrp video` / reboot |
| `ccextractor no instalado` en endpoints CC | Paso 5 |
| `dvbv5-zap`/`scan-channels.sh` da “no signal” | Antena, conexión física, frecuencias del mercado |
| Kernel se actualizó solo a >6.14 | Pinear el kernel viejo y volver a paso 1; eventualmente hay que `apt-mark hold linux-image-generic linux-headers-generic` |

Logs útiles en runtime:
- App: stdout del proceso o `journalctl -u sintonizador -f`
- Driver: `dmesg | grep -i -E 'tbsecp3|saa716x|dvb'`
- Build: `logs/build-clean.log`
