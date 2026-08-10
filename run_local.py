"""Пробный локальный запуск — одной командой, без Docker и без Postgres.

    python run_local.py

Поднимает панель на SQLite, заводит демо-семью с данными за неделю и открывает
всё на http://127.0.0.1:8000. Ключи для уведомлений генерируются автоматически:
на localhost браузер считает соединение безопасным, поэтому push здесь работает
по-настоящему.

Если модель не настроена, агент включает офлайн-режим (`app/agent/stub.py`) —
разбор ключевых слов вместо LLM. Чат при этом живой: видно и выбор инструмента, и
трейс, и карточку, и подтверждение действия. Чтобы говорить с настоящей моделью,
положите рядом `.env` с `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`.

Всё локальное хозяйство лежит в `.local/` и в git не попадает:

    .local/family.db      база
    .local/media/         фото блюд и кадры с камер
    .local/secrets.json   секрет сессий и ключи VAPID

    python run_local.py --reset       начать с чистой базы
    python run_local.py --no-demo     без демо-данных
    python run_local.py --port 9000
"""
import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

LOCAL_DIR = Path(__file__).parent / ".local"
SECRETS_FILE = LOCAL_DIR / "secrets.json"
DB_FILE = LOCAL_DIR / "family.db"
MEDIA_DIR = LOCAL_DIR / "media"

DEMO_PASSWORD = "demo12345"


# --- окружение ------------------------------------------------------------

