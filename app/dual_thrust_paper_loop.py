"""Dual Thrust 돌파 매매 모의투자 봇 (vnpy 대표 CTA 전략)

전날의 데이터를 바탕으로 박스권(Range)을 만들고,
오늘 가격이 그 범위를 돌파(Breakout)하면 강한 추세가 시작된 것으로 판단하여
과감하게 추세를 따라가는 기관급 돌파 매매 전략입니다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd

from app.futures_data import fetch_perp_ohlcv
from app.momentum_state import append_equity_point, now_iso

UNIVERSE = ["BTC", "ETH", "SOL"]
START_CAPITAL_USDT = float(os.environ.get("DUAL_THRUST_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 5  # 5분마다 현재가가 상/하단을 돌파했는지 체크
K1 = 0.5  # 상단 돌파 계수 (숫자가 클수록 둔감)
K2 = 0.5  # 하단 돌파 계수
POSITION_SIZE_PCT = 0.3

def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"

def _calculate_dual_thrust_bounds(symbol: str) -> tuple[float, float, float]:
    """과거 3일간의 일봉을 바탕으로 Range와 오늘의 상/하단 가격을 계산합니다."""
    try:
        # 최근 5일치 일봉 가져오기
        since_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        df = fetch_perp_ohlcv(symbol, "1d", since_iso, None)
        
        if len(df) < 2:
            return 0, 0, 0
            
        # 어제(직전 일봉) 데이터 추출
        yesterday = df.iloc[-2] # -1은 아직 안 끝난 오늘 일봉
        n_days_ago = df.iloc[-4:-1] # 최근 3일 (어제 포함)
        
        hh = n_days_ago["High"].max()
        hc = n_days_ago["Close"].max()
        lc = n_days_ago["Close"].min()
        ll = n_days_ago["Low"].min()
        
        # Range 계산 (가장 큰 변동폭)
        range_val = max(hh - lc, hc - ll)
        
        # 오늘의 시가(Open)를 기준으로 상하단 설정
        today_open = df.iloc[-1]["Open"]
        buy_line = today_open + K1 * range_val
        sell_line = today_open - K2 * range_val
        
        return buy_line, sell_line, df.iloc[-1]["Close"]
    except Exception as exc:
        print(f"Bounds 계산 실패: {exc}")
        return 0, 0, 0

def run_cycle() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dual_thrust_state.json")
    
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            state = json.load(f)
    else:
        state = {"equity_usdt": START_CAPITAL_USDT, "positions": {}, "cumulative_realized_pnl_usdt": 0.0}

    current_prices = {}
    
    for base in UNIVERSE:
        symbol = _perp_symbol(base)
        buy_line, sell_line, current_price = _calculate_dual_thrust_bounds(symbol)
        if buy_line == 0:
            continue
            
        current_prices[base] = current_price
        pos = state["positions"].get(base)
        
        # 1. 청산 및 스위칭 로직
        if pos:
            direction = 1 if pos["side"] == "long" else -1
            pnl = pos["notional_usdt"] * direction * (current_price / pos["entry_price"] - 1)
            
            # 롱인데 하단 돌파하면 손절(청산) 및 숏 스위칭 대기
            if pos["side"] == "long" and current_price < sell_line:
                print(f"[{now_iso()}] 🔴 롱 청산 (하단 붕괴): {base} (손익: {pnl:+.2f})")
                state["cumulative_realized_pnl_usdt"] += pnl
                state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
                del state["positions"][base]

            # 숏인데 상단 돌파하면 손절(청산) 및 롱 스위칭 대기
            elif pos["side"] == "short" and current_price > buy_line:
                print(f"[{now_iso()}] 🔴 숏 청산 (상단 돌파): {base} (손익: {pnl:+.2f})")
                state["cumulative_realized_pnl_usdt"] += pnl
                state["equity_usdt"] += pos["notional_usdt"] + pnl
                del state["positions"][base]

        # 2. 진입 로직
        if base not in state["positions"]:
            invest_amount = START_CAPITAL_USDT * POSITION_SIZE_PCT
            if state["equity_usdt"] >= invest_amount:
                # 상단선(Buy Line) 돌파 시 Long 진입
                if current_price > buy_line:
                    print(f"[{now_iso()}] 🟢 상단 돌파 롱 진입: {base} @ {current_price:.2f} (상단: {buy_line:.2f})")
                    state["positions"][base] = {"side": "long", "entry_price": current_price, "notional_usdt": invest_amount}
                    state["equity_usdt"] -= invest_amount
                # 하단선(Sell Line) 붕괴 시 Short 진입
                elif current_price < sell_line:
                    print(f"[{now_iso()}] 🔴 하단 붕괴 숏 진입: {base} @ {current_price:.2f} (하단: {sell_line:.2f})")
                    state["positions"][base] = {"side": "short", "entry_price": current_price, "notional_usdt": invest_amount}
                    state["equity_usdt"] -= invest_amount

    # 미실현 손익 업데이트
    unrealized_pnl = 0
    for base, pos in state["positions"].items():
        if current_prices.get(base):
            direction = 1 if pos["side"] == "long" else -1
            pnl = pos["notional_usdt"] * direction * (current_prices[base] / pos["entry_price"] - 1)
            pos["unrealized_pnl_usdt"] = pnl
            unrealized_pnl += pnl

    total_equity = state["equity_usdt"] + unrealized_pnl
    print(f"--- 🚀 Dual Thrust 평가자산: {total_equity:,.2f} USDT (보유: {len(state['positions'])}종목) ---")
    append_equity_point(state, total_equity)

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def main() -> None:
    print("Dual Thrust 봇 가동! (5분 간격 / 상하단선 돌파 모니터링)")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
