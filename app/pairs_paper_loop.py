"""페어 트레이딩 (통계적 차익거래) 모의투자 봇

비트코인(BTC)과 이더리움(ETH)의 가격 스프레드(격차)가 통계적으로 벌어질 때
고평가된 것을 공매도(Short), 저평가된 것을 매수(Long)하여
격차가 원래대로 돌아올 때(Mean Reversion) 수익을 내는 시장 중립 전략입니다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd

from app.futures_data import fetch_perp_ohlcv
from app.momentum_state import now_iso

START_CAPITAL_USDT = float(os.environ.get("PAIRS_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 15  # 15분마다 체크
Z_SCORE_ENTRY = 2.0  # 스프레드가 정규분포 2 표준편차 이상 벌어지면 진입
Z_SCORE_EXIT = 0.5   # 스프레드가 0.5 이내로 줄어들면 청산
LOOKBACK_PERIOD = 100 # 스프레드 평균을 낼 과거 캔들 수 (15분봉 100개)

def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"

def _calculate_zscore(spread: pd.Series) -> float:
    mean = spread.mean()
    std = spread.std()
    return (spread.iloc[-1] - mean) / std if std > 0 else 0

def run_cycle() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pairs_paper_state.json")
    
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            state = json.load(f)
    else:
        state = {"equity_usdt": START_CAPITAL_USDT, "positions": {}, "cumulative_realized_pnl_usdt": 0.0}

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    
    try:
        since_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        df_btc = fetch_perp_ohlcv(_perp_symbol("BTC"), "15m", since_iso, None)
        df_eth = fetch_perp_ohlcv(_perp_symbol("ETH"), "15m", since_iso, None)
        
        # 데이터 길이 맞추기
        min_len = min(len(df_btc), len(df_eth))
        if min_len < LOOKBACK_PERIOD:
            return
            
        btc_close = df_btc["Close"].iloc[-min_len:]
        eth_close = df_eth["Close"].iloc[-min_len:]
        
        # 비율(Ratio) 기반 스프레드 계산 (예: BTC 가격 / ETH 가격)
        spread = btc_close / eth_close
        z_score = _calculate_zscore(spread.iloc[-LOOKBACK_PERIOD:])
        
        btc_price = btc_close.iloc[-1]
        eth_price = eth_close.iloc[-1]
        
    except Exception as exc:
        print(f"데이터 수집 에러: {exc}")
        return

    # 1. 청산 로직 (Z-Score가 중앙으로 회귀했을 때)
    if state["positions"] and abs(z_score) < Z_SCORE_EXIT:
        print(f"[{now_iso()}] 🔵 Z-Score 회귀 ({z_score:.2f}) -> 페어 청산 진행")
        for base, pos in list(state["positions"].items()):
            current_price = btc_price if base == "BTC" else eth_price
            direction = 1 if pos["side"] == "long" else -1
            pnl = pos["notional_usdt"] * direction * (current_price / pos["entry_price"] - 1)
            
            print(f"  청산: {base} {pos['side']} (손익: {pnl:+.2f} USDT)")
            state["cumulative_realized_pnl_usdt"] += pnl
            state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
        state["positions"] = {} # 모두 청산

    # 2. 진입 로직
    if not state["positions"]:
        invest_amount = START_CAPITAL_USDT * 0.2  # 자본의 20%를 각 다리에 투자 (총 40%)

        if z_score > Z_SCORE_ENTRY and state["equity_usdt"] >= invest_amount * 2:
            # BTC가 상대적으로 고평가, ETH가 저평가 -> BTC 숏, ETH 롱
            print(f"[{now_iso()}] 🟢 스프레드 확대 (Z: {z_score:.2f}) -> BTC 공매도(Short), ETH 매수(Long)")
            state["positions"]["BTC"] = {"side": "short", "entry_price": btc_price, "notional_usdt": invest_amount}
            state["positions"]["ETH"] = {"side": "long", "entry_price": eth_price, "notional_usdt": invest_amount}
            state["equity_usdt"] -= invest_amount * 2  # 두 다리 원금 차감

        elif z_score < -Z_SCORE_ENTRY and state["equity_usdt"] >= invest_amount * 2:
            # BTC가 저평가, ETH가 고평가 -> BTC 롱, ETH 숏
            print(f"[{now_iso()}] 🟢 스프레드 축소 (Z: {z_score:.2f}) -> BTC 매수(Long), ETH 공매도(Short)")
            state["positions"]["BTC"] = {"side": "long", "entry_price": btc_price, "notional_usdt": invest_amount}
            state["positions"]["ETH"] = {"side": "short", "entry_price": eth_price, "notional_usdt": invest_amount}
            state["equity_usdt"] -= invest_amount * 2

    # 3. 미실현 손익 업데이트
    unrealized_pnl = 0
    for base, pos in state["positions"].items():
        current_price = btc_price if base == "BTC" else eth_price
        direction = 1 if pos["side"] == "long" else -1
        pnl = pos["notional_usdt"] * direction * (current_price / pos["entry_price"] - 1)
        pos["unrealized_pnl_usdt"] = pnl
        unrealized_pnl += pnl

    total_equity = state["equity_usdt"] + unrealized_pnl
    print(f"--- ⚖️ 페어 트레이딩 평가자산: {total_equity:,.2f} USDT (현재 Z-Score: {z_score:.2f}) ---")

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def main() -> None:
    print(f"페어 트레이딩 봇 가동! (15분 간격 / Z-Score 진입:{Z_SCORE_ENTRY} 청산:{Z_SCORE_EXIT})")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
