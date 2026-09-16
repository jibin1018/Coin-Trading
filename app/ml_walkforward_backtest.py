"""머신러닝 예측봇(app/ml_paper_loop.py) 워크포워드 백테스트.

페이퍼봇은 매 5분마다 최근 7일치로 로지스틱 회귀를 실시간 재학습해서 다음 봉을 예측한다.
백테스트에서 매 봉(5분)마다 재학습하면 계산량이 너무 커서(90일 기준 심볼당 25,920회 학습),
여기서는 하루(288봉)에 한 번만 재학습하고 그 모델로 다음 하루치를 예측하는 워크포워드 방식을
쓴다 — 페이퍼봇보다 반응은 느리지만, "미래 데이터를 학습에 쓰지 않는다"는 원칙은 동일하게
지킨다(각 재학습 시점 i 는 오직 i 이전 7일치 데이터만 사용).

주의: 선물 가격만 재현하고 펀딩비는 미반영(app/futures_data.py 참고).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as ta

from app.futures_data import fetch_perp_ohlcv

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

UNIVERSE = ["BTC", "ETH", "SOL"]
LOOKBACK_BARS = 2016  # 7일치 5분봉 (7*24*12)
RETRAIN_INTERVAL_BARS = 288  # 1일마다 재학습
PROBABILITY_THRESHOLD = 0.65
POSITION_SIZE_PCT = 0.3
STOP_LOSS_PCT = 0.03
FEE_PCT = 0.05
FEATURE_COLS = ["rsi", "momentum", "vol_ratio"]


def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = ta.rsi(df["Close"], length=14)
    df["momentum"] = df["Close"] / df["Close"].shift(5) - 1
    df["vol_ratio"] = df["Volume"] / df["Volume"].rolling(20).mean()
    df["target"] = (df["Close"].shift(-1) > df["Close"]).astype(int)
    # vol_ratio는 거래량 평균이 0에 가까우면 inf가 될 수 있는데, dropna는 inf를 안 지운다 —
    # 그대로 두면 스케일러/회귀가 overflow로 깨진다.
    df = df.replace([float("inf"), float("-inf")], pd.NA)
    return df


def _simulate(symbol: str, sim_since: str, until: str | None, start_capital: float) -> dict:
    if not HAS_SKLEARN:
        raise RuntimeError("scikit-learn이 설치되어 있지 않습니다 — requirements.txt에 추가됨, 이미지 재빌드 필요")

    warmup_since = (
        datetime.fromisoformat(sim_since.replace("Z", "+00:00")) - timedelta(days=7)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = fetch_perp_ohlcv(symbol, "5m", warmup_since, until)
    df = _build_features(raw)

    sim_start_ts = pd.Timestamp(sim_since)
    start_idx = df.index.searchsorted(sim_start_ts)
    start_idx = max(start_idx, LOOKBACK_BARS)
    if start_idx >= len(df) - 1:
        return {"symbol": symbol, "trades": 0, "return_pct": 0.0, "max_dd_pct": 0.0, "bh_pct": 0.0}

    budget = start_capital * POSITION_SIZE_PCT
    cash = budget
    position: dict | None = None  # {"side": "long"/"short", "entry_price": float}
    n_trades = 0
    model = None
    equity_curve = []

    for i in range(start_idx, len(df) - 1):  # 마지막 행은 target(다음봉)이 없어 제외
        if model is None or (i - start_idx) % RETRAIN_INTERVAL_BARS == 0:
            train = df.iloc[i - LOOKBACK_BARS:i].dropna(subset=FEATURE_COLS + ["target"])
            if len(train) >= 100 and train["target"].nunique() >= 2:
                model = make_pipeline(StandardScaler(), LogisticRegression())
                model.fit(train[FEATURE_COLS], train["target"])
            else:
                model = None

        row = df.iloc[i]
        price = row["Close"]
        decision, prob = "hold", 0.0
        if model is not None and not row[FEATURE_COLS].isna().any():
            pred_prob = model.predict_proba(row[FEATURE_COLS].to_frame().T)[0][1]
            if pred_prob >= PROBABILITY_THRESHOLD:
                decision, prob = "long", pred_prob
            elif pred_prob <= (1 - PROBABILITY_THRESHOLD):
                decision, prob = "short", 1 - pred_prob

        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            pnl_pct = direction * (price / position["entry_price"] - 1)
            switch = decision != "hold" and decision != position["side"]
            stop = pnl_pct < -STOP_LOSS_PCT
            if switch or stop:
                pnl = budget * pnl_pct - budget * FEE_PCT / 100
                cash += pnl
                n_trades += 1
                position = None

        if position is None and decision in ("long", "short"):
            position = {"side": decision, "entry_price": price}
            cash -= budget * FEE_PCT / 100

        unrealized = 0.0
        if position is not None:
            direction = 1 if position["side"] == "long" else -1
            unrealized = budget * direction * (price / position["entry_price"] - 1)
        equity_curve.append(cash + unrealized)

    equity = pd.Series(equity_curve, index=df.index[start_idx:len(df) - 1])
    total_return_pct = (equity.iloc[-1] / budget - 1) * 100 if len(equity) else 0.0
    peak = equity.cummax()
    max_dd_pct = ((peak - equity) / peak).max() * 100 if len(equity) else 0.0
    bh_pct = (df["Close"].iloc[len(df) - 2] / df["Close"].iloc[start_idx] - 1) * 100
    return {
        "symbol": symbol, "trades": n_trades, "return_pct": total_return_pct,
        "max_dd_pct": max_dd_pct, "bh_pct": bh_pct,
    }


def parse_args() -> argparse.Namespace:
    default_since = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z")
    parser = argparse.ArgumentParser(description="ML 예측봇 워크포워드 백테스트")
    parser.add_argument("--since", default=default_since, help="시뮬레이션 시작일(내부적으로 7일 전 데이터까지 추가로 받아 워밍업)")
    parser.add_argument("--until", default=None)
    parser.add_argument("--start-capital", type=float, default=10_000.0)
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    if not HAS_SKLEARN:
        print("scikit-learn 미설치 — 이미지를 재빌드하세요(`docker build -t coin-trade .`)")
        return
    print(f"[ML 워크포워드 백테스트] {args.since} ~ {args.until or 'now'}, "
          f"{RETRAIN_INTERVAL_BARS}봉(1일)마다 재학습, 7일 룩백\n")
    results = []
    for base in UNIVERSE:
        print(f"[{base}] 데이터 수집 + 워크포워드 시뮬레이션 중... (재학습 반복이라 시간이 좀 걸립니다)")
        r = _simulate(_perp_symbol(base), args.since, args.until, args.start_capital)
        results.append(r)
        print(f"  {base}: 수익률 {r['return_pct']:+.2f}% (매수보유 {r['bh_pct']:+.2f}%, 초과 {r['return_pct']-r['bh_pct']:+.2f}%p), "
              f"최대낙폭 {r['max_dd_pct']:.1f}%, 매매 {r['trades']}건")

    avg_return = sum(r["return_pct"] for r in results) / len(results)
    avg_bh = sum(r["bh_pct"] for r in results) / len(results)
    print(f"\n[요약] {len(UNIVERSE)}종목 평균 수익률 {avg_return:+.2f}% (평균 매수보유 {avg_bh:+.2f}%, 초과 {avg_return-avg_bh:+.2f}%p)")


if __name__ == "__main__":
    run(parse_args())
