"""Indicator DSL evaluator — whitelist + correctness."""
import numpy as np
import pandas as pd
import pytest
from server.indicators.dsl import evaluate, validate, is_signal, DSLError


def _df():
    # flat then jump → ma5 crosses above ma20 after step 30
    close = np.array([10.0] * 30 + [15.0] * 30)
    return pd.DataFrame({
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": np.linspace(1e6, 3e6, 60),
    })


def test_ma_evaluates():
    s = evaluate({"ma": 5}, _df())
    assert len(s) == 60
    assert s.tail(10).notna().all()


def test_cross_up_is_signal_and_fires():
    s = evaluate({"cross_up": [{"ma": 5}, {"ma": 20}]}, _df())
    assert is_signal(s)
    assert bool(s.any())  # the jump causes a crossover


def test_and_combination_evaluates():
    dsl = {"and": [
        {"cross_up": [{"ma": 5}, {"ma": 20}]},
        {"gt": [{"vol": 0}, {"mul": [2, {"ma_vol": 5}]}]},
    ]}
    s = evaluate(dsl, _df())
    assert is_signal(s)


def test_plot_kind_is_numeric():
    s = evaluate({"macd_dif": [12, 26]}, _df())
    assert not is_signal(s)


def test_fundamental_broadcast():
    s = evaluate({"gt": [{"fund": "pe"}, {"const": 20}]}, _df(), {"pe": 30})
    assert is_signal(s) and bool(s.all())


def test_unknown_operator_rejected():
    with pytest.raises(DSLError):
        evaluate({"eval": "__import__('os').system('rm -rf /')"}, _df())


def test_validate_good():
    assert validate({"cross_up": [{"ma": 5}, {"ma": 20}]}) is True


def test_validate_bad():
    assert validate({"exec": ["malicious"]}) is False
