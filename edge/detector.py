"""Motion gate + YOLO detector — the cheap, always-on part of the pipeline.

Two filters in front of everything else, because the point of this project is that
almost nothing should ever leave the house:

    кадр → изменился ли он вообще?     (вычитание кадров, копейки по CPU)
           → да → есть ли в нём объект? (YOLO, только на изменившихся кадрах)
              → да → отправляем на сервер

The server never sees a frame that has no person or vehicle in it.
"""
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

from app.core.logging import get_logger

logger = get_logger("edge.detector")

#: COCO-классы, ради которых имеет смысл будить сервер.
WATCHED_CLASSES = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


@dataclass
class Detection:
    class_name: str
    confidence: float
    area: int
    frame: "np.ndarray"


class MotionGate:
    """Frame-difference gate — decides whether a frame is worth running YOLO on."""

    def __init__(self, threshold: int = 3000):
        self.threshold = threshold
        self._previous: Optional[np.ndarray] = None

    def moved(self, frame: "np.ndarray") -> bool:
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        grey = cv2.GaussianBlur(grey, (21, 21), 0)

        if self._previous is None:
            self._previous = grey
            return False

        delta = cv2.absdiff(self._previous, grey)
        self._previous = grey
        changed = cv2.countNonZero(cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1])
        return changed >= self.threshold


class ObjectDetector:
    """YOLO wrapper. Loaded once per process and shared by all camera threads."""

    def __init__(self, model_path: str = "yolov8n.pt", confidence: float = 0.35, min_area: int = 500):
        from ultralytics import YOLO   # тяжёлый импорт — только когда воркер реально запускается

        self.model = YOLO(model_path)
        self.confidence = confidence
        self.min_area = min_area
        logger.info(f"Детектор загружен: {model_path}")

    def detect(self, frame: "np.ndarray") -> Optional[Detection]:
        """Return the largest watched object in the frame, or None."""
        try:
            result = self.model(frame, verbose=False, conf=self.confidence)[0]
        except Exception:
            logger.exception("Ошибка инференса — пропускаю кадр")
            return None

        best: Optional[Detection] = None
        boxes = result.boxes
        if boxes is None or boxes.cls is None:
            return None

        for class_id, box, conf in zip(boxes.cls.cpu().numpy(),
                                       boxes.xyxy.cpu().numpy().astype(int),
                                       boxes.conf.cpu().numpy()):
            name = WATCHED_CLASSES.get(int(class_id))
            if name is None:
                continue
            x1, y1, x2, y2 = box
            area = int((x2 - x1) * (y2 - y1))
            if area < self.min_area:
                continue
            if best is None or area > best.area:
                annotated = frame.copy()
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (46, 110, 126), 2)
                best = Detection(class_name=name, confidence=float(conf), area=area, frame=annotated)

        return best
