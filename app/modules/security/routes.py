"""Web screens for the security module: лента событий, карточка тревоги, камеры, архив."""
import mimetypes
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.core import media
from app.core.access import is_module_enabled
from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.modules.security import control, service
from app.modules.security.models import Camera
from app.web.context import screen_context

router = APIRouter(prefix="/security", tags=["security"])

FILTERS = [("all", "Всё"), ("anomaly", "Только аномалии"), ("normal", "Штатные")]
ARCHIVE_FILTERS = [("all", "Всё"), ("alerts", "Только срабатывания")]

STREAM_CHUNK = 256 * 1024


def _serve(path: Path, media_type: str, request: Request) -> Response:
    """Отдать файл, понимая заголовок Range.

    `FileResponse` из Starlette его не умеет: он всегда отдаёт файл целиком с
    начала. Для картинки это неважно, а для видео означает, что перемотки нет —
    браузеру приходится тянуть всю запись, чтобы показать её середину.
    """
    size = path.stat().st_size
    raw = request.headers.get("range", "")
    if not raw.startswith("bytes="):
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})

    start_text, _, end_text = raw[len("bytes="):].partition("-")
    try:
        if not start_text:                        # bytes=-500 — «последние 500 байт»
            start, end = max(0, size - int(end_text)), size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def chunks():
        remaining = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                data = f.read(min(STREAM_CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(chunks(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
    })


def _module_off(request: Request, db: Session, current: User, viewed: User, title: str):
    context = screen_context(request, db, current, viewed, title=title,
                             subtitle="Модуль выключен для этого участника")
    return render(request, "security/disabled.html", context)


def _media_file(request: Request, db: Session, viewed: User, media_id: int,
                thumb: bool = False) -> Response:
    """Общая проверка для отдачи файла: модуль включён, семья своя, файл внутри MEDIA_ROOT."""
    if not is_module_enabled(db, viewed.id, "security"):
        raise HTTPException(status_code=404)
    item = service.get_media(db, viewed.family_id, media_id)
    if item is None:
        raise HTTPException(status_code=404)

    rel = item.thumb_rel_path if thumb and item.thumb_rel_path else item.rel_path
    # Превью может не сделаться (битый чанк) — тогда для картинки честнее отдать
    # оригинал, чем дырку в сетке. Для видео оригинал в <img> не годится.
    if thumb and not item.thumb_rel_path and item.is_video:
        raise HTTPException(status_code=404)

    path = media.resolve(rel)
    if not media.is_inside_media_root(str(path)) or not path.exists():
        raise HTTPException(status_code=404)

    media_type = "image/jpeg" if thumb else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    return _serve(path, media_type, request)


@router.get("/events", response_class=HTMLResponse)
def events_screen(
    request: Request,
    only: str = "all",
    event_id: int = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not is_module_enabled(db, viewed.id, "security"):
        return _module_off(request, db, current, viewed, "События")

    only = only if only in dict(FILTERS) else "all"
    events = service.list_events(db, viewed.family_id, only=only)
    cameras = {c.id: c for c in service.list_cameras(db, viewed.family_id)}

    context = screen_context(request, db, current, viewed,
                             title="События",
                             subtitle="Только то, на что стоит взглянуть — остальное просто в логе")
    context.update(
        events=events,
        cameras=cameras,
        filters=FILTERS,
        active_filter=only,
        alert=service.get_event(db, viewed.family_id, event_id) if event_id else None,
    )
    return render(request, "security/events.html", context)


@router.post("/events/{event_id}/ours")
def mark_ours(
    event_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """«Это свои, всё хорошо»."""
    service.mark_ours(db, viewed.family_id, event_id)
    return RedirectResponse("/security/events", status_code=303)


@router.get("/archive", response_class=HTMLResponse)
def archive_screen(
    request: Request,
    camera: str = None,
    only: str = "all",
    page: int = 1,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Всё, что приехало с камер: и тревожное, и штатная запись."""
    if not is_module_enabled(db, viewed.id, "security"):
        return _module_off(request, db, current, viewed, "Архив")

    only = only if only in dict(ARCHIVE_FILTERS) else "all"
    page = max(1, page)
    cameras = service.list_cameras(db, viewed.family_id)
    selected = next((c for c in cameras if c.slug == camera), None)

    items, has_more = service.list_media(
        db, viewed.family_id,
        camera_id=selected.id if selected else None,
        alerts_only=(only == "alerts"),
        page=page,
    )

    def page_url(target: int) -> str:
        params = {k: v for k, v in
                  (("camera", selected.slug if selected else None),
                   ("only", only if only != "all" else None),
                   ("page", target if target > 1 else None)) if v}
        return "/security/archive" + (f"?{urlencode(params)}" if params else "")

    context = screen_context(request, db, current, viewed,
                             title="Архив",
                             subtitle="Записи с камер — хранятся столько, сколько задано у камеры")
    context.update(
        items=items,
        cameras=cameras,
        camera_by_id={c.id: c for c in cameras},
        selected_camera=selected,
        archive_filters=ARCHIVE_FILTERS,
        active_filter=only,
        page=page,
        prev_url=page_url(page - 1) if page > 1 else None,
        next_url=page_url(page + 1) if has_more else None,
        base_url=page_url(1),
        stats=service.media_stats(db, viewed.family_id),
    )
    return render(request, "security/archive.html", context)


@router.get("/media/{media_id}", response_class=HTMLResponse)
def media_screen(
    request: Request,
    media_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not is_module_enabled(db, viewed.id, "security"):
        return _module_off(request, db, current, viewed, "Запись")

    item = service.get_media(db, viewed.family_id, media_id)
    if item is None:
        raise HTTPException(status_code=404)
    camera = db.get(Camera, item.camera_id)

    context = screen_context(request, db, current, viewed,
                             title=camera.label if camera else "Запись",
                             subtitle=item.filename)
    context.update(
        item=item,
        camera=camera,
        event=service.get_event(db, viewed.family_id, item.event_id) if item.event_id else None,
        exists=media.resolve(item.rel_path).exists(),
        active_path="/security/archive",   # страница файла — часть архива, пусть он и подсвечивается
    )
    return render(request, "security/media.html", context)


@router.get("/file/{media_id}")
def media_file(
    request: Request,
    media_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    return _media_file(request, db, viewed, media_id)


@router.get("/thumb/{media_id}")
def media_thumb(
    request: Request,
    media_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    return _media_file(request, db, viewed, media_id, thumb=True)


@router.get("/cameras", response_class=HTMLResponse)
def cameras_screen(
    request: Request,
    notice: str = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not is_module_enabled(db, viewed.id, "security"):
        return _module_off(request, db, current, viewed, "Камеры")

    context = screen_context(request, db, current, viewed,
                             title="Камеры", subtitle="Какие камеры пишут в ленту, а какие — уведомляют")
    context["cameras"] = service.list_cameras(db, viewed.family_id)
    context["control_ready"] = control.configured()
    context["notice"] = notice
    return render(request, "security/cameras.html", context)


@router.post("/cameras/{camera_id}/notify")
def toggle_camera(
    camera_id: int,
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    service.set_camera_notify(db, viewed.family_id, camera_id, enabled == "on")
    return RedirectResponse("/security/cameras", status_code=303)


@router.post("/control/{camera_id}/{action}")
async def camera_control(
    camera_id: int,
    action: str,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Нажатие кнопки на карточке камеры — уезжает рекордеру домой."""
    camera = db.get(Camera, camera_id)
    if camera is None or camera.family_id != viewed.family_id:
        raise HTTPException(status_code=404)

    _, message = await control.send(camera.slug, action)
    return RedirectResponse(f"/security/cameras?{urlencode({'notice': message})}", status_code=303)


@router.get("/snapshot/{event_id}")
def snapshot(
    event_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Serve a frame — only for this family's events, and only from inside MEDIA_ROOT."""
    if not is_module_enabled(db, viewed.id, "security"):
        raise HTTPException(status_code=404)
    event = service.get_event(db, viewed.family_id, event_id)
    if event is None or not event.snapshot_path or not media.is_inside_media_root(event.snapshot_path):
        raise HTTPException(status_code=404)
    return FileResponse(event.snapshot_path, media_type="image/jpeg")


@router.get("/camera-preview/{camera_id}")
def camera_preview(
    request: Request,
    camera_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Latest stored frame from a camera, used as its tile preview."""
    if not is_module_enabled(db, viewed.id, "security"):
        raise HTTPException(status_code=404)
    camera = db.get(Camera, camera_id)
    if camera is None or camera.family_id != viewed.family_id:
        raise HTTPException(status_code=404)

    latest = service.latest_media(db, viewed.family_id, camera_id)
    if latest is None:
        raise HTTPException(status_code=404)
    return _media_file(request, db, viewed, latest.id, thumb=bool(latest.thumb_rel_path))
