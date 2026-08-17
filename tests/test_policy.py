"""Autonomy policy: what the assistant may do by itself.

У обеих ручек — самостоятельности и режима инструмента — два слоя: дом, который
задаёт администратор, и своё, которым человек его перебивает (ADR-0012). Флаг
модуля остался админским: сам себе человек область не открывает.
"""
import pytest

from app.agent import policy, registry
from app.core.access import set_module_enabled
from app.core.models import MODE_ASK, MODE_AUTO, MODE_OFF


def test_read_only_tools_never_ask(db, member):
    """Reading data is not an action — there is nothing to confirm."""
    policy.set_autonomy(db, member.family_id, 0)
    stats = registry.get("get_nutrition_stats")
    assert stats.auto_from == 0
    assert policy.resolve_mode(db, member, stats) == MODE_AUTO


def test_autonomy_slider_gates_writing_tools(db, member):
    remember = registry.get("remember")      # auto_from = 2

    policy.set_autonomy(db, member.family_id, 1)
    assert policy.resolve_mode(db, member, remember) == MODE_ASK

    policy.set_autonomy(db, member.family_id, 2)
    assert policy.resolve_mode(db, member, remember) == MODE_AUTO


def test_the_meal_draft_is_not_gated_twice(db, member):
    """У записи еды своё подтверждение — карточкой, с цифрами перед глазами.

    Если ещё и ползунок будет её придерживать, оценка не посчитается вовсе, и
    подтверждать человеку станет нечего: ассистент скажет «записал» без единой цифры.
    """
    policy.set_autonomy(db, member.family_id, 0)        # «всё спрашивает»

    assert policy.resolve_mode(db, member, registry.get("log_meal")) == MODE_AUTO
    assert policy.resolve_mode(db, member, registry.get("confirm_meal")) == MODE_AUTO


def test_notifying_the_whole_family_needs_the_top_setting(db, member):
    notify = registry.get("notify_family")
    policy.set_autonomy(db, member.family_id, 2)
    assert policy.resolve_mode(db, member, notify) == MODE_ASK

    policy.set_autonomy(db, member.family_id, 3)
    assert policy.resolve_mode(db, member, notify) == MODE_AUTO


def test_wiping_a_month_asks_even_at_full_autonomy(db, member):
    """Одно «да» стирает здесь месяц истории — этого не отдают ползунку.

    Удаление одной записи на самом смелом положении проходит само (её видно в
    разговоре и легко записать заново), а чистку периода спрашивают всегда.
    """
    policy.set_autonomy(db, member.family_id, 3)

    assert policy.resolve_mode(db, member, registry.get("delete_meal")) == MODE_AUTO
    assert policy.resolve_mode(db, member, registry.get("clear_nutrition_period")) == MODE_ASK


def test_explicit_override_beats_the_slider(db, member):
    policy.set_autonomy(db, member.family_id, 3)
    policy.set_mode(db, member.family_id, "log_meal", MODE_ASK)
    assert policy.resolve_mode(db, member, registry.get("log_meal")) == MODE_ASK


def test_disabled_module_hides_its_tools_from_the_model(db, other):
    names = {spec.name for spec in policy.available_tools(db, other)}
    assert "log_meal" in names

    set_module_enabled(db, other.id, "nutrition", False)
    names = {spec.name for spec in policy.available_tools(db, other)}
    assert "log_meal" not in names
    assert "get_security_log" in names      # другой модуль остался


def test_tool_switched_off_disappears_too(db, member):
    policy.set_mode(db, member.family_id, "notify_family", MODE_OFF)
    names = {spec.name for spec in policy.available_tools(db, member)}
    assert "notify_family" not in names


def test_the_family_dials_are_the_default_for_everyone(db, member, other):
    """Пока никто не крутил своего, обе ручки работают на всех одинаково."""
    policy.set_autonomy(db, member.family_id, 3)
    remember = registry.get("remember")

    assert policy.resolve_mode(db, member, remember) == MODE_AUTO
    assert policy.resolve_mode(db, other, remember) == MODE_AUTO

    policy.set_mode(db, member.family_id, "remember", MODE_ASK)

    assert policy.resolve_mode(db, other, remember) == MODE_ASK


