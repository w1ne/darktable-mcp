"""Tests for LibraryDB against a synthetic darktable schema."""

import sqlite3
from pathlib import Path

import pytest

from darktable_mcp.darktable.library_db import LibraryDB, LibraryNotFoundError


def _make_library(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE film_rolls (id INTEGER PRIMARY KEY, folder TEXT);
            CREATE TABLE images (
                id INTEGER PRIMARY KEY,
                film_id INTEGER,
                filename TEXT,
                flags INTEGER
            );
            INSERT INTO film_rolls (id, folder) VALUES (1, '/photos/2024');
            INSERT INTO film_rolls (id, folder) VALUES (2, '/photos/2025');
            INSERT INTO images (id, film_id, filename, flags) VALUES (1, 1, 'a.raw', 5);
            INSERT INTO images (id, film_id, filename, flags) VALUES (2, 1, 'b.raw', 8);
            INSERT INTO images (id, film_id, filename, flags) VALUES (3, 2, 'c.raw', 2);
            INSERT INTO images (id, film_id, filename, flags) VALUES (4, 2, 'd.jpg', 4);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_view_photos_no_filter(tmp_path):
    db = tmp_path / "library.db"
    _make_library(db)

    rows = LibraryDB(db_path=db).view_photos()

    assert len(rows) == 4
    assert rows[0].id == 4
    by_filename = {Path(r.path).name: r for r in rows}
    assert by_filename["a.raw"].rating == 5
    assert by_filename["b.raw"].rating == -1
    assert by_filename["c.raw"].rating == 2
    assert by_filename["a.raw"].path == "/photos/2024/a.raw"


def test_view_photos_rating_min_excludes_rejected(tmp_path):
    db = tmp_path / "library.db"
    _make_library(db)

    rows = LibraryDB(db_path=db).view_photos(rating_min=4)

    assert {Path(r.path).name for r in rows} == {"a.raw", "d.jpg"}
    assert all(r.rating >= 4 for r in rows)


def test_view_photos_filter_substring(tmp_path):
    db = tmp_path / "library.db"
    _make_library(db)

    rows = LibraryDB(db_path=db).view_photos(filter_text="2025")

    assert {Path(r.path).name for r in rows} == {"c.raw", "d.jpg"}


def test_view_photos_limit(tmp_path):
    db = tmp_path / "library.db"
    _make_library(db)

    rows = LibraryDB(db_path=db).view_photos(limit=2)

    assert len(rows) == 2


def test_missing_library_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("DARKTABLE_LIBRARY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    with pytest.raises(LibraryNotFoundError):
        LibraryDB()
