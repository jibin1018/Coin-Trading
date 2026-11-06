"""마하세븐 이평선 눌림목 — 국장/미장 포트폴리오 레벨 백테스트 (yfinance).

app/mach7_portfolio_backtest.py(코인용)와 완전히 같은 포트폴리오 엔진(하나의 계좌로 매일
청산→빈 슬롯 채우기→균등배분)을 국장/미장 주식에 적용한다. README 미해결 문제 2번
("Squeeze/Pullback도 종목별 독립 검증만 됨 — 포트폴리오 레벨 미검증")에 대한 답.

코인 버전은 레짐필터로 BTC를 고정 앵커로 쓰는데, 주식은 그런 단일 앵커가 없어서 유니버스
등가중 인덱스 자체의 200일선(app/kr_us_rotation_backtest.py와 동일 방식)으로 대체했다.

데이터는 HANTOO_TEST_KEY가 없으면 yfinance로 받는다(KR은 .KS 먼저, 없으면 .KQ).

실행: ROTATION_MARKET=KR python -m app.kr_us_mach7_backtest  (또는 US)
"""
from __future__ import annotations

import itertools
import os

import numpy as np
import pandas as pd
import pandas_ta as ta
import yfinance as yf

from app.mach7_indicators import add_mach7_ma_pullback

MARKET = os.environ.get("ROTATION_MARKET", "KR").upper()
SINCE = os.environ.get("ROTATION_SINCE", "2018-01-01")
UNTIL = os.environ.get("ROTATION_UNTIL") or None
MIN_BARS = 260

FEE_PCT = 0.1
EMA_FAST, EMA_MID, EMA_SLOW, EXIT_EMA, PULLBACK_WINDOW = 10, 50, 200, 50, 10
PCT_STOP = 0.08
RS_LOOKBACK = 90

TOP_NS = [3, 5, 8]
REGIMES = ["none", "self_sma200", "index_sma200"]
RS_RANKS = ["off", "on"]
EXIT_MODES = ["trail_ema", "rr2"]
STOP_MODES = ["swing", "pct"]


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
            # yfinance가 거의 모든 종목에 날짜 1~2개씩 NaN Close를 섞어 내보낸다 — 포트폴리오
            # 평가금 합산 중 NaN 하나가 들어가면 그 이후 전체 equity가 NaN으로 영구 오염되고,
            # _metrics의 "final > 0" 체크가 NaN에서 항상 False라 CAGR이 -100%로 나왔다.
            return hist[["Open", "High", "Low", "Close", "Volume"]].ffill()
    return pd.DataFrame()


def _prep(frame: pd.DataFrame) -> pd.DataFrame:
    out = add_mach7_ma_pullback(
        frame, ema_fast=EMA_FAST, ema_mid=EMA_MID, ema_slow=EMA_SLOW,
        pullback_window=PULLBACK_WINDOW, exit_ema=EXIT_EMA,
    )
    out["ATR14"] = ta.atr(out["High"], out["Low"], out["Close"], length=14)
    out["SMA200f"] = ta.sma(out["Close"], length=200)
    out["RET90"] = out["Close"].pct_change(RS_LOOKBACK)
    return out


def _metrics(equity: pd.Series) -> dict:
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-6)
    final = float(equity.iloc[-1])
    cagr = (final ** (1 / years) - 1) * 100 if final > 0 else -100.0
    peak = equity.cummax()
    mdd = float(((peak - equity) / peak).max() * 100)
    daily = equity.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe, "final": final}


def _run_portfolio(prepped: dict, dates: pd.DatetimeIndex, idx_ok: pd.Series,
                    rs_median: pd.Series, top_n: int, regime: str, rs_rank: str,
                    exit_mode: str, stop_mode: str) -> dict:
    cash = 1.0
    positions: dict[str, dict] = {}
    fee = FEE_PCT / 100
    eq_curve = []
    n_trades = 0
    exposure_days = 0

    for d in dates:
        for sym in list(positions):
            df = prepped[sym]
            if d not in df.index:
                continue
            row = df.loc[d]
            pos = positions[sym]
            price, low, high = float(row["Close"]), float(row["Low"]), float(row["High"])
            pos["trail_high"] = max(pos["trail_high"], high)
            stop = pos["stop"]
            exit_p = None
            if low <= stop:
                exit_p = min(stop, price)
            elif exit_mode == "rr2" and pos["tp"] is not None and high >= pos["tp"]:
                exit_p = pos["tp"]
            elif exit_mode == "trail_ema" and price < float(row["EMA_FAST"]):
                exit_p = price
            elif exit_mode == "rr2" and price < float(row["EMA_EXIT"]):
                exit_p = price
            if exit_p is not None:
                cash += pos["qty"] * exit_p * (1 - fee)
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
                if regime == "self_sma200" and not (row["Close"] > row["SMA200f"]):
                    continue
                if regime == "index_sma200" and not bool(idx_ok.get(d, False)):
                    continue
                rs = row["RET90"]
                if rs_rank == "on" and not (pd.notna(rs) and rs > rs_median.get(d, np.inf)):
                    continue
                cands.append((sym, rs if pd.notna(rs) else -np.inf, row))
            cands.sort(key=lambda x: x[1], reverse=True)

            mtm = cash + sum(
                positions[s]["qty"] * float(prepped[s].loc[d, "Close"])
                for s in positions if d in prepped[s].index
            )
            for sym, _, row in cands[:free]:
                price = float(row["Close"])
                stop = float(row["STOP_PRICE"]) if stop_mode == "swing" else price * (1 - PCT_STOP)
                if stop >= price:
                    continue
                alloc = min(mtm / top_n, cash)
                if alloc <= 0:
                    break
                qty = alloc * (1 - fee) / price
                cash -= alloc
                tp = price + (price - stop) * 2.0 if exit_mode == "rr2" else None
                positions[sym] = {"qty": qty, "entry": price, "stop": stop, "tp": tp, "trail_high": price}

        mtm = cash + sum(
            positions[s]["qty"] * float(prepped[s].loc[d, "Close"])
            for s in positions if d in prepped[s].index
        )
        eq_curve.append((d, mtm))
        if positions:
            exposure_days += 1

    equity = pd.Series([v for _, v in eq_curve], index=[t for t, _ in eq_curve])
    m = _metrics(equity)
    m["trades"] = n_trades
    m["exposure_pct"] = exposure_days / len(dates) * 100
    return m


