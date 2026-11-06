"""Squeeze / Pullback Rebound — 국장/미장 포트폴리오 레벨 백테스트 (yfinance).

README 미해결 문제 2번("Squeeze/Pullback도 종목별 독립 검증만 됨")의 나머지 절반 — 마하세븐은
app/kr_us_mach7_backtest.py에서 이미 포트폴리오 레벨로 확인했고, 여기서는 Squeeze
(app/squeeze_indicators.py)와 Pullback Rebound(app/pullback_rebound_indicators.py)를
같은 포트폴리오 엔진(매일 청산→빈 슬롯 채우기→균등배분, app/mach7_portfolio_backtest.py와
같은 패턴)으로 검증한다.

둘 다 원래 종목별 독립 시뮬레이션은 손절이 없었다(EXIT_SIGNAL로만 청산) — 포트폴리오
레벨에서는 이번 세션에서 검증된 손실 최소화 도구(일일 손절 옵션, 계좌 킬스위치)를 같이
테스트할 수 있게 넣었다.

실행: ROTATION_MARKET=KR STRATEGY=squeeze python -m app.kr_us_squeeze_pullback_portfolio_backtest
      (STRATEGY: squeeze | pullback)
"""
from __future__ import annotations

import itertools
import os

import numpy as np
import pandas as pd
import yfinance as yf

from app.pullback_rebound_indicators import add_pullback_rebound_indicators
from app.squeeze_indicators import add_squeeze_indicators

MARKET = os.environ.get("ROTATION_MARKET", "KR").upper()
STRATEGY = os.environ.get("STRATEGY", "squeeze").lower()  # squeeze | pullback
SINCE = os.environ.get("ROTATION_SINCE", "2018-01-01")
UNTIL = os.environ.get("ROTATION_UNTIL") or None
MIN_BARS = 260
FEE_PCT = 0.1

TOP_NS = [3, 5, 8]
REGIMES = ["none", "index_sma200"]
STOP_MODES = [None, 0.08]  # None=손절 없음(원본과 동일), 0.08=-8% 손절
KILL_DD = float(os.environ.get("LAB_KILL_DD", "0")) or None
KILL_COOLDOWN_DAYS = 20


def _universe() -> list[tuple[str, tuple[str, ...]]]:
    if MARKET == "KR":
        from app.ema_cross_watchlist import STOCK_UNIVERSE
        return [(code, (".KS", ".KQ")) for code, _ in STOCK_UNIVERSE]
    from app.us_swing_search import STOCK_UNIVERSE
    return [(sym, ("",)) for sym, _, _ in STOCK_UNIVERSE]


def _fetch_ohlc(ticker: str, suffixes: tuple[str, ...]) -> pd.DataFrame:
    for suffix in suffixes:
        try:
            hist = yf.Ticker(f"{ticker}{suffix}").history(start=SINCE, end=UNTIL, auto_adjust=True)
        except Exception:
            continue
        if not hist.empty:
            hist.index = hist.index.tz_localize(None)
            return hist[["Open", "High", "Low", "Close", "Volume"]].ffill()  # NaN Close 전염 방지
    return pd.DataFrame()


def _prep(frame: pd.DataFrame) -> pd.DataFrame:
    if STRATEGY == "squeeze":
        return add_squeeze_indicators(frame)
    return add_pullback_rebound_indicators(frame)


def _metrics(equity: pd.Series) -> dict:
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-6)
    final = float(equity.iloc[-1])
    cagr = (final ** (1 / years) - 1) * 100 if final > 0 else -100.0
    peak = equity.cummax()
    mdd = float(((peak - equity) / peak).max() * 100)
    daily = equity.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe}


