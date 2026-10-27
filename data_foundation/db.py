"""External SQLite service storage; no network or publication authority."""
import os
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def external_path(path: str | Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path == REPO or REPO in path.parents:
        raise ValueError("service storage must be outside repository")
    return path


def data_paths() -> tuple[Path, Path]:
    base = external_path(os.environ.get("HANSET_DATA_DIR", "/workspace/hanset-data"))
    return external_path(base / "raw"), external_path(base / "db/hanset_facts.sqlite3")


def open_db(path: str | Path | None = None) -> sqlite3.Connection:
    path = external_path(path) if path is not None else data_paths()[1]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript((REPO / "migrations/0001_fact_store.sql").read_text())
    os.chmod(path, 0o600)
    return db