def load_dotenv(path: Path):
    """Минимальный разбор .env — чтобы можно было подложить ключ настоящей модели."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ensure_secrets() -> dict:
    """Секрет сессий и пара ключей VAPID: генерируются один раз и переживают перезапуски."""
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))

    sys.path.insert(0, str(Path(__file__).parent))
    from app.core.webpush import generate_vapid_keys

    private, public = generate_vapid_keys()
    data = {
        "session_secret": secrets.token_urlsafe(32),
        "ingest_api_key": secrets.token_urlsafe(24),
        "vapid_private": private,
        "vapid_public": public,
    }
    SECRETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def prepare_environment(port: int) -> dict:
    LOCAL_DIR.mkdir(exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    load_dotenv(Path(__file__).parent / ".env")
    keys = ensure_secrets()

    os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB_FILE}")
    os.environ.setdefault("MEDIA_ROOT", str(MEDIA_DIR))
    os.environ.setdefault("SESSION_SECRET", keys["session_secret"])
    os.environ.setdefault("INGEST_API_KEY", keys["ingest_api_key"])
    os.environ.setdefault("VAPID_PUBLIC_KEY", keys["vapid_public"])
    os.environ.setdefault("VAPID_PRIVATE_KEY", keys["vapid_private"])
    os.environ.setdefault("VAPID_SUBJECT", "mailto:local@example.com")
    os.environ.setdefault("PUBLIC_BASE_URL", f"http://127.0.0.1:{port}")
    os.environ["COOKIE_SECURE"] = "false"          # локально всегда http

    # С --no-demo база будет пустой, и войти было бы некому: тот же bootstrap
    # из окружения, что и в бою, только с заранее известным паролем.
    os.environ.setdefault("ADMIN_USERNAME", "admin")
    os.environ.setdefault("ADMIN_PASSWORD", DEMO_PASSWORD)
    os.environ.setdefault("ADMIN_NAME", "Администратор")
    os.environ.setdefault("FAMILY_NAME", "Наша семья")

    # Настоящая модель — если её положили в .env; иначе офлайн-режим.
    if not os.environ.get("LLM_API_KEY") and not os.environ.get("LLM_BASE_URL"):
        os.environ.setdefault("LLM_STUB", "1")

    return keys


# --- демо-данные ----------------------------------------------------------

def seed_demo():
    """Семья из трёх человек с недельной историей — чтобы экраны не были пустыми."""
    from app.core.auth import hash_password
    from app.core.db import session_scope
    from app.core.family import get_settings
    from app.core.models import ROLE_HEAD, ROLE_MEMBER, Family, User
    from app.modules.memory import service as memory
    from app.modules.memory.models import KIND_HEALTH, KIND_PREF, KIND_TASK
    from app.modules.nutrition import service as nutrition
    from app.modules.nutrition.vision import MealEstimate
    from app.modules.security import service as security
    from app.web.routes_invite import new_invite_code

    with session_scope() as db:
        if db.query(User).count():
            return None

        family = Family(name="Наша семья")
        db.add(family)
        db.flush()
        get_settings(db, family.id)

        head = User(family_id=family.id, username="marina", password_hash=hash_password(DEMO_PASSWORD),
                    display_name="Марина", relation="мама", role=ROLE_HEAD, avatar_slot=0, autonomy=2)
        son = User(family_id=family.id, username="leva", password_hash=hash_password(DEMO_PASSWORD),
                   display_name="Лёва", relation="сын", role=ROLE_MEMBER, avatar_slot=1, autonomy=1)
        daughter = User(family_id=family.id, username="sonya", display_name="Соня", relation="дочь",
                        role=ROLE_MEMBER, avatar_slot=2, autonomy=1, invite_code=new_invite_code())
        db.add_all([head, son, daughter])
        db.flush()

        _seed_week(db, head.id, nutrition)
        _seed_notes(db, head.id, memory, KIND_PREF, KIND_HEALTH, KIND_TASK)
        _seed_home(db, family.id, security)

        return {"family": family.name, "head": head.username, "invite": daughter.invite_code}


def _seed_week(db, user_id: int, nutrition):
    """Неделя питания и активности — чтобы график и статистика были живыми.

    Времена задаются местные и переводятся в UTC: в базе всё лежит в UTC, а
    завтрак должен выглядеть завтраком на любых часах.
    """
    from app.core.clock import local_now, to_utc
    from app.modules.nutrition.vision import MealEstimate

    menu = [
        ("Овсянка с ягодами", 380, 12, 9, 62),
        ("Кофе с молоком", 90, 4, 4, 9),
        ("Суп и салат", 430, 18, 16, 48),
        ("Куриная грудка с рисом", 610, 45, 12, 74),
        ("Творог с мёдом", 280, 26, 6, 30),
    ]
    now = local_now()
    for day_offset in range(6, -1, -1):
        day = now - timedelta(days=day_offset)
        for index, (title, kcal, protein, fat, carbs) in enumerate(menu[: 3 + day_offset % 2]):
            meal = nutrition.create_draft(
                db, user_id,
                MealEstimate(title=title, kcal=kcal, protein=protein, fat=fat, carbs=carbs,
                             portion="обычная порция"),
                eaten_at=to_utc(day.replace(hour=8 + index * 4, minute=20)),
            )
            # Вчерашние и более ранние записи человек уже подтвердил, сегодняшние — ещё нет.
            if day_offset:
                nutrition.confirm_meal(db, user_id, meal.id, {})

        nutrition.log_activity(db, user_id, "steps", 5200 + day_offset * 430,
                               happened_at=to_utc(day.replace(hour=21, minute=0)))
        if day_offset % 3 == 0:
            nutrition.log_activity(db, user_id, "workout", 45,
                                   happened_at=to_utc(day.replace(hour=19, minute=0)))


def _seed_notes(db, user_id: int, memory, kind_pref, kind_health, kind_task):
    memory.add_note(db, user_id, "Соня не ест грибы", kind=kind_pref, source="из разговора 3 августа")
    memory.add_note(db, user_id, "У Лёвы аллергия на арахис", kind=kind_health,
                    source="добавлено вручную")
    memory.add_note(db, user_id, "Купить корм коту", kind=kind_task, source="из разговора",
                    when_text="завтра", remind_at=datetime.utcnow() + timedelta(days=1))


def _seed_home(db, family_id: int, security):
    """Две камеры и несколько событий, включая одну настоящую ночную аномалию."""
    from app.core.clock import local_now, to_utc
    from app.core.events import SECURITY_ANOMALY, bus

    gate = security.get_or_create_camera(db, family_id, "gate", "Калитка")
    gate.hint = "Часто срабатывает на кошек — уведомляет только ночью"
    yard = security.get_or_create_camera(db, family_id, "yard", "Двор")
    yard.notify_enabled = False
    yard.hint = "Только лог: днём здесь всё время кто-то ходит"

    now = local_now()
    yesterday_night = (now - timedelta(days=1)).replace(hour=23, minute=14)
    night = security.record_event(db, family_id, gate, to_utc(yesterday_night),
                                  detected_class="person", confidence=0.86, area=5200)
    # Прогоняем ночное событие через шину, как это сделал бы домашний воркер:
    # так в демо видно весь путь — правила, отметка «уведомил семью», push.
    bus.publish(SECURITY_ANOMALY, {"event_id": night.id, "family_id": family_id,
                                   "camera_id": gate.id, "verdict": night.verdict})
    security.record_event(db, family_id, gate, to_utc(now.replace(hour=9, minute=40)),
                          detected_class="person", confidence=0.91, area=4800)   # утром — штатно
    security.record_event(db, family_id, yard, to_utc(now.replace(hour=14, minute=5)),
                          detected_class="car", confidence=0.78, area=9000)      # днём — штатно


# --- запуск ---------------------------------------------------------------

def banner(port: int, demo: dict, stub: bool):
    line = "─" * 64
    print(f"\n{line}")
    print("  Семейный ассистент — локальный запуск")
    print(line)
    print(f"  Панель:      http://127.0.0.1:{port}")
    print(f"  База:        {DB_FILE}  (SQLite)")
    if demo:
        print(f"  Семья:       «{demo['family']}»")
        print(f"  Вход:        {demo['head']} / {DEMO_PASSWORD}   (глава семьи)")
        print(f"               leva / {DEMO_PASSWORD}   (участник — увидит, что чужие цифры скрыты)")
        print(f"  Приглашение: http://127.0.0.1:{port}/invite/{demo['invite']}   (Соня задаст пароль)")
    if not demo:
        print(f"  Вход:        admin / {DEMO_PASSWORD}   (создан из ADMIN_* при первом старте)")
    if stub:
        print("  Агент:       офлайн-режим без модели — понимает простые фразы")
        print("               («съел суп и салат», «что было ночью», «запомни: …»)")
        print("               Настоящая модель — LLM_* в .env рядом с этим файлом")
    else:
        print(f"  Модель:      {os.environ.get('LLM_MODEL')} · {os.environ.get('LLM_BASE_URL')}")
    print(f"{line}\n")


def main():
    parser = argparse.ArgumentParser(description="Локальный запуск на SQLite")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--reset", action="store_true", help="удалить базу и медиа и начать заново")
    parser.add_argument("--no-demo", action="store_true", help="не заводить демо-семью")
    parser.add_argument("--no-reload", action="store_true", help="без перезапуска по изменению файлов")
    args = parser.parse_args()

    if args.reset:
        import shutil
        DB_FILE.unlink(missing_ok=True)
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)
        print("Локальные данные удалены — начинаем с чистого листа")

    prepare_environment(args.port)
    sys.path.insert(0, str(Path(__file__).parent))

    from app.core.config import settings
    from app.core.db import create_all
    from app.modules import load_modules

    load_modules()      # чтобы create_all увидел таблицы модулей
    create_all()

    demo = None if args.no_demo else seed_demo()
    banner(args.port, demo, settings.llm.stub)

    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port,
                reload=not args.no_reload, log_level="info")


if __name__ == "__main__":
    main()
