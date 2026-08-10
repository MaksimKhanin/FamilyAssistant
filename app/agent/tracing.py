"""Трейсы агента: что ушло в модель, что вернулось и во что это обошлось.

Экран «Трейсы агента» отвечает на вопросы, на которые логи не отвечают:
почему ассистент ответил именно так, какой системный промпт он на самом деле
видел, что вернул инструмент и сколько токенов стоил один ответ — в разбивке по
людям и по разговорам.

Устройство простое. `runtime.respond` открывает прогон (`AgentRun`), а всё, что
происходит внутри, дописывает к нему шаги (`AgentTraceStep`). Чтобы не тащить
запись через все слои руками, активный писарь лежит в `ContextVar`: клиент
модели находит его сам, включая обращения из инструментов (оценка блюда по фото,
разбор события) — они попадают в тот же прогон.

Запись никогда не мешает ассистенту работать: любая ошибка здесь гасится и
уходит в лог. Трейс — вещь полезная, но не та, ради которой стоит уронить ответ.
"""
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.models import AgentRun, AgentTraceStep, TraceSettings, User

logger = get_logger("tracing")

#: Пауза, после которой следующая реплика считается новым разговором.
SESSION_GAP = timedelta(minutes=30)

#: Сколько знаков одного поля переживает запись. Картинка в base64 — это мегабайты,
#: и в трейсе от неё нужен только факт, что она была.
VALUE_LIMIT = 4000

_active: ContextVar[Optional["Recorder"]] = ContextVar("agent_trace_recorder", default=None)


# --- настройки ------------------------------------------------------------

def get_settings(db: Session, family_id: int) -> TraceSettings:
    row = db.query(TraceSettings).filter(TraceSettings.family_id == family_id).one_or_none()
    if row is None:
        row = TraceSettings(family_id=family_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def enabled(db: Session, family_id: int) -> bool:
    try:
        return bool(get_settings(db, family_id).enabled)
    except Exception as e:                                    # noqa: BLE001 — см. модуль
        logger.warning(f"Не удалось прочитать настройки трейсов: {e}")
        return False


# --- запись ---------------------------------------------------------------

def _trim(value: Any) -> Any:
    """Обрезает всё длинное, сохраняя форму: словарь остаётся словарём."""
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim(v) for v in value]
    if isinstance(value, str) and len(value) > VALUE_LIMIT:
        return value[:VALUE_LIMIT] + f"… (обрезано, всего {len(value)} знаков)"
    return value


def _dump(value: Any) -> str:
    try:
        return json.dumps(_trim(value), ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError) as e:
        return json.dumps({"не удалось записать": str(e)}, ensure_ascii=False)


class Recorder:
    """Писарь одного прогона. Живёт ровно столько, сколько идёт ответ."""

    def __init__(self, db: Session, run: AgentRun):
        self.db = db
        self.run = run
        self._steps = 0

    def _add(self, kind: str, name: str, request: Any, response: Any,
             status: str = "ok", usage: Dict[str, int] = None, duration_ms: int = 0):
        usage = usage or {}
        self._steps += 1
        step = AgentTraceStep(
            run_id=self.run.id,
            step_no=self._steps,
            kind=kind,
            name=(name or "?")[:64],
            status=status,
            request_json=_dump(request),
            response_json=_dump(response),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            duration_ms=duration_ms,
        )
        self.db.add(step)

        self.run.prompt_tokens += step.prompt_tokens
        self.run.completion_tokens += step.completion_tokens
        self.run.total_tokens += step.total_tokens
        if kind == "llm":
            self.run.llm_calls += 1
        else:
            self.run.tool_calls += 1
        self.db.commit()

    # Ошибка записи не должна стоить человеку ответа — отсюда широкий except.
    def llm(self, request: dict, response: Any, status: str = "ok",
            usage: Dict[str, int] = None, duration_ms: int = 0):
        try:
            self._add("llm", (request or {}).get("model", "?"), request, response,
                      status=status, usage=usage, duration_ms=duration_ms)
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"Не удалось записать трейс обращения к модели: {e}")

    def tool(self, name: str, arguments: Any, result: Any,
             status: str = "ok", duration_ms: int = 0):
        try:
            self._add("tool", name, arguments, result, status=status, duration_ms=duration_ms)
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"Не удалось записать трейс инструмента {name}: {e}")


