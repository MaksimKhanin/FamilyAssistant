"""Media files on disk.

The database stores metadata and a path; the bytes live under MEDIA_ROOT with
rotation (see app/modules/security/retention.py). Nothing outside this module
builds media paths by hand.
"""
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("media")


def media_dir(*parts: str) -> Path:
    path = Path(settings.media_root).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_bytes(data: bytes, *parts: str, suffix: str = ".jpg") -> str:
    path = media_dir(*parts) / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(data)
    return str(path)


def stage_attachment(data: bytes, user_id: int, suffix: str = ".jpg") -> str:
    """Park an attachment that belongs to an action still waiting for a human «да»."""
    return store_bytes(data, "pending", str(user_id), datetime.utcnow().strftime("%Y-%m-%d"), suffix=suffix)


def read_and_discard(path: Optional[str]) -> Optional[bytes]:
    """Read a staged attachment and remove it — it has served its purpose."""
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        logger.warning(f"Вложение отложенного действия не найдено: {path}")
        return None
    data = file_path.read_bytes()
    try:
        file_path.unlink()
    except OSError:
        logger.warning(f"Не удалось удалить временное вложение: {path}")
    return data


def is_inside_media_root(path: str) -> bool:
    """Guard for routes that serve files by path from the database."""
    try:
        Path(path).resolve().relative_to(Path(settings.media_root).resolve())
        return True
    except (ValueError, OSError):
        return False


def relative(path) -> str:
    """Путь внутри MEDIA_ROOT — то, что попадает в базу.

    Архив живёт годами и переезжает вместе с томом, поэтому в базе хранится
    относительный путь: сменить MEDIA_ROOT можно, не трогая ни одной строки.
    """
    return Path(path).resolve().relative_to(Path(settings.media_root).resolve()).as_posix()


def resolve(rel_path: str) -> Path:
    """Относительный путь из базы → файл на диске."""
    return Path(settings.media_root) / rel_path


def prune_empty_dirs(*parts: str) -> None:
    """Убрать опустевшие каталоги после ротации, не трогая сам MEDIA_ROOT."""
    root = Path(settings.media_root).resolve()
    path = Path(settings.media_root).joinpath(*parts).resolve()
    while path != root and root in path.parents:
        try:
            path.rmdir()
        except OSError:
            return       # каталог не пуст или недоступен — дальше подниматься незачем
        path = path.parent
