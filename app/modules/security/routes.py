"""Web screens for the security module: лента событий, карточка тревоги, камеры."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core import media
from app.core.access import is_module_enabled
from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.modules.security import service
from app.modules.security.models import Camera
from app.web.context import screen_context

router = APIRouter(prefix="/security", tags=["security"])

FILTERS = [("all", "Всё"), ("anomaly", "Только аномалии"), ("normal", "Штатные")]


def _module_off(request: Request, db: Session, current: User, viewed: User, title: str):
    context = screen_context(request, db, current, viewed, title=title,
                             subtitle="Модуль выключен для этого участника")
    return render(request, "security/disabled.html", context)


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


@router.get("/cameras", response_class=HTMLResponse)
def cameras_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not is_module_enabled(db, viewed.id, "security"):
        return _module_off(request, db, current, viewed, "Камеры")

    context = screen_context(request, db, current, viewed,
                             title="Камеры", subtitle="Какие камеры пишут в ленту, а какие — уведомляют")
    context["cameras"] = service.list_cameras(db, viewed.family_id)
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
    camera_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Latest stored frame from a camera, used as its tile preview."""
    camera = db.get(Camera, camera_id)
    if camera is None or camera.family_id != viewed.family_id:
        raise HTTPException(status_code=404)
    latest = next((e for e in service.list_events(db, viewed.family_id, days=30, limit=200)
                   if e.camera_id == camera_id and e.snapshot_path), None)
    if latest is None or not media.is_inside_media_root(latest.snapshot_path):
        raise HTTPException(status_code=404)
    return FileResponse(latest.snapshot_path, media_type="image/jpeg")