def current() -> Optional[Recorder]:
    """Писарь текущего прогона, если запись включена."""
    return _active.get()


def _session_id(db: Session, user_id: int) -> str:
    """Тот же разговор, если перерыв меньше `SESSION_GAP`, иначе новый."""
    previous = (
        db.query(AgentRun)
        .filter(AgentRun.user_id == user_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .first()
    )
    if previous is not None and datetime.utcnow() - previous.created_at < SESSION_GAP:
        return previous.session_id
    return uuid.uuid4().hex[:12]


def _forget_old(db: Session, family_id: int, keep: int):
    """Оставляет только последние `keep` прогонов семьи: трейсы весят много."""
    own = db.query(AgentRun.id).join(User, User.id == AgentRun.user_id).filter(User.family_id == family_id)
    total = own.count()
    if total <= keep:
        return
    doomed = own.order_by(AgentRun.created_at.asc(), AgentRun.id.asc()).limit(total - keep).all()
    db.query(AgentTraceStep).filter(AgentTraceStep.run_id.in_([d.id for d in doomed])).delete(
        synchronize_session=False)
    db.query(AgentRun).filter(AgentRun.id.in_([d.id for d in doomed])).delete(synchronize_session=False)
    db.commit()


@contextmanager
def run(db: Session, actor: User, subject: User, channel: str, trigger: str):
    """Открывает прогон и делает его писаря активным на время ответа.

    Если запись выключена или что-то пошло не так — отдаёт `None`, и все, кто
    спрашивает `current()`, просто ничего не пишут.
    """
    if not enabled(db, actor.family_id):
        yield None
        return

    started = time.monotonic()
    try:
        row = AgentRun(
            session_id=_session_id(db, actor.id),
            user_id=actor.id,
            subject_id=subject.id if subject else None,
            channel=channel,
            trigger=(trigger or "")[:VALUE_LIMIT],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    except Exception as e:                                    # noqa: BLE001
        logger.warning(f"Не удалось открыть трейс: {e}")
        yield None
        return

    recorder = Recorder(db, row)
    token = _active.set(recorder)
    try:
        yield recorder
    except Exception as e:
        _finish(db, row, started, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        _active.reset(token)
        if row.duration_ms == 0:
            _finish(db, row, started)
        try:
            _forget_old(db, actor.family_id, get_settings(db, actor.family_id).keep_runs)
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"Не удалось подчистить старые трейсы: {e}")


def _finish(db: Session, row: AgentRun, started: float, error: str = None):
    try:
        row.duration_ms = max(1, int((time.monotonic() - started) * 1000))
        if error:
            row.error = error[:VALUE_LIMIT]
        db.commit()
    except Exception as e:                                    # noqa: BLE001
        logger.warning(f"Не удалось закрыть трейс: {e}")


def finish(reply: str):
    """Записать в текущий прогон, чем он закончился."""
    recorder = current()
    if recorder is None:
        return
    try:
        recorder.run.reply = (reply or "")[:VALUE_LIMIT]
        recorder.db.commit()
    except Exception as e:                                    # noqa: BLE001
        logger.warning(f"Не удалось дописать ответ в трейс: {e}")


# --- чтение ---------------------------------------------------------------

def runs(db: Session, limit: int = 50, user_id: int = None, session_id: str = None) -> List[AgentRun]:
    query = db.query(AgentRun)
    if user_id:
        query = query.filter(AgentRun.user_id == user_id)
    if session_id:
        query = query.filter(AgentRun.session_id == session_id)
    return query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit).all()


def by_user(db: Session, family_id: int) -> List[dict]:
    """Сводка по людям: сколько прогонов и токенов на каждого."""
    rows = (
        db.query(AgentRun, User)
        .join(User, User.id == AgentRun.user_id)
        .filter(User.family_id == family_id)
        .all()
    )
    totals: Dict[int, dict] = {}
    for run_row, user in rows:
        entry = totals.setdefault(user.id, {
            "user_id": user.id, "name": user.display_name, "runs": 0,
            "sessions": set(), "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        })
        entry["runs"] += 1
        entry["sessions"].add(run_row.session_id)
        entry["prompt_tokens"] += run_row.prompt_tokens
        entry["completion_tokens"] += run_row.completion_tokens
        entry["total_tokens"] += run_row.total_tokens

    result = [dict(entry, sessions=len(entry["sessions"])) for entry in totals.values()]
    return sorted(result, key=lambda e: e["total_tokens"], reverse=True)


def by_session(db: Session, family_id: int, limit: int = 30) -> List[dict]:
    """Сводка по разговорам, свежие сверху."""
    rows = (
        db.query(AgentRun, User)
        .join(User, User.id == AgentRun.user_id)
        .filter(User.family_id == family_id)
        .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
        .all()
    )
    sessions: Dict[str, dict] = {}
    for run_row, user in rows:
        entry = sessions.setdefault(run_row.session_id, {
            "session_id": run_row.session_id, "name": user.display_name,
            "user_id": user.id, "channel": run_row.channel,
            "started_at": run_row.created_at, "finished_at": run_row.created_at,
            "runs": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        })
        entry["runs"] += 1
        entry["finished_at"] = run_row.created_at
        entry["prompt_tokens"] += run_row.prompt_tokens
        entry["completion_tokens"] += run_row.completion_tokens
        entry["total_tokens"] += run_row.total_tokens

    ordered = sorted(sessions.values(), key=lambda e: e["started_at"], reverse=True)
    return ordered[:limit]


def _step_payload(step: AgentTraceStep) -> dict:
    return {
        "step_no": step.step_no,
        "kind": step.kind,
        "name": step.name,
        "status": step.status,
        "at": step.created_at.isoformat(),
        "duration_ms": step.duration_ms,
        "tokens": {"prompt": step.prompt_tokens, "completion": step.completion_tokens,
                   "total": step.total_tokens},
        "request": _load(step.request_json),
        "response": _load(step.response_json),
    }


def _load(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def run_payload(db: Session, row: AgentRun, with_steps: bool = True) -> dict:
    user = db.get(User, row.user_id)
    payload = {
        "run_id": row.id,
        "session_id": row.session_id,
        "at": row.created_at.isoformat(),
        "channel": row.channel,
        "user": {"id": row.user_id, "name": user.display_name if user else "?"},
        "subject_id": row.subject_id,
        "trigger": row.trigger,
        "reply": row.reply,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "tokens": {"prompt": row.prompt_tokens, "completion": row.completion_tokens,
                   "total": row.total_tokens},
        "llm_calls": row.llm_calls,
        "tool_calls": row.tool_calls,
    }
    if with_steps:
        payload["steps"] = [_step_payload(s) for s in row.steps]
    return payload


def run_view(db: Session, row: AgentRun) -> dict:
    """То же для экрана: шаги готовым текстом, время — как есть, для фильтров Jinja."""
    payload = run_payload(db, row, with_steps=False)
    payload["at"] = row.created_at
    payload["steps"] = [{
        "step_no": step.step_no,
        "kind": step.kind,
        "name": step.name,
        "status": step.status,
        "duration_ms": step.duration_ms,
        "tokens": {"prompt": step.prompt_tokens, "completion": step.completion_tokens,
                   "total": step.total_tokens},
        "request_text": step.request_json or "",
        "response_text": step.response_json or "",
    } for step in row.steps]
    return payload


def export(db: Session, family_id: int, user_id: int = None, session_id: str = None,
           run_id: int = None, limit: int = 500) -> dict:
    """Всё, что показано на экране, — одним JSON-объектом."""
    query = (
        db.query(AgentRun)
        .join(User, User.id == AgentRun.user_id)
        .filter(User.family_id == family_id)
    )
    if user_id:
        query = query.filter(AgentRun.user_id == user_id)
    if session_id:
        query = query.filter(AgentRun.session_id == session_id)
    if run_id:
        query = query.filter(AgentRun.id == run_id)
    rows = query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit).all()

    return {
        "exported_at": datetime.utcnow().isoformat(),
        "filter": {"user_id": user_id, "session_id": session_id, "run_id": run_id},
        "by_user": by_user(db, family_id),
        "by_session": by_session(db, family_id, limit=limit),
        "runs": [run_payload(db, row) for row in rows],
    }


def clear(db: Session, family_id: int):
    """Стереть все трейсы семьи — вместе с промптами, которые в них лежат."""
    ids = [
        row.id for row in
        db.query(AgentRun.id).join(User, User.id == AgentRun.user_id)
        .filter(User.family_id == family_id).all()
    ]
    if not ids:
        return
    db.query(AgentTraceStep).filter(AgentTraceStep.run_id.in_(ids)).delete(synchronize_session=False)
    db.query(AgentRun).filter(AgentRun.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
