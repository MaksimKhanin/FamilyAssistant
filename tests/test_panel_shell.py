"""Каркас панели: инварианты, без которых ломаются переходы.

Переход в панели подменяет тело документа целиком (ADR-0001), а не перезагружает
страницу. Из этого следует правило, которое иначе нигде не записано и которое
невозможно поймать обычным тестом на ответ сервера: внутри тела документа не
должно быть ни одного скрипта.

Скрипт в теле выполняется заново при каждом переходе, в том же глобальном
контексте: `const` на втором заходе падает с SyntaxError и уносит с собой все
скрипты страницы, а `addEventListener` копится дубликатами. Ошибка при этом
целиком клиентская — сервер отдаёт ровно тот же HTML, что и раньше, поэтому
остальные тесты её не видят.
"""
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
BASE = TEMPLATES / "base.html"

#: Шаблоны верхнего уровня, которые каркас не используют: вход, приглашение,
#: страницы ошибок. Они живут своей жизнью и переходами не затрагиваются.
STANDALONE = {"login.html", "invite.html", "invite_expired.html", "not_found.html"}


def swapped_templates():
    """Всё, что оказывается внутри подменяемого тела документа."""
    return [path for path in sorted(TEMPLATES.rglob("*.html"))
            if path != BASE and path.name not in STANDALONE]


@pytest.mark.parametrize("template", swapped_templates(), ids=lambda p: p.name)
def test_no_scripts_inside_swapped_body(template):
    assert "<script" not in template.read_text(encoding="utf-8"), (
        f"{template.relative_to(TEMPLATES)} содержит <script>. Тело документа "
        f"подменяется при каждом переходе, и скрипт выполнится заново — "
        f"переносите код в app/static/app.js, данные для него — в data-атрибуты."
    )


def test_base_keeps_its_scripts_in_head():
    """В каркасе скрипты допустимы, но только в <head> — он не подменяется."""
    markup = BASE.read_text(encoding="utf-8")
    head, _, body = markup.partition("</head>")

    assert "<script" in head, "каркас должен подключать htmx и app.js"
    assert "<script" not in body, (
        "в <body> каркаса появился скрипт: он выполнится заново при каждом "
        "переходе. Место такому коду — в app/static/app.js."
    )
