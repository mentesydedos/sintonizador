"""Grabación de video 24x7 (H.264 + AAC, segmentos rotativos de 30 min).

Reusa el mismo `ArchiveTarget`/`build_targets` del archiver de Closed Captions
(`sintonizador.archiver.config`) y el mismo patrón de `TsConsumer` colgado del
`MuxReaderRegistry` compartido — ver `sintonizador.archiver.pipeline` y
`sintonizador.monitor.transcode` para el precedente de este patrón.
"""

from sintonizador.videorec.config import pretty_name, safe_name
from sintonizador.videorec.pipeline import PipelineStats, VideoRecordPipeline
from sintonizador.videorec.recorder import VideoRecorder

__all__ = [
    "PipelineStats",
    "VideoRecorder",
    "VideoRecordPipeline",
    "pretty_name",
    "safe_name",
]
