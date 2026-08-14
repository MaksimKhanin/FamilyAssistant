"""Кому какой экран открыт — правило, записанное один раз (app/core/roles.py).

Тест держится за само правило, а не за роуты: его читают и навигация, и заслон,
и стоит ему разъехаться с адресами — участник увидит админский экран или наоборот.
Умолчание проверяется отдельно: новый экран обязан оказаться участниковым, иначе
модуль, добавивший страницу, случайно спрятал бы её от семьи.
"""
from app.core import roles


class FakeUser:
    def __init__(self, is_admin: bool):
        self.is_admin = is_admin


ADMIN = FakeUser(True)
MEMBER = FakeUser(False)


def test_an_unknown_screen_belongs_to_the_family():
    """Умолчание — участниковое: модуль заводит экран для людей, а не для админа."""
    assert roles.area_of("/finance/budget") == roles.AREA_MEMBER
    assert roles.may_open(MEMBER, "/finance/budget")
    assert not roles.may_open(ADMIN, "/finance/budget")


def test_the_admin_area_is_named_by_address():
    for path in ("/settings/accounts", "/settings/agent", "/settings/model",
                 "/settings/traces", "/security/cameras", "/onboarding"):
        assert roles.area_of(path) == roles.AREA_ADMIN, path
        assert roles.may_open(ADMIN, path)
        assert not roles.may_open(MEMBER, path)


def test_nested_addresses_follow_their_screen():
    """Гейт смотрит на префикс: кнопка на админском экране остаётся админской."""
    assert roles.area_of("/settings/accounts/member/3/delete", "POST") == roles.AREA_ADMIN
    assert roles.area_of("/settings/traces/export.json") == roles.AREA_ADMIN


def test_the_way_in_and_out_belongs_to_nobody():
    """Иначе администратор не смог бы даже выйти: /logout — не его экран."""
    for path in ("/login", "/logout", "/invite/abc", "/static/style.css",
                 "/api/security/media", "/healthz"):
        assert roles.area_of(path) == roles.AREA_ANY, path
        assert roles.may_open(ADMIN, path) and roles.may_open(MEMBER, path)


def test_the_profile_is_shared_but_only_the_password_and_the_theme():
    assert roles.may_open(ADMIN, "/settings/profile")
    assert roles.may_open(ADMIN, "/settings/profile/password", "POST")
    assert roles.may_open(ADMIN, "/settings/profile/theme", "POST")

    # Личное на том же экране остаётся участниковым — по адресу, а не по флагу.
    assert not roles.may_open(ADMIN, "/settings/profile/character", "POST")
    assert not roles.may_open(ADMIN, "/settings/profile/memo/nutrition", "POST")
    assert not roles.may_open(ADMIN, "/settings/profile", "POST")


def test_each_role_has_its_own_home():
    assert roles.home_for(MEMBER) == "/"
    assert roles.home_for(ADMIN) == "/settings/accounts"

    # Админа с чужой «главной» уводят, а не показывают ему заглушку: по этому
    # адресу приходят и после входа, и с иконки на телефоне.
    assert roles.redirect_home(ADMIN, "/") == "/settings/accounts"
    assert roles.redirect_home(MEMBER, "/") is None
    assert roles.redirect_home(ADMIN, "/memory") is None
