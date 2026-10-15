"""Serve the console against the DuckDB stand-in (UI development without Exasol).

    python -m tests.serve_fake            # http://127.0.0.1:8001
"""
import uvicorn

from oee.app import create_app
from tests.fakedb import DuckBackend

if __name__ == "__main__":
    backend = DuckBackend(days=60)
    uvicorn.run(create_app(lambda: backend), host="127.0.0.1", port=8001, log_level="warning")
