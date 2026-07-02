"""User-defined technical/fundamental indicator DSL — whitelist evaluator.

A node is a dict with exactly one key. The key selects an operator or base
indicator; the value is its argument (scalar, list of sub-nodes, or window).
Evaluation is recursive and whitelist-only — there is no eval/exec, so a
malformed DSL cannot execute arbitrary code.

Example — "5日线上穿20日线 且 量>5日均量2倍":
    {"and":[
      {"cross_up":[{"ma":5},{"ma":20}]},
      {"gt":[{"vol"},{"mul":[2,{"ma_vol":5}]}]}
    ]}
"""
import pandas as pd


class DSLError(Exception):
    """Malformed or non-whitelisted DSL."""


def _align(df, x):
    """Return x as a Series aligned to df.index (scalars broadcast)."""
    if isinstance(x, pd.Series):
        return x.reindex(df.index)
    return pd.Series([x] * len(df), index=df.index, dtype=float)


# ── base indicators: (df, fund, arg) -> Series ──

def _b_price(df, fund, col):
    if col in df.columns:
        return df[col].astype(float)
    raise DSLError("unknown price field: %r" % col)


def _b_ma(df, fund, n):
    return df["close"].rolling(int(n)).mean()


def _b_ema(df, fund, n):
    return df["close"].ewm(span=int(n), adjust=False).mean()


def _b_vol(df, fund, _):
    return df["volume"].astype(float)


def _b_mavol(df, fund, n):
    return df["volume"].rolling(int(n)).mean()


def _b_rsi(df, fund, n):
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(int(n)).mean()
    loss = (-delta.clip(upper=0)).rolling(int(n)).mean()
    rs = gain / loss.replace(0, pd.NA)
    return (100 - 100 / (1 + rs)).fillna(50)


def _b_kdj_k(df, fund, n):
    low = df["low"].rolling(int(n)).min()
    high = df["high"].rolling(int(n)).max()
    rsv = (df["close"] - low) / (high - low).replace(0, pd.NA) * 100
    return rsv.ewm(alpha=1 / 3, adjust=False).mean()


def _boll(sign):
    """Build a boll_mid/upper/lower builder. sign: 0 mid, +1 upper, -1 lower."""
    def fn(df, fund, args):
        n = int(args[0]) if isinstance(args, (list, tuple)) else int(args)
        k = float(args[1]) if isinstance(args, (list, tuple)) and len(args) > 1 else 2.0
        mid = df["close"].rolling(n).mean()
        std = df["close"].rolling(n).std()
        if sign == 0:
            return mid
        return mid + sign * k * std
    return fn


def _b_macd_dif(df, fund, args):
    f, s = int(args[0]), int(args[1])
    return df["close"].ewm(span=f, adjust=False).mean() - df["close"].ewm(span=s, adjust=False).mean()


def _b_macd_dea(df, fund, args):
    f, s, sig = int(args[0]), int(args[1]), int(args[2])
    dif = df["close"].ewm(span=f, adjust=False).mean() - df["close"].ewm(span=s, adjust=False).mean()
    return dif.ewm(span=sig, adjust=False).mean()


_FUNDS = {"pe", "pb", "roe", "revenue_growth", "market_cap", "dividend_yield"}

_BASE = {
    "price": _b_price, "ma": _b_ma, "ema": _b_ema, "vol": _b_vol, "ma_vol": _b_mavol,
    "rsi": _b_rsi, "kdj_k": _b_kdj_k,
    "boll_mid": _boll(0), "boll_upper": _boll(1), "boll_lower": _boll(-1),
    "macd_dif": _b_macd_dif, "macd_dea": _b_macd_dea,
}


# ── operators ──

def _cross_up(df, a, b):
    return (a > b) & (a.shift(1) <= b.shift(1))


def _cross_down(df, a, b):
    return (a < b) & (a.shift(1) >= b.shift(1))


def _to_int(node):
    """Extract an int window from a const node or raw number."""
    if isinstance(node, dict) and "const" in node:
        return int(node["const"])
    if isinstance(node, (int, float)):
        return int(node)
    raise DSLError("expected a window size, got %r" % (node,))


