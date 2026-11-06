"""Derivative market features from the Delta ticker.

Funding and open interest tell us how *positioned* the crowd is. Extreme
funding means one side is paying the other to hold - a crowding signal that
tends to be contrarian at extremes. OI change shows whether a move is
backed by new money or just unwinding.
"""


def funding_features(funding_rate: float | None, funding_8h: float | None = None) -> dict:
    fr = float(funding_rate) if funding_rate is not None else 0.0
    fr8 = float(funding_8h) if funding_8h is not None else fr

    # funding_rate_8h is a ratio (e.g. 0.0006 = 0.06%). Score extremes only.
    pct = fr8 * 100.0
    if pct <= -0.10:
        score, label = 1.0, "extreme negative funding (crowded shorts) - bullish lean"
    elif pct <= -0.03:
        score, label = 0.5, "negative funding (moderately short-crowded) - slight bullish lean"
    elif pct >= 0.10:
        score, label = -1.0, "extreme positive funding (crowded longs) - bearish lean"
    elif pct >= 0.03:
        score, label = -0.5, "positive funding (moderately long-crowded) - slight bearish lean"
    else:
        score, label = 0.0, "funding neutral"

    return {"funding_rate": fr, "funding_8h": fr8, "funding_score": score, "funding_explanation": label}


def oi_features(oi_contracts: float | None, oi_change_usd_6h: float | None, close: float | None, oi_value_usd: float | None = None) -> dict:
    oi = float(oi_contracts) if oi_contracts is not None else 0.0
    chg6 = float(oi_change_usd_6h) if oi_change_usd_6h is not None else 0.0
    price = float(close) if close is not None else 0.0

    # Normalize the 6h OI change (% of notional).
    notional = float(oi_value_usd) if oi_value_usd else (oi * price)
    oi_change_pct = (chg6 / notional) * 100.0 if notional else 0.0

    if oi_change_pct >= 3.0:
        oi_score, oi_label = 1.0, "OI rising sharply (+%.1f%% 6h) - fresh money entering" % oi_change_pct
    elif oi_change_pct >= 1.0:
        oi_score, oi_label = 0.5, "OI rising (%.1f%% 6h)" % oi_change_pct
    elif oi_change_pct <= -3.0:
        oi_score, oi_label = -1.0, "OI falling sharply (%.1f%% 6h) - positions unwinding" % oi_change_pct
    elif oi_change_pct <= -1.0:
        oi_score, oi_label = -0.5, "OI falling (%.1f%% 6h)" % oi_change_pct
    else:
        oi_score, oi_label = 0.0, "OI flat"

    return {
        "oi_contracts": oi,
        "oi_value_usd": notional,
        "oi_change_pct_6h": oi_change_pct,
        "oi_score": oi_score,
        "oi_explanation": oi_label,
    }


def derivatives_features(ticker: dict) -> dict:
    funding = funding_features(ticker.get("funding_rate"), ticker.get("funding_rate_8h"))
    oi = oi_features(
        ticker.get("oi_contracts"),
        ticker.get("oi_change_usd_6h"),
        ticker.get("close"),
        ticker.get("oi_value_usd"),
    )
    return {**funding, **oi}