"""Autonomy policy: what the assistant may do by itself, for whom."""
from app.agent import policy, registry
from app.core.access import set_module_enabled
from app.core.models import MODE_ASK, MODE_AUTO, MODE_OFF


def test_read_only_tools_never_ask(db, head):
    """Reading data is not an action — there is nothing to confirm."""
    head.autonomy = 0
    db.commit()
    stats = registry.get("get_nutrition_stats")
    assert stats.auto_from == 0
    assert policy.resolve_mode(db, head, stats) == MODE_AUTO


def test_autonomy_slider_gates_writing_tools(db, head):
    remember = registry.get("remember")      # auto_from = 2

    head.autonomy = 1
    db.commit()
    assert policy.resolve_mode(db, head, remember) == MODE_ASK

    head.autonomy = 2
    db.commit()
    assert policy.resolve_mode(db, head, remember) == MODE_AUTO


def test_the_meal_draft_is_not_gated_twice(db, head):
    """У записи еды своё подтверждение — карточкой, с цифрами перед глазами.

    Если ещё и ползунок будет её придерживать, оценка не посчитается вовсе, и
    подтверждать человеку станет нечего: ассистент скажет «записал» без единой цифры.
    """
    head.autonomy = 0        # «всё спрашивает»
    db.commit()

    assert policy.resolve_mode(db, head, registry.get("log_meal")) == MODE_AUTO
    assert policy.resolve_mode(db, head, registry.get("confirm_meal")) == MODE_AUTO


def test_notifying_the_whole_family_needs_the_top_setting(db, head):
    notify = registry.get("notify_family")
    head.autonomy = 2
    db.commit()
    assert policy.resolve_mode(db, head, notify) == MODE_ASK

    head.autonomy = 3
    db.commit()
    assert policy.resolve_mode(db, head, notify) == MODE_AUTO


def test_wiping_a_month_asks_even_at_full_autonomy(db, head):
    """Одно «да» стирает здесь месяц истории — этого не отдают ползунку.

    Удаление одной записи на самом смелом положении проходит само (её видно в
    разговоре и легко записать заново), а чистку периода спрашивают всегда.
    """
    head.autonomy = 3
    db.commit()

    assert policy.resolve_mode(db, head, registry.get("delete_meal")) == MODE_AUTO
    assert policy.resolve_mode(db, head, registry.get("clear_nutrition_period")) == MODE_ASK


def test_explicit_override_beats_the_slider(db, head):
    head.autonomy = 3
    db.commit()
    policy.set_mode(db, head, "log_meal", MODE_ASK)
    assert policy.resolve_mode(db, head, registry.get("log_meal")) == MODE_ASK


def test_disabled_module_hides_its_tools_from_the_model(db, member):
    names = {spec.name for spec in policy.available_tools(db, member)}
    assert "log_meal" in names

    set_module_enabled(db, member.id, "nutrition", False)
    names = {spec.name for spec in policy.available_tools(db, member)}
    assert "log_meal" not in names
    assert "get_security_log" in names      # другой модуль остался


def test_tool_switched_off_disappears_too(db, head):
    policy.set_mode(db, head, "notify_family", MODE_OFF)
    names = {spec.name for spec in policy.available_tools(db, head)}
    assert "notify_family" not in names
