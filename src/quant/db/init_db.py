from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from quant.experiments.store import ensure_experiment_schema


DEFAULT_DB_PATH = Path("data/quant.duckdb")
DEFAULT_SCHEMA_PATH = Path("sql/schema_v1.sql")


@dataclass(frozen=True)
class InitResult:
    db_path: Path
    schema_path: Path
    statements_executed: int


def load_schema(schema_path: Path) -> str:
    """Load the SQL schema file from disk."""
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    return schema_path.read_text(encoding="utf-8")


def count_executable_statements(schema_sql: str) -> int:
    """Count non-empty SQL statements separated by semicolons."""
    return sum(1 for statement in schema_sql.split(";") if statement.strip())


def init_duckdb(
    db_path: Path = DEFAULT_DB_PATH,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
) -> InitResult:
    """Initialize a DuckDB database using the v1 schema."""
    resolved_db_path = db_path.resolve()
    resolved_schema_path = schema_path.resolve()
    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = load_schema(resolved_schema_path)
    statements_executed = count_executable_statements(schema_sql)

    with duckdb.connect(str(resolved_db_path)) as connection:
        connection.execute(schema_sql)
        ensure_experiment_schema(connection)

    return InitResult(
        db_path=resolved_db_path,
        schema_path=resolved_schema_path,
        statements_executed=statements_executed,
    )
