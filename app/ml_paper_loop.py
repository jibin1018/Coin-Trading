"""머신러닝 다이나믹 예측 모의투자 봇 (FreqAI / ML4T 스타일)

과거 데이터(RSI, MACD, 거래량, 과거 변동성 등)를 바탕으로
'다음 5분봉이 오를 확률'을 머신러닝(Logistic Regression 등)으로 예측하여
확률이 매우 높을 때만 진입하는 인공지능 예측 봇입니다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import pandas_ta as ta

from app.futures_data import fetch_perp_ohlcv
from app.momentum_state import now_iso

# scikit-learn이 없을 경우를 대비한 처리
try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

UNIVERSE = ["BTC", "ETH", "SOL"]
START_CAPITAL_USDT = float(os.environ.get("ML_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 5  # 5분마다 예측
PROBABILITY_THRESHOLD = 0.65  # 65% 이상 오를/내릴 확률일 때만 진입

def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"

def _train_and_predict(symbol: str) -> tuple[str, float]:
    """최근 7일 데이터를 불러와 실시간으로 학습하고 다음 봉을 예측합니다."""
    try:
        since_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        df = fetch_perp_ohlcv(symbol, "5m", since_iso, None)
        
        if len(df) < 200:
            return "hold", 0.0

        # 특성(Feature) 엔지니어링: AI가 학습할 단서들
        df["rsi"] = ta.rsi(df["Close"], length=14)
        df["momentum"] = df["Close"] / df["Close"].shift(5) - 1
        df["vol_ratio"] = df["Volume"] / df["Volume"].rolling(20).mean()

        # 정답(Target) 생성: 다음 봉이 올랐으면 1, 내렸으면 0
        df["target"] = (df["Close"].shift(-1) > df["Close"]).astype(int)

        # vol_ratio는 거래량 평균이 0에 가까우면 inf가 될 수 있는데, dropna는 inf를 안 지운다 —
        # 그대로 두면 스케일러/회귀가 overflow로 깨진다.
        df.replace([float("inf"), float("-inf")], pd.NA, inplace=True)
        df.dropna(inplace=True)
        
        # 마지막 행(현재 봉)은 정답이 없으므로 테스트용으로 분리
        train_df = df.iloc[:-1]
        current_features = df.iloc[-1:][["rsi", "momentum", "vol_ratio"]]
        
        if not HAS_SKLEARN:
            # sklearn이 설치되어 있지 않다면 임시 로직 반환 (단순 모멘텀 추종)
            pred_prob = 0.7 if current_features["momentum"].iloc[0] > 0.02 else 0.3
        else:
            # 머신러닝 학습 — rsi(0~100)와 momentum(-1~1 근처) 스케일 차이가 커서
            # 스케일링 없이 넣으면 로지스틱 회귀가 overflow로 깨짐(RuntimeWarning) → 표준화 후 학습
            X = train_df[["rsi", "momentum", "vol_ratio"]]
            y = train_df["target"]

            model = make_pipeline(StandardScaler(), LogisticRegression())
            model.fit(X, y)
            
            # 현재 차트 상황을 주고 확률 예측
            # predict_proba는 [내릴 확률, 오를 확률]을 반환
            pred_prob = model.predict_proba(current_features)[0][1] 
            
        if pred_prob >= PROBABILITY_THRESHOLD:
            return "long", pred_prob
        elif pred_prob <= (1 - PROBABILITY_THRESHOLD):
            return "short", 1 - pred_prob
        else:
            return "hold", max(pred_prob, 1 - pred_prob)
            
    except Exception as exc:
        print(f"ML 예측 실패: {exc}")
        return "hold", 0.0

def run_cycle() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ml_paper_state.json")
    if os.path.exists(state_file):
        with open(state_file, "r") as f: state = json.load(f)
    else:
        state = {"equity_usdt": START_CAPITAL_USDT, "positions": {}, "cumulative_realized_pnl_usdt": 0.0}

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    
    for base in UNIVERSE:
        symbol = _perp_symbol(base)
        decision, prob = _train_and_predict(symbol)
        
        try:
            current_price = float(exchange.fetch_ticker(symbol)["last"])
        except:
            continue
            
        pos = state["positions"].get(base)
        
        # 청산 로직 (예측이 반대로 바뀌거나 확률이 떨어지면)
        if pos:
            direction = 1 if pos["side"] == "long" else -1
            pnl = pos["notional_usdt"] * direction * (current_price / pos["entry_price"] - 1)
            
            if pos["side"] != decision and decision != "hold":
                print(f"[{now_iso()}] 🔴 ML 예측 변경으로 스위칭 청산: {base} (손익: {pnl:+.2f})")
                state["cumulative_realized_pnl_usdt"] += pnl
                state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
                del state["positions"][base]
            elif pnl < -pos["notional_usdt"] * 0.03: # 3% 강제 손절
                print(f"[{now_iso()}] 🔴 ML 봇 강제 손절: {base} (손익: {pnl:+.2f})")
                state["cumulative_realized_pnl_usdt"] += pnl
                state["equity_usdt"] += pos["notional_usdt"] + pnl
                del state["positions"][base]

        # 진입 로직 — 예산 체크가 아예 없었음(항상 진입) — 추가
        if base not in state["positions"] and decision in ["long", "short"]:
            invest_amount = START_CAPITAL_USDT * 0.3
            if state["equity_usdt"] >= invest_amount:
                print(f"[{now_iso()}] 🟢 ML 봇 진입: {base} {decision} @ {current_price:.2f} (AI 확신도: {prob*100:.1f}%)")
                state["positions"][base] = {"side": decision, "entry_price": current_price, "notional_usdt": invest_amount}
                state["equity_usdt"] -= invest_amount

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
        
    print(f"--- 🧠 ML 봇 평가자산: {state['equity_usdt']:,.2f} USDT (보유: {len(state['positions'])}) ---")

def main() -> None:
    if not HAS_SKLEARN:
        print("⚠️ 주의: scikit-learn 라이브러리가 없습니다. 'pip install scikit-learn' 후 실행하면 진짜 AI가 가동됩니다.")
    print("머신러닝 예측 봇 가동! (매 5분마다 실시간 학습 및 예측)")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
