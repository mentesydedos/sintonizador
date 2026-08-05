"""Fan-out compartido del TS: un tap por adapter, N consumidores.

Ver `reader.MuxReader` para el constraint de un-tap-por-adapter.
"""

from sintonizador.mux.reader import MuxReader, TsConsumer
from sintonizador.mux.registry import MuxReaderRegistry

__all__ = ["MuxReader", "TsConsumer", "MuxReaderRegistry"]
