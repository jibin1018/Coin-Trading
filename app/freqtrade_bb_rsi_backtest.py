"""볼린저밴드+RSI 역추세(app/freqtrade_paper_loop.py) 백테스트.

페이퍼봇과 동일한 규칙: 종가가 SMA(100) 위에 있고(추세 필터) BB(20,2) 하단 아래이면서
RSI(14)<30이면 롱 진입, BB 중단(SMA20) 회복 또는 RSI>70이면 청산, 그 외 -5% 강제손절.
손절 이후 같은 심볼은 COOLDOWN_BARS 동안 재진입하지 않는다. 5분봉 기준.

추세 필터 + 쿨다운을 추가한 이유: 원래는 추세 필터가 없어서 하락장에서도 "하단밴드+RSI
과매도"면 무조건 매수했다 — 계속 떨어지는 칼날을 받다가 손절, 재진입, 또 손절이 반복되며
3개월 백테스트에서 평균 -8.60%(매수보유 대비 -33.23%p) 순손실을 냈다. 평균회귀 전략은
범위장/상승장에서만 유효하다는 원칙을 적용해 하락 추세에서의 진입을 차단했다.

주의: 선물 가격만 재현하고 펀딩비는 미반영(app/futures_data.py 참고).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as ta

from app.futures_data import fetch_perp_ohlcv

UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30
RSI_SELL_THRESHOLD = 70
BB_LENGTH = 20
BB_STD = 2.0
TREND_SMA_LENGTH = 100
COOLDOWN_BARS = 12  # 5분봉 기준 1시간 — 손절 후 바로 같은 자리에서 다시 긁히는 걸 막음
POSITION_SIZE_PCT = 0.2
FEE_PCT = 0.05


def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"


def _simulate(symbol: str, since: str, until: str | None, start_capital: float) -> dict:
    df = fetch_perp_ohlcv(symbol, "5m", since, until)
    if len(df) < BB_LENGTH + RSI_PERIOD + TREND_SMA_LENGTH:
        return {"symbol": symbol, "trades": 0, "return_pct": 0.0, "max_dd_pct": 0.0}

    df["rsi"] = ta.rsi(df["Close"], length=RSI_PERIOD)
    df["trend_sma"] = ta.sma(df["Close"], length=TREND_SMA_LENGTH)
    bbands = ta.bbands(df["Close"], length=BB_LENGTH, std=BB_STD)
    df = pd.concat([df, bbands], axis=1).dropna()
    # pandas-ta 버전에 따라 컬럼명이 BBL_20_2.0 이거나 BBL_20_2.0_2.0 이거나 달라서 접두어로 찾는다
    lower_col = next(c for c in bbands.columns if c.startswith("BBL_"))
    mid_col = next(c for c in bbands.columns if c.startswith("BBM_"))

    budget = start_capital * POSITION_SIZE_PCT
    cash = budget
    entry_price: float | None = None
    cooldown_until: int = -1
    equity_curve = []
    n_trades = 0

    for i, (_, row) in enumerate(df.iterrows()):
        price = row["Close"]

        if entry_price is not None:
            pnl_pct = price / entry_price - 1
            exit_signal = price > row[mid_col] or row["rsi"] > RSI_SELL_THRESHOLD or pnl_pct < -0.05
            if exit_signal:
                pnl = budget * pnl_pct - budget * FEE_PCT / 100
                cash += pnl
                n_trades += 1
                entry_price = None
                if pnl_pct < -0.05:
                    cooldown_until = i + COOLDOWN_BARS

        if (
            entry_price is None and i >= cooldown_until
            and price > row["trend_sma"] and price < row[lower_col] and row["rsi"] < RSI_BUY_THRESHOLD
        ):
            entry_price = price
            cash -= budget * FEE_PCT / 100

        unrealized = budget * (price / entry_price - 1) if entry_price is not None else 0.0
        equity_curve.append(cash + unrealized)

    equity = pd.Series(equity_curve, index=df.index[-len(equity_curve):])
    total_return_pct = (equity.iloc[-1] / budget - 1) * 100 if len(equity) else 0.0
    peak = equity.cummax()
    max_dd_pct = ((peak - equity) / peak).max() * 100 if len(equity) else 0.0
    bh_pct = (df["Close"].iloc[-1] / df["Close"].iloc[0] - 1) * 100
    return {
        "symbol": symbol, "trades": n_trades, "return_pct": total_return_pct,
        "max_dd_pct": max_dd_pct, "bh_pct": bh_pct,
    }


def parse_args() -> argparse.Namespace:
    default_since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
    parser = argparse.ArgumentParser(description="볼린저밴드+RSI 역추세 백테스트")
    parser.add_argument("--since", default=default_since, help="5분봉이라 기간을 너무 길게 잡지 말 것(기본 최근 90일)")
    parser.add_argument("--until", default=None)
    parser.add_argument("--start-capital", type=float, default=10_000.0)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    print(f"[BB+RSI 역추세 백테스트] {args.since} ~ {args.until or 'now'}\n")
    results = []
    for base in UNIVERSE:
        print(f"[{base}] 5분봉 수집 중... (기간이 길면 시간이 걸립니다)")
        r = _simulate(_perp_symbol(base), args.since, args.until, args.start_capital)
        results.append(r)
        print(f"  {base}: 수익률 {r['return_pct']:+.2f}% (매수보유 {r['bh_pct']:+.2f}%, 초과 {r['return_pct']-r['bh_pct']:+.2f}%p), "
              f"최대낙폭 {r['max_dd_pct']:.1f}%, 매매 {r['trades']}건")

    avg_return = sum(r["return_pct"] for r in results) / len(results)
    avg_bh = sum(r["bh_pct"] for r in results) / len(results)
    print(f"\n[요약] {len(UNIVERSE)}종목 평균 수익률 {avg_return:+.2f}% (평균 매수보유 {avg_bh:+.2f}%, 초과 {avg_return-avg_bh:+.2f}%p)")


if __name__ == "__main__":
    run(parse_args())