def run() -> None:
    universe = _universe()
    print(f"[{MARKET}] {len(universe)}종목 로드 중... (yfinance)")
    prepped: dict[str, pd.DataFrame] = {}
    for ticker, suffixes in universe:
        fr = _fetch_ohlc(ticker, suffixes)
        if len(fr) >= MIN_BARS:
            prepped[ticker] = _prep(fr)
    print(f"유효 {len(prepped)}종목.")

    all_dates = sorted(set().union(*[set(df.index) for df in prepped.values()]))
    dates = pd.DatetimeIndex(all_dates)
    warm = dates[max(210, RS_LOOKBACK + 10):]
    print(f"거래일 {len(warm)}일 ({warm[0].date()} ~ {warm[-1].date()})")

    ret90 = pd.DataFrame({b: d["RET90"] for b, d in prepped.items()})
    rs_median = ret90.median(axis=1)

    # 유니버스 등가중 인덱스 — 코인 버전의 btc_sma200 대신 쓰는 레짐 앵커
    closes = pd.DataFrame({b: d["Close"] for b, d in prepped.items()}).reindex(dates)
    idx_series = (1 + closes.pct_change().mean(axis=1).fillna(0.0)).cumprod()
    idx_ok = idx_series > idx_series.rolling(200).mean()

    ew = pd.DataFrame({b: d["Close"] / d["Close"].reindex(warm).dropna().iloc[0]
                       for b, d in prepped.items() if d["Close"].reindex(warm).notna().any()}).reindex(warm)
    ew_bh = _metrics(ew.mean(axis=1).dropna())

    combos = list(itertools.product(TOP_NS, REGIMES, RS_RANKS, EXIT_MODES, STOP_MODES))
    print(f"조합 {len(combos)}개 실행...\n")
    rows = []
    for i, (top_n, regime, rs_rank, exit_mode, stop_mode) in enumerate(combos, 1):
        m = _run_portfolio(prepped, warm, idx_ok, rs_median, top_n, regime, rs_rank, exit_mode, stop_mode)
        m["key"] = f"n{top_n:<2} {regime:<14} rs:{rs_rank:<3} {exit_mode:<9} {stop_mode:<6}"
        rows.append(m)

    rows.sort(key=lambda r: r["cagr"], reverse=True)
    print("=" * 100)
    print(f"[{MARKET}] 포트폴리오 랭킹 (CAGR)")
    print(f"  벤치마크  유니버스 균등매수보유: CAGR {ew_bh['cagr']:+.1f}%  MDD {ew_bh['mdd']:.0f}%  Sharpe {ew_bh['sharpe']:.2f}")
    print("=" * 100)
    print(f"{'설정':<48} {'CAGR':>8} {'MDD':>6} {'Sharpe':>7} {'노출%':>6} {'매매':>5}")
    for r in rows[:20]:
        print(f"{r['key']:<48} {r['cagr']:>+7.1f}% {r['mdd']:>5.0f}% {r['sharpe']:>7.2f} {r['exposure_pct']:>5.0f}% {r['trades']:>5}")

    beat = [r for r in rows if r["cagr"] > ew_bh["cagr"]]
    print(f"\nEW-B&H 이긴 조합: {len(beat)}/{len(rows)}")
    b = rows[0]
    print(f"\n최고: {b['key'].strip()}")
    print(f"  CAGR {b['cagr']:+.1f}%, MDD {b['mdd']:.0f}%, Sharpe {b['sharpe']:.2f}, 노출 {b['exposure_pct']:.0f}%, {b['trades']}매매")
    print("\n주의: 배당 재투자 반영(조정종가), 생존편향 있음(현재 유니버스 고정).")


if __name__ == "__main__":
    run()
