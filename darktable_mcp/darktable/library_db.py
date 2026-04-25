"""Read-only access to darktable's SQLite library."""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..utils.errors import DarktableMCPError


class LibraryNotFoundError(DarktableMCPError):
    """Raised when darktable's library.db cannot be located."""


@dataclass
class PhotoRow:
    id: int
    path: str
    rating: int  # -1 = rejected, 0–5 = stars


def _candidate_paths() -> List[Path]:
    home = Path.home()
    paths: List[Path] = []

    env_path = os.environ.get("DARKTABLE_LIBRARY")
    if env_path:
        paths.append(Path(env_path))

    paths.append(home / ".config" / "darktable" / "library.db")

    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        paths.append(Path(localappdata) / "darktable" / "library.db")
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "darktable" / "library.db")

    paths.append(home / "Library" / "Application Support" / "darktable" / "library.db")
    return paths


class LibraryDB:
    """Thin read-only wrapper around darktable's library.db."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        resolved: Optional[Path] = Path(db_path) if db_path is not None else None
        if resolved is None:
            for candidate in _candidate_paths():
                if candidate.is_file():
                    resolved = candidate
                    break
        if resolved is None or not resolved.is_file():
            raise LibraryNotFoundError(
                "Could not locate darktable library.db. "
                "Set DARKTABLE_LIBRARY env var or pass db_path explicitly."
            )
        self.db_path = resolved

    def _connect(self) -> sqlite3.Connection:
        # mode=ro is enough; we deliberately don't pass immutable=1 because
        # darktable may be writing to the file concurrently.
        uri = f"file:{self.db_path}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def view_photos(
        self,
        rating_min: Optional[int] = None,
        filter_text: Optional[str] = None,
        limit: int = 50,
    ) -> List[PhotoRow]:
        clauses: List[str] = []
        params: List[object] = []

        if rating_min is not None:
            clauses.append("(i.flags & 7) >= ?")
            clauses.append("(i.flags & 8) = 0")
            params.append(int(rating_min))

        if filter_text:
            clauses.append("(fr.folder LIKE ? OR i.filename LIKE ?)")
            like = f"%{filter_text}%"
            params.extend([like, like])

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT i.id, fr.folder, i.filename, i.flags "
            "FROM images i "
            "JOIN film_rolls fr ON i.film_id = fr.id "
            f"{where} "
            "ORDER BY i.id DESC "
            "LIMIT ?"
        )
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        result: List[PhotoRow] = []
        for image_id, folder, filename, flags in rows:
            rating = -1 if (flags & 8) else (flags & 7)
            path = str(Path(folder) / filename)
            result.append(PhotoRow(id=int(image_id), path=path, rating=int(rating)))
        return result
