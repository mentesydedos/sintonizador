"""Estáticos del dashboard + workbench. Se sirven desde rutas en api/app.py."""

from pathlib import Path

WEB_DIR = Path(__file__).parent
INDEX_PATH = WEB_DIR / "index.html"
WORKBENCH_PATH = WEB_DIR / "workbench.html"
