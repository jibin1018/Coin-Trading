"""Freqtrade 스타일 볼린저 밴드 + RSI 역추세 전략 (페이퍼 트레이딩 전용)

Freqtrade 커뮤니티에서 가장 많이 쓰이는 기본 전략 중 하나인 
'Bollinger Band 반등 + RSI 과매도' 로직을 Coin-Trading 프레임워크에 이식했습니다.

로직:
1. 매수(Long) 진입: 현재가가 볼린저 밴드 하단(Lower Band)을 하향 돌파하고, RSI(14)가 30 이하일 때
2. 매도(Long 청산): 현재가가 볼린저 밴드 중단(Middle Band, SMA20)을 상향 돌파하거나 RSI가 70 이상일 때
3. (숏은 위험 관리를 위해 일단 제외하고 롱 온리로 모의투자 세팅)

실행 방법:
python -m app.freqtrade_paper_loop
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import pandas_ta as ta

from app.crypto_tick_state import load_state as load_tick_state
from app.futures_data import fetch_perp_ohlcv
from app.momentum_state import append_equity_point, log_event, now_iso
from app.watchdog import run_with_timeout

# 환경변수 및 설정
UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE"]  # 거래량이 많고 휩소가 적은 메이저 코인 5개로 제한
START_CAPITAL_USDT = float(os.environ.get("FREQTRADE_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 5  # 5분마다 체크 (5분봉 단타 스윙)
CYCLE_TIMEOUT_SECONDS = 300
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30
RSI_SELL_THRESHOLD = 70
BB_LENGTH = 20
BB_STD = 2.0
POSITION_SIZE_PCT = 0.2  # 자본의 20%씩 분산 투자

def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"

def _fetch_current_prices(exchange: ccxt.binance) -> dict[str, float]:
    prices = {}
    want = {_perp_symbol(b): b for b in UNIVERSE}
    try:
        tickers = exchange.fetch_tickers(list(want))
        for symbol, base in want.items():
            last = (tickers.get(symbol) or {}).get("last")
            if last:
                prices[base] = float(last)
    except Exception as exc:
        print(f"시세조회 실패: {exc}", flush=True)
    return prices

def _get_signals() -> dict[str, str]:
    """각 코인별 5분봉 기준 지표를 계산하여 'buy' 또는 'sell' 시그널 반환"""
    signals = {}
    since_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat() # 이틀치 5분봉
    
    for base in UNIVERSE:
        try:
            df = fetch_perp_ohlcv(_perp_symbol(base), "5m", since_iso, None)
            if len(df) < BB_LENGTH + 1:
                continue
            
            # pandas-ta 지표 계산
            df["rsi"] = ta.rsi(df["Close"], length=RSI_PERIOD)
            bbands = ta.bbands(df["Close"], length=BB_LENGTH, std=BB_STD)
            df = pd.concat([df, bbands], axis=1)
            
            # 마지막 확정된 캔들(이전 캔들)과 현재 캔들 확인
            last = df.iloc[-2]
            curr = df.iloc[-1]

            # pandas-ta 버전에 따라 컬럼명이 BBL_20_2.0 이거나 BBL_20_2.0_2.0 이거나 달라서
            # 하드코딩하면 KeyError로 매 사이클 죽는다 — 접두어로 찾는다.
            lower_band = last[next(c for c in bbands.columns if c.startswith("BBL_"))]
            mid_band = last[next(c for c in bbands.columns if c.startswith("BBM_"))]
            
            # 진입 로직: 종가가 볼린저 밴드 하단 터치 & RSI 과매도
            if curr["Close"] < lower_band and curr["rsi"] < RSI_BUY_THRESHOLD:
                signals[base] = "buy"
            # 청산 로직: 종가가 중앙선 회복 또는 RSI 과매수
            elif curr["Close"] > mid_band or curr["rsi"] > RSI_SELL_THRESHOLD:
                signals[base] = "sell"
                
        except Exception as exc:
            print(f"지표 계산 에러 ({base}): {exc}", flush=True)
            
    return signals

def run_cycle() -> None:
    # 기존 momentum_state.json과 충돌하지 않도록 별도 파일 사용
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "freqtrade_paper_state.json")
    
    import json
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            state = json.load(f)
    else:
        state = {"equity_usdt": START_CAPITAL_USDT, "positions": {}, "cumulative_realized_pnl_usdt": 0.0}

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    prices = _fetch_current_prices(exchange)
    
    if not prices:
        return

    signals = _get_signals()
    
    # 1. 청산 (Sell) 로직 처리
    for base, pos in list(state["positions"].items()):
        current_price = prices.get(base)
        if not current_price:
            continue
            
        pnl = pos["notional_usdt"] * (current_price / pos["entry_price"] - 1)
        
        # 수익 실현 또는 손절 시그널 발생 시
        if signals.get(base) == "sell" or pnl < -pos["notional_usdt"] * 0.05: # 5% 손절 라인
            print(f"[{now_iso()}] 🔴 청산: {base} @ {current_price:.4f} (손익: {pnl:+.2f} USDT)")
            state["cumulative_realized_pnl_usdt"] += pnl
            state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
            del state["positions"][base]

    # 2. 진입 (Buy) 로직 처리
    for base, signal in signals.items():
        if signal == "buy" and base not in state["positions"]:
            current_price = prices.get(base)
            if not current_price:
                continue
                
            invest_amount = START_CAPITAL_USDT * POSITION_SIZE_PCT
            if state["equity_usdt"] >= invest_amount: # 잔고 확인
                print(f"[{now_iso()}] 🟢 진입: {base} @ {current_price:.4f} (투자금: {invest_amount:.2f} USDT)")
                state["positions"][base] = {
                    "side": "long",
                    "entry_price": current_price,
                    "notional_usdt": invest_amount,
                    "ts": now_iso()
                }
                state["equity_usdt"] -= invest_amount  # 진입 원금만큼 가용잔고에서 차감

    # 3. 미실현 손익(평가금) 업데이트
    unrealized_pnl = 0
    for base, pos in state["positions"].items():
        if prices.get(base):
            pnl = pos["notional_usdt"] * (prices[base] / pos["entry_price"] - 1)
            pos["unrealized_pnl_usdt"] = pnl
            unrealized_pnl += pnl
            
    current_total_equity = state["equity_usdt"] + unrealized_pnl
    print(f"--- 현재 평가자산: {current_total_equity:,.2f} USDT (보유: {len(state['positions'])}종목) ---")
    append_equity_point(state, current_total_equity)

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def main() -> None:
    print(f"Freqtrade-style Paper Trading 시작 (자본금: {START_CAPITAL_USDT} USDT, 5분마다 체크)")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
