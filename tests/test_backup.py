"""Тесты бэкапа БД и вспомогательной политики (T02).

Снимки делаются через ``VACUUM INTO`` во временный каталог; рабочая
``data/bot.db`` не затрагивается.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from bot.db import Database
from bot.models import to_iso
from tests.helpers import T0, run


def test_backup_creates_readable_snapshot(db: Database, tmp_path: Path) -> None:
    post = run(db.create_post(url="https://example.com/backup", source="VGC"))
    backup_dir = tmp_path / "backups"

    snapshot = run(db.backup(backup_dir))

    assert snapshot.exists()
    assert snapshot.parent == backup_dir
    # Снимок — полноценная БД: открываем и находим тот же пост.
    copy = Database(snapshot)
    restored = run(copy.get_post(post.id))
    assert restored is not None and restored.url == post.url


def test_backup_rotation_keeps_latest(db: Database, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    run(db.create_post(url="https://example.com/rotate", source="VGC"))

    for hours in range(5):
        run(db.backup(backup_dir, keep=2, now=T0 + timedelta(hours=hours)))

    snapshots = sorted(backup_dir.glob("*.db"))
    assert len(snapshots) == 2


def test_backup_of_empty_database(db: Database, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    snapshot = run(db.backup(backup_dir))
    assert snapshot.exists() and snapshot.stat().st_size > 0


def test_to_iso_is_lexicographically_ordered() -> None:
    """Формат меток времени позволяет сравнивать их строками в SQL."""
    earlier = to_iso(T0)
    later = to_iso(T0 + timedelta(seconds=1))
    assert earlier is not None and later is not None
    assert earlier < later
    assert earlier.endswith("+00:00")
