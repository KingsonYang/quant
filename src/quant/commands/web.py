from __future__ import annotations

from pathlib import Path

import typer

from quant.db.init_db import DEFAULT_DB_PATH


def register(app: typer.Typer) -> None:
    @app.command("web")
    def web_command(
        host: str = typer.Option("127.0.0.1", "--host", help="Bind host."),
        port: int = typer.Option(8000, "--port", help="Bind port."),
        db_path: Path = typer.Option(
            DEFAULT_DB_PATH,
            "--db-path",
            help="Target DuckDB file path.",
        ),
    ) -> None:
        """Start the local strategy workbench."""
        import uvicorn

        from quant.web.app import create_app

        uvicorn.run(create_app(db_path), host=host, port=port)
