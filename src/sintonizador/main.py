"""Entry point: levanta uvicorn con la app de la API."""

from __future__ import annotations

import logging

import uvicorn


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        "sintonizador.api.app:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=False,  # Demasiado ruido con polls /tuners cada poco
    )


if __name__ == "__main__":
    run()
