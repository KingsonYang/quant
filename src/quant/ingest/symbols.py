from __future__ import annotations

from pathlib import Path


def load_symbol_name_pairs(markdown_path: Path) -> list[tuple[str, str]]:
    """Parse a markdown table with columns `代码` and `名称`."""
    if not markdown_path.exists():
        raise FileNotFoundError(f"Symbol file not found: {markdown_path}")

    rows: list[tuple[str, str]] = []
    for raw_line in markdown_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        if "代码" in line and "名称" in line:
            continue
        if set(line.replace("|", "").replace("-", "").replace(" ", "")) == set():
            continue

        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) < 2:
            continue

        symbol = parts[0].upper()
        name = parts[1]
        if symbol and name:
            rows.append((symbol, name))
    return rows
