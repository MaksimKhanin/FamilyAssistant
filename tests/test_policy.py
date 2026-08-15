"""Autonomy policy: what the assistant may do by itself.

Обе ручки — самостоятельность и режим инструмента — семейные: их задаёт
администратор сразу для всех (ADR-0008). Личным остаётся только флаг модуля.
"""
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


def test_the_dials_are_shared_by_the_whole_family(db, member, other):
    """Самостоятельность и режимы — общие: администратор задаёт их на всех разом."""
    policy.set_autonomy(db, member.family_id, 3)
    remember = registry.get("remember")

    assert policy.resolve_mode(db, member, remember) == MODE_AUTO
    assert policy.resolve_mode(db, other, remember) == MODE_AUTO

    policy.set_mode(db, member.family_id, "remember", MODE_ASK)

    assert policy.resolve_mode(db, other, remember) == MODE_ASK