# operator -> (arity, fn). fn takes (df, *aligned_or_int_args).
_OPS = {
    # arithmetic
    "add": (2, lambda df, a, b: a + b),
    "sub": (2, lambda df, a, b: a - b),
    "mul": (2, lambda df, a, b: a * b),
    "div": (2, lambda df, a, b: a / b.replace(0, pd.NA)),
    # comparison -> bool
    "gt": (2, lambda df, a, b: a > b),
    "lt": (2, lambda df, a, b: a < b),
    "ge": (2, lambda df, a, b: a >= b),
    "le": (2, lambda df, a, b: a <= b),
    "eq": (2, lambda df, a, b: a == b),
    # logic
    "and": (2, lambda df, a, b: (a.astype(bool)) & (b.astype(bool))),
    "or": (2, lambda df, a, b: (a.astype(bool)) | (b.astype(bool))),
    "not": (1, lambda df, a: ~a.astype(bool)),
    # time-series events
    "cross_up": (2, _cross_up),
    "cross_down": (2, _cross_down),
    "highest": (2, lambda df, a, n: a.rolling(_to_int(n)).max()),
    "lowest": (2, lambda df, a, n: a.rolling(_to_int(n)).min()),
    "ref": (2, lambda df, a, n: a.shift(_to_int(n))),
}


def eval_node(node, df, fund):
    """Recursively evaluate a DSL node against df (OHLCV) + fund (dict)."""
    # Bare scalars (e.g. the `2` in {"mul":[2, {...}]}) are treated as constants.
    if isinstance(node, bool):
        return _align(df, node)
    if isinstance(node, (int, float)):
        return _align(df, node)
    if not isinstance(node, dict) or len(node) != 1:
        raise DSLError("invalid node: %r" % (node,))
    op, arg = next(iter(node.items()))

    if op == "const":
        return _align(df, arg)
    if op == "fund":
        if arg not in _FUNDS:
            raise DSLError("unknown fundamental: %r" % arg)
        v = fund.get(arg)
        return pd.Series([float("nan") if v in (None, "") else float(v)] * len(df), index=df.index)
    if op in _BASE:
        return _BASE[op](df, fund, arg)
    if op in _OPS:
        arity, fn = _OPS[op]
        if not isinstance(arg, list) or len(arg) != arity:
            raise DSLError("%s expects %d args, got %r" % (op, arity, arg))
        vals = [eval_node(a, df, fund) for a in arg]
        aligned = [_align(df, v) for v in vals]
        return fn(df, *aligned)
    raise DSLError("unknown operator: %r" % op)


def evaluate(dsl, df, fund=None):
    """Evaluate the top-level DSL node. Returns a pd.Series.

    Raises DSLError on any malformed/non-whitelisted node. ``df`` must have
    columns open/high/low/close/volume; ``fund`` is a dict of fundamentals.
    """
    if fund is None:
        fund = {}
    for col in ("close", "high", "low", "volume"):
        if col not in df.columns:
            raise DSLError("df missing column: %s" % col)
    return eval_node(dsl, df, fund)


def is_signal(dsl_result):
    """A signal is a boolean Series; a plot is numeric."""
    return str(dsl_result.dtype).startswith("bool")


def validate(dsl):
    """Dry-run a DSL against synthetic OHLCV + fundamentals. Returns True iff
    it evaluates without DSLError (i.e. only uses whitelisted ops/base)."""
    import numpy as np
    n = 60
    df = pd.DataFrame({
        "open": np.linspace(10, 15, n),
        "high": np.linspace(11, 16, n),
        "low": np.linspace(9, 14, n),
        "close": np.linspace(10, 15, n),
        "volume": np.linspace(1e6, 2e6, n),
    })
    fund = {k: 10.0 for k in _FUNDS}
    try:
        r = evaluate(dsl, df, fund)
        return isinstance(r, pd.Series)
    except DSLError:
        return False
