"""ASGI entrypoint for local hosting (``uvicorn app:app``).

Vercel uses ``api/index.py`` instead: the dashboard needs the local SQLite
database, which is not deployed.
"""

from atlas.dashboard import create_app

app = create_app()
