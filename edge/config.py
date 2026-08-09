"""Configuration for the edge vision worker.

Runs at home, next to the cameras — a plain Python process, no Docker. Reads
`edge/cameras.yml` (an untracked copy of `cameras.yml.example`), with secrets
overridable from the environment.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml

CONFIG_PATH = Path(os.environ.get("EDGE_CONFIG", "edge/cameras.yml"))


@dataclass
class CameraConfig:
    name: str                       # slug, под которым камера появится на сервере
    url: str                        # RTSP-поток (обычно sub-stream: он дешевле для детекции)
    motion_threshold: int = 3000    # площадь изменившихся пикселей, с которой начинаем смотреть
    min_area: int = 500             # минимальная площадь объекта, чтобы считать его объектом
    cooldown_sec: int = 45          # не слать одно и то же событие чаще, чем раз в N секунд


@dataclass
class EdgeConfig:
    server_url: str = ""
    api_key: str = ""
    family_id: int = 0
    model_path: str = "yolov8n.pt"
    confidence: float = 0.35
    sample_every: int = 3           # обрабатывать каждый N-й кадр
    cameras: List[CameraConfig] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.server_url and self.api_key and self.cameras)


def load_config(path: Path = None) -> EdgeConfig:
    path = path or CONFIG_PATH
    if not path.exists():
        raise RuntimeError(f"Нет файла конфигурации: {path} (скопируйте cameras.yml.example)")

    raw: Dict = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    server = raw.get("server") or {}
    detector = raw.get("detector") or {}

    cameras = []
    for name, camera_raw in (raw.get("cameras") or {}).items():
        if not camera_raw or not camera_raw.get("url"):
            raise ValueError(f"У камеры «{name}» не задан url")
        cameras.append(CameraConfig(
            name=name,
            url=camera_raw["url"],
            motion_threshold=int(camera_raw.get("motion_threshold", 3000)),
            min_area=int(camera_raw.get("min_area", 500)),
            cooldown_sec=int(camera_raw.get("cooldown_sec", 45)),
        ))

    return EdgeConfig(
        server_url=os.environ.get("SERVER_URL", server.get("url", "")).rstrip("/"),
        api_key=os.environ.get("INGEST_API_KEY", server.get("api_key", "")),
        family_id=int(os.environ.get("FAMILY_ID", server.get("family_id", 0)) or 0),
        model_path=detector.get("model", "yolov8n.pt"),
        confidence=float(detector.get("confidence", 0.35)),
        sample_every=int(detector.get("sample_every", 3)),
        cameras=cameras,
    )
