"""Dual Thrust 돌파매매(app/dual_thrust_paper_loop.py) 백테스트.

페이퍼봇과 동일한 규칙을 과거 데이터로 재현한다: 전날까지 3일간의 레인지로 오늘의
상단/하단선을 계산하고, 종가가 상단을 뚫으면 롱/하단을 뚫으면 숏, 반대 선을 뚫으면
즉시 반전(stop-and-reverse)한다. 페이퍼봇은 5분 간격으로 체크하지만, 여기서는 1시간봉
종가로 근사한다(돌파 시점의 정확한 슬리피지는 반영 안 됨 — 실측은 이보다 나쁠 수 있음).

주의: 선물 가격만 재현하고 펀딩비는 미반영(app/futures_data.py 참고) — 방향성 포지션을
8시간 이상 들고 가는 구조라 펀딩비 영향이 특히 클 수 있다.
"""
from __future__ import annotations

import argparse

import pandas as pd

from app.futures_data import fetch_perp_ohlcv

UNIVERSE = ["BTC", "ETH", "SOL"]
K1 = 0.5
K2 = 0.5
POSITION_SIZE_PCT = 0.3
FEE_PCT = 0.05  # 선물 편도 수수료 근사치


def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"


def _daily_bounds(hourly: pd.DataFrame) -> pd.DataFrame:
    """1시간봉을 일봉으로 뭉쳐서, 각 날짜의 상/하단선(buy_line/sell_line)을 계산한다."""
    daily = hourly.resample("1D").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    rows = []
    for i in range(3, len(daily)):
        window = daily.iloc[i - 3:i]  # 직전 3일
        hh, hc, lc, ll = window["High"].max(), window["Close"].max(), window["Close"].min(), window["Low"].min()
        range_val = max(hh - lc, hc - ll)
        today_open = daily["Open"].iloc[i]
        rows.append({
            "date": daily.index[i],
            "buy_line": today_open + K1 * range_val,
            "sell_line": today_open - K2 * range_val,
        })
    return pd.DataFrame(rows).set_index("date")


def _simulate(symbol: str, since: str, until: str | None, start_capital: float) -> dict:
    hourly = fetch_perp_ohlcv(symbol, "1h", since, until)
    bounds = _daily_bounds(hourly)
    if bounds.empty:
        return {"symbol": symbol, "trades": 0, "return_pct": 0.0, "max_dd_pct": 0.0}

    budget = start_capital * POSITION_SIZE_PCT
    cash = budget
    position: dict | None = None  # {"side": "long"/"short", "entry_price": float}
    equity_curve = []
    n_trades = 0

    for ts, row in hourly.iterrows():
        day = ts.normalize()
        if day not in bounds.index:
            continue
        buy_line, sell_line = bounds.loc[day, "buy_line"], bounds.loc[day, "sell_line"]
        price = row["Close"]

        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            should_exit = (position["side"] == "long" and price < sell_line) or \
                          (position["side"] == "short" and price > buy_line)
            if should_exit:
                pnl = budget * direction * (price / position["entry_price"] - 1) - budget * FEE_PCT / 100
                cash += pnl
                n_trades += 1
                position = None

        if position is None:
            if price > buy_line:
                position = {"side": "long", "entry_price": price}
                cash -= budget * FEE_PCT / 100
            elif price < sell_line:
                position = {"side": "short", "entry_price": price}
                cash -= budget * FEE_PCT / 100

        unrealized = 0.0
        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            unrealized = budget * direction * (price / position["entry_price"] - 1)
        equity_curve.append(cash + unrealized)

    equity = pd.Series(equity_curve, index=hourly.index[-len(equity_curve):])
    total_return_pct = (equity.iloc[-1] / budget - 1) * 100 if len(equity) else 0.0
    peak = equity.cummax()
    max_dd_pct = ((peak - equity) / peak).max() * 100 if len(equity) else 0.0
    bh_pct = (hourly["Close"].iloc[-1] / hourly["Close"].iloc[0] - 1) * 100
    return {
        "symbol": symbol, "trades": n_trades, "return_pct": total_return_pct,
        "max_dd_pct": max_dd_pct, "bh_pct": bh_pct,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dual Thrust 돌파매매 백테스트")
    parser.add_argument("--since", default="2023-01-01T00:00:00Z")
    parser.add_argument("--until", default=None)
    parser.add_argument("--start-capital", type=float, default=10_000.0)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    print(f"[Dual Thrust 백테스트] {args.since} ~ {args.until or 'now'}, K1={K1} K2={K2}\n")
    results = []
    for base in UNIVERSE:
        print(f"[{base}] 1시간봉 수집 중...")
        r = _simulate(_perp_symbol(base), args.since, args.until, args.start_capital)
        results.append(r)
        print(f"  {base}: 수익률 {r['return_pct']:+.2f}% (매수보유 {r['bh_pct']:+.2f}%, 초과 {r['return_pct']-r['bh_pct']:+.2f}%p), "
              f"최대낙폭 {r['max_dd_pct']:.1f}%, 매매 {r['trades']}건")

    avg_return = sum(r["return_pct"] for r in results) / len(results)
    avg_bh = sum(r["bh_pct"] for r in results) / len(results)
    print(f"\n[요약] {len(UNIVERSE)}종목 평균 수익률 {avg_return:+.2f}% (평균 매수보유 {avg_bh:+.2f}%, 초과 {avg_return-avg_bh:+.2f}%p)")


if __name__ == "__main__":
    run(parse_args())