def _run_portfolio(prepped: dict, dates: pd.DatetimeIndex, idx_ok: pd.Series,
                    top_n: int, regime: str, stop_pct: float | None) -> dict:
    cash = 1.0
    positions: dict[str, dict] = {}
    fee = FEE_PCT / 100
    eq_curve = []
    n_trades = 0
    peak = 1.0
    halted_until = None

    for d in dates:
        if KILL_DD is not None and (halted_until is None or d > halted_until):
            mtm_now = cash + sum(
                positions[s]["qty"] * float(prepped[s].loc[d, "Close"])
                for s in positions if d in prepped[s].index
            )
            peak = max(peak, mtm_now)
            if 1 - mtm_now / peak >= KILL_DD and positions:
                for sym, pos in positions.items():
                    price = float(prepped[sym].loc[d, "Close"]) if d in prepped[sym].index else pos["entry"]
                    cash += pos["qty"] * price * (1 - fee)
                    n_trades += 1
                positions = {}
                halted_until = d + pd.Timedelta(days=KILL_COOLDOWN_DAYS)
                peak = cash

        if halted_until is not None and d <= halted_until:
            eq_curve.append((d, cash))
            continue

        for sym in list(positions):
            df = prepped[sym]
            if d not in df.index:
                continue
            row = df.loc[d]
            pos = positions[sym]
            price = float(row["Close"])
            exit_now = bool(row["EXIT_SIGNAL"])
            if stop_pct is not None and price <= pos["entry"] * (1 - stop_pct):
                exit_now = True
            if exit_now:
                cash += pos["qty"] * price * (1 - fee)
                del positions[sym]
                n_trades += 1

        free = top_n - len(positions)
        if free > 0:
            cands = []
            for sym, df in prepped.items():
                if sym in positions or d not in df.index:
                    continue
                row = df.loc[d]
                if not bool(row["ENTRY_SIGNAL"]):
                    continue
                if regime == "index_sma200" and not bool(idx_ok.get(d, False)):
                    continue
                cands.append((sym, row))

            mtm = cash + sum(
                positions[s]["qty"] * float(prepped[s].loc[d, "Close"])
                for s in positions if d in prepped[s].index
            )
            for sym, row in cands[:free]:
                price = float(row["Close"])
                alloc = min(mtm / top_n, cash)
                if alloc <= 0:
                    break
                qty = alloc * (1 - fee) / price
                cash -= alloc
                positions[sym] = {"qty": qty, "entry": price}

        mtm = cash + sum(
            positions[s]["qty"] * float(prepped[s].loc[d, "Close"])
            for s in positions if d in prepped[s].index
        )
        eq_curve.append((d, mtm))

    equity = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve])
    m = _metrics(equity)
    m["trades"] = n_trades
    return m


def run() -> None:
    universe = _universe()
    print(f"[{MARKET}/{STRATEGY}] {len(universe)}종목 로드 중... (yfinance)")
    prepped: dict[str, pd.DataFrame] = {}
    for ticker, suffixes in universe:
        fr = _fetch_ohlc(ticker, suffixes)
        if len(fr) >= MIN_BARS:
            prepped[ticker] = _prep(fr).dropna(subset=["ENTRY_SIGNAL"])
    print(f"유효 {len(prepped)}종목.")

    all_dates = sorted(set().union(*[set(df.index) for df in prepped.values()]))
    dates = pd.DatetimeIndex(all_dates)
    warm = dates[210:]
    print(f"거래일 {len(warm)}일 ({warm[0].date()} ~ {warm[-1].date()})")

    closes = pd.DataFrame({b: d["Close"] for b, d in prepped.items()}).reindex(dates)
    idx_series = (1 + closes.pct_change().mean(axis=1).fillna(0.0)).cumprod()
    idx_ok = idx_series > idx_series.rolling(200).mean()

    ew = pd.DataFrame({b: d["Close"] / d["Close"].reindex(warm).dropna().iloc[0]
                       for b, d in prepped.items() if d["Close"].reindex(warm).notna().any()}).reindex(warm)
    ew_bh = _metrics(ew.mean(axis=1).dropna())
    print(f"[벤치마크] 등가중 매수&보유: CAGR {ew_bh['cagr']:+.1f}%, MDD {ew_bh['mdd']:.0f}%, Sharpe {ew_bh['sharpe']:.2f}\n")

    rows = []
    for top_n, regime, stop_pct in itertools.product(TOP_NS, REGIMES, STOP_MODES):
        m = _run_portfolio(prepped, warm, idx_ok, top_n, regime, stop_pct)
        stop_label = f"{stop_pct*100:.0f}%손절" if stop_pct else "손절없음"
        m["key"] = f"top{top_n} {regime:<14} {stop_label}"
        rows.append(m)

    rows.sort(key=lambda r: r["cagr"], reverse=True)
    print(f"{'설정':<32} {'CAGR':>8} {'MDD':>6} {'Sharpe':>7} {'매매':>5}")
    for r in rows:
        print(f"{r['key']:<32} {r['cagr']:>+7.1f}% {r['mdd']:>5.0f}% {r['sharpe']:>7.2f} {r['trades']:>5}")

    beat = [r for r in rows if r["cagr"] > ew_bh["cagr"]]
    print(f"\nEW-B&H 이긴 조합: {len(beat)}/{len(rows)}")
    b = rows[0]
    print(f"\n최고: {b['key'].strip()} -> CAGR {b['cagr']:+.1f}%, MDD {b['mdd']:.0f}%, Sharpe {b['sharpe']:.2f}")


if __name__ == "__main__":
    run()
