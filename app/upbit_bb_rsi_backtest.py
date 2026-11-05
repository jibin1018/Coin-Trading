"""업비트 BB+RSI 역추세(app/upbit_paper_loop.py) 백테스트.

app/freqtrade_bb_rsi_backtest.py와 같은 규칙(SMA(100) 추세 필터 + BB(20,2) 하단
+ RSI(14)<30 진입, 중단 회복/RSI>70 청산, -5% 강제손절, 손절 후 쿨다운)을 업비트
KRW 현물 시세로 재현한다. 업비트는 선물/숏이 없어 원래부터 롱온리다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import pandas_ta as ta

UNIVERSE = ["BTC", "ETH", "XRP", "SOL", "DOGE"]
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30
RSI_SELL_THRESHOLD = 70
BB_LENGTH = 20
BB_STD = 2.0
TREND_SMA_LENGTH = 100
COOLDOWN_BARS = 12  # 5분봉 기준 1시간
POSITION_SIZE_PCT = 0.2
FEE_PCT = 0.05


def _symbol(base: str) -> str:
    return f"{base}/KRW"


def _fetch_ohlcv(symbol: str, since_ms: int) -> pd.DataFrame:
    exchange = ccxt.upbit({"enableRateLimit": True})
    rows: list[list[float]] = []
    since = since_ms
    for _ in range(200):
        batch = exchange.fetch_ohlcv(symbol, timeframe="5m", since=since, limit=200)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 2:
            break
        since = batch[-1][0] + 1
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts")


def _simulate(symbol: str, since_ms: int, start_capital: float) -> dict:
    df = _fetch_ohlcv(symbol, since_ms)
    if len(df) < BB_LENGTH + RSI_PERIOD + TREND_SMA_LENGTH:
        return {"symbol": symbol, "trades": 0, "return_pct": 0.0, "max_dd_pct": 0.0, "bh_pct": 0.0}

    df["rsi"] = ta.rsi(df["Close"], length=RSI_PERIOD)
    df["trend_sma"] = ta.sma(df["Close"], length=TREND_SMA_LENGTH)
    bbands = ta.bbands(df["Close"], length=BB_LENGTH, std=BB_STD)
    df = pd.concat([df, bbands], axis=1).dropna()
    lower_col = next(c for c in bbands.columns if c.startswith("BBL_"))
    mid_col = next(c for c in bbands.columns if c.startswith("BBM_"))

    budget = start_capital * POSITION_SIZE_PCT
    cash = budget
    entry_price: float | None = None
    cooldown_until = -1
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
    parser = argparse.ArgumentParser(description="업비트 BB+RSI 역추세 백테스트")
    parser.add_argument("--days", type=int, default=90, help="5분봉이라 기간을 너무 길게 잡지 말 것(기본 최근 90일)")
    parser.add_argument("--start-capital", type=float, default=1_000_000.0)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
    print(f"[업비트 BB+RSI 백테스트] 최근 {args.days}일\n")
    results = []
    for base in UNIVERSE:
        print(f"[{base}] 5분봉 수집 중... (기간이 길면 시간이 걸립니다)")
        r = _simulate(_symbol(base), since_ms, args.start_capital)
        results.append(r)
        print(f"  {base}: 수익률 {r['return_pct']:+.2f}% (매수보유 {r['bh_pct']:+.2f}%, 초과 {r['return_pct']-r['bh_pct']:+.2f}%p), "
              f"최대낙폭 {r['max_dd_pct']:.1f}%, 매매 {r['trades']}건")

    avg_return = sum(r["return_pct"] for r in results) / len(results)
    avg_bh = sum(r["bh_pct"] for r in results) / len(results)
    print(f"\n[요약] {len(UNIVERSE)}종목 평균 수익률 {avg_return:+.2f}% (평균 매수보유 {avg_bh:+.2f}%, 초과 {avg_return-avg_bh:+.2f}%p)")


if __name__ == "__main__":
    run(parse_args())
