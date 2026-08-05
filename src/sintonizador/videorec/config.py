"""Mapeo de "nombre bonito" para la grabación de video.

`Channel.name` (y por lo tanto `ArchiveTarget.channel_name`) es el callsign
crudo del PMT (ej. "XHGA"), no el nombre comercial ("Las Estrellas"). Para que
las carpetas de grabación coincidan con los nombres de canal que usa
transcriber-linux (BD de transcripciones, EPG, dashboard AlertaTV), hace falta
esta capa de traducción aparte — no se toca `channels.py` ni `ArchiveTarget`.
"""

from __future__ import annotations

from sintonizador.archiver.config import ArchiveTarget

# Confirmado por cruce con TVHeadend (mismo mercado ATSC de Guadalajara,
# alerts/epg.py TVH_CHANNEL_MAP en transcriber-linux) y con
# /home/sintonizador/ANALISIS-CANALES.md:
#   - XHGA 2.1 = Las Estrellas (canal principal, confirmado en pantalla).
#   - XHSFJ 7.1/7.2 = Azteca 7 / A más + (TVHeadend liga ambos nombres al mismo
#     callsign XHSFJ/575MHz, coincide con el 7.1 principal + 7.2 secundario).
#   - XHJAL 1.1/1.2 = Azteca Uno / ADN Noticias (mismo cruce, 587MHz).
# XHCTGD (557MHz) 3.1/3.3/3.4: el scan original (ANALISIS-CANALES.md) NO
# determinó nombre comercial para estos subcanales. TVHeadend tiene "Imagen" y
# "Excélsior TV" ligados a servicios llamados "XHCTGD", pero en instancias/
# redes distintas — no hay certeza de cuál vchannel es cuál sin verlo. NO
# asumir "Canal 44" (ese es XHUDG/551MHz, un mux distinto). Verificar viendo
# el contenido real antes de asignarles nombre; mientras tanto quedan con el
# nombre genérico que arma `pretty_name()`.
PRETTY_NAMES: dict[tuple[str, str], str] = {
    ("XHGA", "2.1"): "Las Estrellas",
    ("XHSFJ", "7.1"): "Azteca 7",
    ("XHSFJ", "7.2"): "A más +",
    ("XHJAL", "1.1"): "Azteca Uno",
    ("XHJAL", "1.2"): "ADN Noticias",
}


def pretty_name(target: ArchiveTarget) -> str:
    """Nombre de canal "bonito" para carpetas/BD, o un genérico si no se conoce."""
    key = (target.channel_name, target.vchannel)
    if key in PRETTY_NAMES:
        return PRETTY_NAMES[key]
    return f"{target.channel_name}_{target.vchannel.replace('.', '_')}"


def safe_name(name: str) -> str:
    """Idéntico al `_safe_name()` de video_recorder.py (transcriber-linux) —
    NO usar `make_slug` de archiver/config.py (preserva puntos/guiones, es un
    formato distinto pensado para slugs de archivo internos del archiver).
    `alerts/clips.py` en transcriber-linux matchea carpetas de canal comparando
    contra este mismo algoritmo, así que debe ser carácter-por-carácter igual."""
    return "".join(c if c.isalnum() else "_" for c in name)
