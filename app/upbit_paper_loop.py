"""업비트(KRW 현물) BB+RSI 롱온리 전략 (페이퍼 트레이딩 전용)

app/freqtrade_paper_loop.py와 같은 로직(SMA(100) 추세 필터 + 볼린저 밴드 하단 반등 +
RSI 과매도 진입, 손절 후 쿨다운)을 쓰지만, 바이낸스 USDT 선물이 아니라 업비트 KRW 현물
시세로 돌린다. 업비트는 선물/숏이 없어서 롱온리로만 동작한다.

추세 필터+쿨다운 없이 처음 만들었을 때는 freqtrade_paper_loop.py의 개선 전과 같은 문제
(하락장에서도 무조건 매수 시도)가 있을 수 있어서, 같은 수정을 적용했다 — 업비트 자체
백테스트(app/upbit_bb_rsi_backtest.py)로 확인: 평균 +0.11%, MDD 최대 1.6%(추세 필터
적용 전 상태로는 백테스트 안 해봤지만 freqtrade와 같은 구조라 같은 문제가 있었을 것).

시세 조회는 공개 API라 API 키가 없어도 동작한다(ccxt.upbit 공개 엔드포인트).
실거래로 전환하려면 .env에 UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY를 채우고 주문 실행부를
추가해야 한다 — 지금은 페이퍼(가상 손익 추적)만 한다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import pandas_ta as ta

from app.momentum_state import append_equity_point, now_iso
from app.watchdog import run_with_timeout

UNIVERSE = ["BTC", "ETH", "XRP", "SOL", "DOGE"]  # 업비트 KRW 마켓에 전부 상장된 메이저 코인
START_CAPITAL_KRW = float(os.environ.get("UPBIT_START_CAPITAL_KRW", "100000"))  # 10만원
CHECK_INTERVAL_SECONDS = 60 * 5  # 5분마다 체크 (freqtrade_paper_loop와 동일 주기)
CYCLE_TIMEOUT_SECONDS = 300
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30
RSI_SELL_THRESHOLD = 70
BB_LENGTH = 20
BB_STD = 2.0
TREND_SMA_LENGTH = 100
COOLDOWN_HOURS = 1.0  # 손절 후 같은 심볼 재진입까지 대기 (freqtrade_paper_loop와 동일)
POSITION_SIZE_PCT = 0.2

STATE_PATH = os.environ.get(
    "UPBIT_PAPER_STATE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "upbit_paper_state.json"),
)


def _symbol(base: str) -> str:
    return f"{base}/KRW"


def _exchange() -> ccxt.upbit:
    # 공개 시세만 쓰므로 키가 없어도 동작한다 — 실거래 전환 시에만 apiKey/secret이 필요해진다.
    config = {"enableRateLimit": True}
    access_key = os.environ.get("UPBIT_ACCESS_KEY")
    secret_key = os.environ.get("UPBIT_SECRET_KEY")
    if access_key and secret_key:
        config["apiKey"] = access_key
        config["secret"] = secret_key
    return ccxt.upbit(config)


def _fetch_current_prices(exchange: ccxt.upbit) -> dict[str, float]:
    prices = {}
    want = {_symbol(b): b for b in UNIVERSE}
    try:
        tickers = exchange.fetch_tickers(list(want))
        for symbol, base in want.items():
            last = (tickers.get(symbol) or {}).get("last")
            if last:
                prices[base] = float(last)
    except Exception as exc:
        print(f"시세조회 실패: {exc}", flush=True)
    return prices


def _get_signals(exchange: ccxt.upbit) -> dict[str, str]:
    signals = {}
    for base in UNIVERSE:
        try:
            ohlcv = exchange.fetch_ohlcv(_symbol(base), timeframe="5m", limit=BB_LENGTH + TREND_SMA_LENGTH + 10)
            df = pd.DataFrame(ohlcv, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
            if len(df) < BB_LENGTH + TREND_SMA_LENGTH + 1:
                continue

            df["rsi"] = ta.rsi(df["Close"], length=RSI_PERIOD)
            df["trend_sma"] = ta.sma(df["Close"], length=TREND_SMA_LENGTH)
            bbands = ta.bbands(df["Close"], length=BB_LENGTH, std=BB_STD)
            df = pd.concat([df, bbands], axis=1)

            last = df.iloc[-2]
            curr = df.iloc[-1]

            # pandas-ta 버전에 따라 컬럼명이 BBL_20_2.0 이거나 BBL_20_2.0_2.0 이거나 달라서
            # 하드코딩하면 KeyError로 매 사이클 죽는다 — 접두어로 찾는다.
            lower_band = last[next(c for c in bbands.columns if c.startswith("BBL_"))]
            mid_band = last[next(c for c in bbands.columns if c.startswith("BBM_"))]

            # 진입 로직: 장기추세(SMA100) 위 + 볼린저 밴드 하단 터치 + RSI 과매도
            if curr["Close"] > curr["trend_sma"] and curr["Close"] < lower_band and curr["rsi"] < RSI_BUY_THRESHOLD:
                signals[base] = "buy"
            elif curr["Close"] > mid_band or curr["rsi"] > RSI_SELL_THRESHOLD:
                signals[base] = "sell"

        except Exception as exc:
            print(f"지표 계산 에러 ({base}): {exc}", flush=True)

    return signals


def run_cycle() -> None:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
    else:
        state = {"equity_krw": START_CAPITAL_KRW, "positions": {}, "cumulative_realized_pnl_krw": 0.0}

    exchange = _exchange()
    prices = _fetch_current_prices(exchange)
    if not prices:
        return

    signals = _get_signals(exchange)
    cooldown_until = state.setdefault("cooldown_until", {})

    # 1. 청산(Sell)
    for base, pos in list(state["positions"].items()):
        current_price = prices.get(base)
        if not current_price:
            continue
        pnl = pos["notional_krw"] * (current_price / pos["entry_price"] - 1)
        is_stop_loss = pnl < -pos["notional_krw"] * 0.05
        if signals.get(base) == "sell" or is_stop_loss:
            print(f"[{now_iso()}] 🔴 청산: {base} @ {current_price:,.0f} (손익: {pnl:+,.0f} KRW)")
            state["cumulative_realized_pnl_krw"] += pnl
            state["equity_krw"] += pos["notional_krw"] + pnl
            del state["positions"][base]
            if is_stop_loss:
                cooldown_until[base] = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)).isoformat()

    # 2. 진입(Buy) — 업비트는 숏이 없어 롱온리
    now = datetime.now(timezone.utc)
    for base, signal in signals.items():
        in_cooldown = base in cooldown_until and now < datetime.fromisoformat(cooldown_until[base])
        if signal == "buy" and base not in state["positions"] and not in_cooldown:
            current_price = prices.get(base)
            if not current_price:
                continue
            invest_amount = START_CAPITAL_KRW * POSITION_SIZE_PCT
            if state["equity_krw"] >= invest_amount:
                print(f"[{now_iso()}] 🟢 진입: {base} @ {current_price:,.0f} (투자금: {invest_amount:,.0f} KRW)")
                state["positions"][base] = {
                    "side": "long",
                    "entry_price": current_price,
                    "notional_krw": invest_amount,
                    "ts": now_iso(),
                }
                state["equity_krw"] -= invest_amount

    # 3. 평가금 업데이트
    unrealized_pnl = 0.0
    for base, pos in state["positions"].items():
        if prices.get(base):
            pnl = pos["notional_krw"] * (prices[base] / pos["entry_price"] - 1)
            pos["unrealized_pnl_krw"] = pnl
            unrealized_pnl += pnl

    total_equity = state["equity_krw"] + unrealized_pnl
    print(f"--- 🪙 업비트 봇 평가자산: ₩{total_equity:,.0f} (보유: {len(state['positions'])}종목) ---")
    append_equity_point(state, total_equity)

    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def main() -> None:
    print(f"업비트 BB+RSI 페이퍼 트레이딩 시작 (자본금: {START_CAPITAL_KRW:,.0f} KRW, 5분마다 체크)")
    while True:
        try:
            run_with_timeout(run_cycle, CYCLE_TIMEOUT_SECONDS, on_timeout=lambda: print("사이클 타임아웃, 다음으로 넘어감", flush=True))
        except Exception:
            import traceback
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