# --- своё поверх общего (ADR-0012) ----------------------------------------

def test_own_autonomy_beats_the_family_one_and_only_for_its_owner(db, member, other):
    """Тот, кому переспрашивания мешают, снимает их себе, никого не трогая."""
    policy.set_autonomy(db, member.family_id, 0)          # дом: всё спрашивает
    remember = registry.get("remember")

    policy.set_own_autonomy(db, member, 3)

    assert policy.resolve_mode(db, member, remember) == MODE_AUTO
    assert policy.resolve_mode(db, other, remember) == MODE_ASK


def test_own_autonomy_tightens_as_readily_as_it_loosens(db, member):
    """Ручка личная, а не поблажка: ею и просят спрашивать почаще."""
    policy.set_autonomy(db, member.family_id, 3)
    policy.set_own_autonomy(db, member, 0)

    assert policy.resolve_mode(db, member, registry.get("remember")) == MODE_ASK


def test_dropping_the_own_autonomy_follows_the_house_again(db, member):
    """«Как у всех» — это отсутствие своей настройки, а не ещё одно значение.

    Разница видна, когда администратор передумает: тот, кто отказался от своей,
    поедет за домом дальше, а не застынет на том, что было в момент отказа.
    """
    policy.set_autonomy(db, member.family_id, 0)
    policy.set_own_autonomy(db, member, 3)
    policy.set_own_autonomy(db, member, None)

    assert policy.dials(db, member).follows_family
    policy.set_autonomy(db, member.family_id, 2)
    assert policy.dials(db, member).autonomy == 2


def test_own_tool_mode_beats_both_the_slider_and_the_family_exception(db, member):
    policy.set_autonomy(db, member.family_id, 3)
    policy.set_mode(db, member.family_id, "remember", MODE_AUTO)

    policy.set_own_mode(db, member, "remember", MODE_ASK)

    assert policy.resolve_mode(db, member, registry.get("remember")) == MODE_ASK


def test_a_family_exception_still_beats_the_personal_slider(db, member):
    """Точная настройка бьёт общую, чья бы она ни была.

    Иначе личный ползунок молча снимал бы исключение, выставленное на весь дом
    руками, — а его выставляли как раз потому, что ползунка мало.
    """
    policy.set_mode(db, member.family_id, "remember", MODE_ASK)
    policy.set_own_autonomy(db, member, 3)

    assert policy.resolve_mode(db, member, registry.get("remember")) == MODE_ASK


def test_what_the_administrator_switched_off_stays_off(db, member):
    """Единственный настоящий запрет в доме — и себе он не включается."""
    policy.set_mode(db, member.family_id, "notify_family", MODE_OFF)

    with pytest.raises(policy.LockedByFamily):
        policy.set_own_mode(db, member, "notify_family", MODE_AUTO)

    policy.set_own_autonomy(db, member, 3)
    assert policy.resolve_mode(db, member, registry.get("notify_family")) == MODE_OFF
    assert "notify_family" not in {s.name for s in policy.available_tools(db, member)}


def test_switching_a_tool_off_for_yourself_keeps_it_on_the_screen(db, member):
    """Выключенный себе инструмент пропадает у модели, но остаётся ручкой в профиле.

    Иначе включить его обратно стало бы нечем: на экране пусто, а сказать
    ассистенту — он о таком инструменте уже не знает.
    """
    policy.set_own_mode(db, member, "write_entry", MODE_OFF)

    assert "write_entry" not in {s.name for s in policy.available_tools(db, member)}
    assert "write_entry" in {row["spec"].name for row in policy.own_overview(db, member)}
    assert [row["spec"].name for row in policy.own_exceptions(db, member)] == ["write_entry"]


def test_the_personal_screen_does_not_offer_a_disabled_module(db, other):
    set_module_enabled(db, other.id, "nutrition", False)

    names = {row["spec"].name for row in policy.own_overview(db, other)}
    assert "log_meal" not in names
    assert "remember" in names


def test_an_unknown_tool_is_not_stored_as_a_personal_exception(db, member):
    with pytest.raises(ValueError):
        policy.set_own_mode(db, member, "не-инструмент", MODE_ASK)
