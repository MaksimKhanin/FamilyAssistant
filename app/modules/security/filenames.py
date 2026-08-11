"""Грамматика имён файлов, которые присылает домашний рекордер.

Рекордер не передаёт ни времени съёмки, ни типа файла отдельным полем — и то,
и другое зашито в имя. Значит имя является частью протокола, и разбирать его
надо ровно так же, как рекордер его собирает:

    2026-02-08_11-18-06_camera_1_video_done.mp4            — чанк видео
    2026-02-08_11-18-06_camera_1_image_by_external_signal_done_post.jpg
    26-02-08T-13-11-59_captured_3800_0.4713_person_done_post.jpg   — срабатывание YOLO

Время в имени — **локальное** время дома (`datetime.now()` на стороне рекордера),
поэтому вызывающий обязан прогнать результат через `clock.to_utc`.
"""
import re
from pathlib import Path
from typing import Optional

from datetime import datetime

#: 2026-02-08_11-18-06_...
_VIDEO_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_")
#: 26-02-08T-13-11-59_captured_...
_ALERT_TS_RE = re.compile(r"^(\d{2}-\d{2}-\d{2}T-\d{2}-\d{2}-\d{2})_captured")

_VIDEO_TS_FMT = "%Y-%m-%d_%H-%M-%S"
_ALERT_TS_FMT = "%y-%m-%dT-%H-%M-%S"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}

KIND_PHOTO = "photo"
KIND_VIDEO = "video"


def safe_filename(raw: str) -> str:
    """Имя от клиента, пригодное для подстановки в путь.

    Отклоняем, а не «чиним»: имя приходит по сети и участвует в построении пути,
    так что тихая замена `../../etc/passwd` на `passwd` сохранила бы файл не туда,
    куда думает отправитель, и никто бы этого не заметил.
    """
    name = (raw or "").strip()
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Имя файла не должно содержать путь")
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError("Пустое или скрытое имя файла")
    if len(name) > 255:
        raise ValueError("Слишком длинное имя файла")
    return name


def guess_kind(filename: str) -> str:
    return KIND_VIDEO if Path(filename).suffix.lower() in VIDEO_EXTENSIONS else KIND_PHOTO


def parse_captured_at(filename: str) -> Optional[datetime]:
    """Локальное время съёмки из имени файла, либо None, если формат не наш."""
    match = _ALERT_TS_RE.match(filename)
    if match:
        try:
            return datetime.strptime(match.group(1), _ALERT_TS_FMT)
        except ValueError:
            return None

    match = _VIDEO_TS_RE.match(filename)
    if match:
        try:
            return datetime.strptime(match.group(1), _VIDEO_TS_FMT)
        except ValueError:
            return None
    return None
