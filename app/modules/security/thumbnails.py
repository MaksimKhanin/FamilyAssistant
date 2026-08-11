"""Превью для архива.

Сетка архива — это сотни картинок на экран. Отдавать в неё полноразмерные кадры
(а для видео — вообще нечего отдавать) значит выжечь и трафик, и батарею телефона,
поэтому рядом с каждым файлом кладётся jpeg с длинной стороной 320 px. Для видео
превью — первый кадр.

Работа блокирующая (cv2 декодирует файл), вызывать только из пула потоков.
"""
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.modules.security.filenames import KIND_VIDEO

logger = get_logger("security.thumbnails")

THUMB_MAX_DIM = 320
THUMB_SUFFIX = "_thumb.jpg"
THUMB_QUALITY = 80


def thumbnail_path_for(media_path: Path) -> Path:
    return media_path.with_name(media_path.stem + THUMB_SUFFIX)


def generate(media_path: Path, kind: str) -> Optional[Path]:
    """Сделать превью рядом с оригиналом. None, если файл не читается."""
    try:
        import cv2
    except ImportError:                                   # pragma: no cover
        logger.warning("opencv не установлен — превью для архива не будет")
        return None

    try:
        if kind == KIND_VIDEO:
            capture = cv2.VideoCapture(str(media_path))
            ok, frame = capture.read()
            capture.release()
            if not ok:
                logger.warning(f"Не удалось прочитать первый кадр: {media_path.name}")
                return None
        else:
            frame = cv2.imread(str(media_path))
            if frame is None:
                logger.warning(f"Не удалось прочитать изображение: {media_path.name}")
                return None

        height, width = frame.shape[:2]
        scale = min(THUMB_MAX_DIM / max(height, width), 1.0)   # не растягиваем мелкое
        if scale < 1.0:
            frame = cv2.resize(frame, (int(width * scale), int(height * scale)),
                               interpolation=cv2.INTER_AREA)

        thumb_path = thumbnail_path_for(media_path)
        if not cv2.imwrite(str(thumb_path), frame, [cv2.IMWRITE_JPEG_QUALITY, THUMB_QUALITY]):
            return None
        return thumb_path
    except Exception as e:                                 # cv2 бросает всё что угодно
        logger.warning(f"Превью для {media_path.name} не сделалось: {e}")
        return None
