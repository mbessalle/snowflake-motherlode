"""The spending cap must hold before a call is made, without relying on Snowflake's delayed usage views."""
import pytest

from motherlode import cost
from motherlode.cost import BudgetExceeded, SpendGuard


def test_estimates_follow_the_published_rates():
    # 1M input + 1M output tokens of claude-sonnet-5 = 1.20 + 6.00 AI credits at 2.00 USD
    assert cost.complete_usd("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(14.40)
    assert cost.function_usd("AI_EXTRACT", 1_000_000) == pytest.approx(11.10)
    assert cost.function_usd("AI_CLASSIFY", 500_000) == pytest.approx(1.62)
    assert cost.parse_usd(1000) == pytest.approx(7.32) and cost.parse_usd(1000, "OCR") == pytest.approx(1.36)
    assert cost.warehouse_usd(1.5) == pytest.approx(3.00)
    assert cost.tokens("abcdefgh") == 2 and cost.tokens(9) == 3


def test_an_unknown_model_is_an_error_not_a_free_call():
    with pytest.raises(KeyError):
        cost.complete_usd("some-new-model", 1000, 1000)


def test_guard_allows_steps_until_the_cap_and_then_refuses(tmp_path):
    guard = SpendGuard(cap_usd=10.0, ledger_path=tmp_path / "ledger.jsonl")
    assert guard.authorise("parse", 4.0) == pytest.approx(6.0)
    assert guard.authorise("extract", 5.0) == pytest.approx(1.0)
    with pytest.raises(BudgetExceeded, match="Nothing was run"):
        guard.authorise("agent", 1.5)
    assert guard.spent_usd() == pytest.approx(9.0)          # the refused step left no trace
    assert [e["step"] for e in guard.entries()] == ["parse", "extract"]


def test_measured_amount_replaces_the_plan(tmp_path):
    guard = SpendGuard(cap_usd=10.0, ledger_path=tmp_path / "ledger.jsonl")
    guard.authorise("extract", 5.0)
    guard.record_actual("extract", 1.25)
    assert guard.spent_usd() == pytest.approx(1.25)
    assert guard.authorise("agent", 8.0) == pytest.approx(0.75)


def test_ledger_survives_a_new_process(tmp_path):
    path = tmp_path / "ledger.jsonl"
    SpendGuard(10.0, path).authorise("parse", 9.5)
    with pytest.raises(BudgetExceeded):
        SpendGuard(10.0, path).authorise("extract", 1.0)
