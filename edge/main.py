"""Edge vision worker — run at home, next to the cameras.

    python -m edge.main

One thread per camera: read the RTSP stream, gate on motion, run YOLO on what
moved, and upload only frames that actually contain a person or a vehicle. A
cooldown keeps one person walking past a gate from becoming forty notifications.

The worker holds no state and no database: everything it knows how to do is
«посмотреть и рассказать». All judgement about what matters lives on the server,
where the family can change it (see app/modules/security/service.py).
"""
import signal
import threading
import time
from datetime import datetime

import cv2

from app.core.logging import get_logger
from edge.config import CameraConfig, EdgeConfig, load_config
from edge.detector import MotionGate, ObjectDetector
from edge.uploader import Uploader

logger = get_logger("edge")

RECONNECT_DELAY_SEC = 10
stop_event = threading.Event()


def watch_camera(camera: CameraConfig, detector: ObjectDetector, uploader: Uploader, sample_every: int):
    gate = MotionGate(camera.motion_threshold)
    last_sent = 0.0
    frame_index = 0

    while not stop_event.is_set():
        capture = cv2.VideoCapture(camera.url)
        if not capture.isOpened():
            logger.error(f"Камера {camera.name} недоступна, повтор через {RECONNECT_DELAY_SEC} с")
            capture.release()
            stop_event.wait(RECONNECT_DELAY_SEC)
            continue

        logger.info(f"Камера {camera.name}: поток открыт")
        while not stop_event.is_set():
            ok, frame = capture.read()
            if not ok:
                logger.warning(f"Камера {camera.name}: поток оборвался")
                break

            frame_index += 1
            if frame_index % sample_every:
                continue
            if not gate.moved(frame):
                continue
            if time.monotonic() - last_sent < camera.cooldown_sec:
                continue

            detection = detector.detect(frame)
            if detection is None:
                continue

            logger.info(f"Камера {camera.name}: {detection.class_name} "
                        f"(уверенность {detection.confidence:.2f}, площадь {detection.area})")
            uploader.send(camera.name, detection.class_name, detection.confidence,
                          detection.area, detection.frame, captured_at=datetime.utcnow())
            last_sent = time.monotonic()

        capture.release()
        stop_event.wait(RECONNECT_DELAY_SEC)

    logger.info(f"Камера {camera.name}: остановлена")


def main():
    config: EdgeConfig = load_config()
    if not config.configured:
        raise SystemExit("Не задан адрес сервера, ключ или список камер — проверьте edge/cameras.yml")

    detector = ObjectDetector(config.model_path, config.confidence,
                              min_area=min(c.min_area for c in config.cameras))
    uploader = Uploader(config.server_url, config.api_key, config.family_id)

    def shutdown(*_):
        logger.info("Получен сигнал остановки")
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threads = [
        threading.Thread(target=watch_camera, args=(camera, detector, uploader, config.sample_every),
                         name=f"camera-{camera.name}", daemon=True)
        for camera in config.cameras
    ]
    for thread in threads:
        thread.start()
    logger.info(f"Наблюдение запущено: камер — {len(threads)}")

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=5)
        logger.info("Воркер остановлен")


if __name__ == "__main__":
    main()
