"""Один заслон на весь HTTP вместо проверки роли в каждом роуте.

Роутов в панели под сотню, и модули добавляют новые, ничего не зная о ролях.
Проверка «а можно ли сюда администратору» в каждом из них — это сотня мест, где
её можно забыть, и забытая означала бы открытый экран. Поэтому решение принимает
`app/core/roles.py`, а спрашивает его одно место — этот гейт.

Заодно он ловит и адрес, набранный руками: спрятать пункт в навигации мало,
экран должен не открываться и по прямой ссылке.
"""
from fastapi import Request
from fastapi.responses import RedirectResponse

from app.core import roles
from app.core.auth import session_user
from app.core.db import session_scope
from app.core.templating import render


async def role_gate(request: Request, call_next):
    path = request.url.path

    # Вход, выход, статика, ingest с камер — до роли им дела нет. Проверка стоит
    # первой: без неё администратор не смог бы даже выйти из панели.
    if roles.area_of(path, request.method) == roles.AREA_ANY:
        return await call_next(request)

    with session_scope() as db:
        user = session_user(request, db)
        if user is None:
            # Не вошёл — это не про роли: дальше сработает NotAuthenticatedException
            # и уведёт на /login. Гейт в чужую работу не лезет.
            return await call_next(request)

        home = roles.redirect_home(user, path)
        if home is not None:
            return RedirectResponse(home, status_code=303)

        if not roles.may_open(user, path, request.method):
            return _denied(request, db, user)

    return await call_next(request)


def _denied(request: Request, db, user):
    """Объяснить, почему экран не открылся, вместо голого 403.

    Экран рисуется в том же каркасе: человек видит свою навигацию и уходит
    оттуда одним нажатием, а не упирается в белую страницу с числом.
    """
    from app.web.context import screen_context

    context = screen_context(request, db, user, user,
                             title="Не ваш экран",
                             subtitle="У администратора и участника разные панели")
    context.update(reason=roles.denial_text(user), home=roles.home_for(user))
    return render(request, "denied.html", context, status_code=403)
