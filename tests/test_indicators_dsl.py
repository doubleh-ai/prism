"""Indicator DSL evaluator — whitelist + correctness."""

import numpy as np
import pandas as pd
import pytest

from server.indicators.dsl import DSLError, evaluate, is_signal, validate


def _df():
    # flat then jump → ma5 crosses above ma20 after step 30
    close = np.array([10.0] * 30 + [15.0] * 30)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.linspace(1e6, 3e6, 60),
        }
    )


def test_ma_evaluates():
    s = evaluate({"ma": 5}, _df())
    assert len(s) == 60
    assert s.tail(10).notna().all()


def test_cross_up_is_signal_and_fires():
    s = evaluate({"cross_up": [{"ma": 5}, {"ma": 20}]}, _df())
    assert is_signal(s)
    assert bool(s.any())  # the jump causes a crossover


def test_and_combination_evaluates():
    dsl = {
        "and": [
            {"cross_up": [{"ma": 5}, {"ma": 20}]},
            {"gt": [{"vol": 0}, {"mul": [2, {"ma_vol": 5}]}]},
        ]
    }
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


def test_parameterized_base_indicator_arguments():
    dsl = {"cross_up": [{"ma": {"param": ["fast", 5]}}, {"ma": {"param": ["slow", 20]}}]}
    s = evaluate(dsl, _df(), params={"fast": 5, "slow": 20})
    assert is_signal(s)
    assert bool(s.any())
    assert validate(dsl, {"fast": 5, "slow": 20}) is True


def test_extended_time_series_ops():
    df = _df()
    pct = evaluate({"pct_change": [{"price": "close"}, 5]}, df)
    assert pct.iloc[-1] == 0
    cnt = evaluate({"count": [{"gt": [{"price": "close"}, {"const": 12}]}, 10]}, df)
    assert cnt.iloc[-1] == 10
    recent = evaluate({"within": [{"cross_up": [{"ma": 5}, {"ma": 20}]}, 30]}, df)
    assert bool(recent.iloc[-1])


def _ma_stack_volume_dsl():
    return {
        "and": [
            {
                "and": [
                    {
                        "within": [
                            {"cross_up": [{"ma": {"param": ["fast", 5]}}, {"ma": {"param": ["mid", 10]}}]},
                            {"param": ["sync_window", 1]},
                        ]
                    },
                    {
                        "within": [
                            {"cross_up": [{"ma": {"param": ["mid", 10]}}, {"ma": {"param": ["slow", 20]}}]},
                            {"param": ["sync_window", 1]},
                        ]
                    },
                ]
            },
            {
                "gt": [
                    {"vol": 0},
                    {
                        "mul": [
                            {"param": ["multiplier", 3]},
                            {"ref": [{"ma_vol": {"param": ["baseline_window", 5]}}, 1]},
                        ]
                    },
                ]
            },
        ]
    }


def test_ma_stack_volume_combines_three_conditions_and_excludes_current_volume():
    close = np.array([10.0] * 30 + [30.0])
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.array([100.0] * 30 + [301.0]),
        }
    )
    params = {
        "fast": 5,
        "mid": 10,
        "slow": 20,
        "sync_window": 1,
        "baseline_window": 5,
        "multiplier": 3,
    }

    result = evaluate(_ma_stack_volume_dsl(), df, params=params)
    assert is_signal(result)
    assert bool(result.iloc[-1])
    assert validate(_ma_stack_volume_dsl(), params)

    df.loc[df.index[-1], "volume"] = 300.0
    strict_result = evaluate(_ma_stack_volume_dsl(), df, params=params)
    assert not bool(strict_result.iloc[-1])


def test_natural_language_composite_translation_keeps_all_three_conditions():
    from server.indicators.translator import _rule_translate

    translated = _rule_translate("5日线上穿10日线，10日线上穿20日线，日成交量大于前五日的3倍")
    assert translated is not None
    assert translated["name"] == "多级均线放量共振(5/10/20)"
    values = {item["key"]: item["value"] for item in translated["params"]["items"]}
    assert values == {
        "fast": 5,
        "mid": 10,
        "slow": 20,
        "sync_window": 1,
        "baseline_window": 5,
        "multiplier": 3.0,
    }
    assert translated["dsl"]["and"][0]["and"][0]["within"][0]["cross_up"]
    volume_rhs = translated["dsl"]["and"][1]["gt"][1]["mul"][1]
    assert volume_rhs["ref"][1] == 1
