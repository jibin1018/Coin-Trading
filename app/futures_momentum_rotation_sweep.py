"""모멘텀 로테이션 파라미터 강건성 체크 — 특정 파라미터 하나에 우연히 맞은 결과가 아닌지
lookback/리밸런스주기/롱숏개수를 조금씩 바꿔가며 연환산·MDD가 일관되게 나오는지 확인.

FUNDING_AWARE=true 로 돌리면 펀딩비(8시간마다 정산)도 반영한다 — README가 명시한 미해결
문제("모멘텀 로테이션에 펀딩비 미반영")를 닫기 위함. 롱은 펀딩비가 양수일 때 지불하고
숏은 받으므로(공식: 포지션당 손익 = -funding_rate * weight, weight는 롱+ 숏-로 부호가
이미 들어있어 양쪽에 같은 식이 적용된다), 종목별 8시간 펀딩률을 하루 합으로 모아서 그날
가중치에 곱해 equity에서 차감/가산한다.
"""
from __future__ import annotations

import os

import pandas as pd

from app.funding_arb_backtest import fetch_funding_history
from app.futures_data import fetch_perp_ohlcv
from app.futures_momentum_rotation_probe import UNIVERSE, SINCE, COMMISSION_PCT

FUNDING_AWARE = os.environ.get("FUNDING_AWARE", "false").lower() == "true"
# app/kr_us_rotation_backtest.py에서 검증된 계좌 전체 킬스위치(드로다운 임계값 넘으면
# 전량청산+쿨다운)를 코인 쪽에도 테스트해볼 수 있게 옵션으로 추가. 0/미설정=꺼짐.
KILL_DD = float(os.environ.get("KILL_DD", "0")) or None
KILL_COOLDOWN_DAYS = int(os.environ.get("KILL_COOLDOWN_DAYS", "20"))


def _fetch_closes() -> pd.DataFrame:
    series = {}
    for base in UNIVERSE:
        try:
            frame = fetch_perp_ohlcv(f"{base}/USDT:USDT", "1d", SINCE, None)
        except Exception:
            continue
        if frame.empty:
            continue
        series[base] = frame["Close"]
    return pd.DataFrame(series).sort_index()


def _fetch_funding_daily(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """종목별 8시간 펀딩률을 하루 합으로 묶어 closes와 같은 날짜 인덱스에 맞춘다."""
    series = {}
    for base in UNIVERSE:
        try:
            hist = fetch_funding_history(f"{base}/USDT:USDT", SINCE, None)
        except Exception as exc:
            print(f"  {base}: 펀딩비 조회 실패 ({exc})")
            continue
        if hist.empty:
            continue
        daily = hist["fundingRate"].resample("1D").sum()
        daily.index = daily.index.tz_localize(None)
        series[base] = daily
    funding = pd.DataFrame(series).reindex(dates).fillna(0.0)
    return funding


def simulate(
    closes: pd.DataFrame, lookback: int, rebalance_every: int, top_k: int,
    funding: pd.DataFrame | None = None,
) -> tuple[float, float, float, int]:
    returns = closes.pct_change()
    momentum = closes.pct_change(lookback)
    dates = closes.index
    start_idx = lookback + 1
    weights = pd.Series(0.0, index=closes.columns)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    last_weights = weights.copy()
    n_rebal = 0
    halted_until = -1

    for i in range(start_idx, len(dates)):
        date = dates[i]
        day_ret = returns.loc[date]
        port_ret = (weights * day_ret.fillna(0.0)).sum()
        if funding is not None and date in funding.index:
            # 롱(weight>0)은 펀딩률이 양수면 지불(-), 숏(weight<0)은 받음(+) — 부호가 이미
            # weight에 들어있어 -funding_rate * weight 하나의 식으로 양쪽을 다 표현한다.
            port_ret -= (weights * funding.loc[date].reindex(weights.index).fillna(0.0)).sum()
        equity *= (1 + port_ret)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

        if KILL_DD is not None and halted_until < i:
            dd = 1 - equity / peak
            if dd >= KILL_DD:
                if weights.abs().sum() > 0:
                    turnover = weights.abs().sum()
                    equity *= (1 - turnover * COMMISSION_PCT / 100)
                weights = pd.Series(0.0, index=closes.columns)
                last_weights = weights.copy()
                halted_until = i + KILL_COOLDOWN_DAYS
                peak = equity

        if halted_until >= i:
            continue  # 쿨다운 중엔 리밸런스도 건너뜀(전액 현금 유지)

        if (i - start_idx) % rebalance_every == 0:
            mom_today = momentum.loc[date].dropna()
            mom_today = mom_today[closes.loc[date].notna()]
            if len(mom_today) >= top_k * 2:
                ranked = mom_today.sort_values(ascending=False)
                longs = ranked.index[:top_k]
                shorts = ranked.index[-top_k:]
                new_weights = pd.Series(0.0, index=closes.columns)
                new_weights[longs] = 0.5 / top_k
                new_weights[shorts] = -0.5 / top_k
                turnover = (new_weights - last_weights).abs().sum()
                equity *= (1 - turnover * COMMISSION_PCT / 100)
                weights = new_weights
                last_weights = new_weights.copy()
                n_rebal += 1

    years = (dates[-1] - dates[start_idx]).days / 365.25
    annualized_pct = ((equity) ** (1 / years) - 1) * 100 if years > 0 else float("nan")
    return annualized_pct, max_dd * 100, years, n_rebal


def run() -> None:
    print("종가 수집 중...")
    closes = _fetch_closes()
    print(f"{len(closes.columns)}개 종목, {len(closes)}행\n")

    funding = None
    if FUNDING_AWARE:
        print("펀딩비 수집 중... (종목당 페이지네이션이라 시간이 좀 걸립니다)")
        funding = _fetch_funding_daily(closes.index)
        print(f"펀딩비 {funding.shape[1]}종목 수집 완료\n")

    print(f"{'lookback':>9}{'rebal':>7}{'top_k':>7}{'연환산':>9}{'MDD':>8}{'리밸런스':>9}")
    grid = []
    for lookback in [14, 30, 45, 60]:
        for rebalance in [3, 7, 14]:
            for top_k in [3, 5, 8]:
                ann, mdd, years, n = simulate(closes, lookback, rebalance, top_k, funding)
                grid.append((lookback, rebalance, top_k, ann, mdd, n))
                print(f"{lookback:>9}{rebalance:>7}{top_k:>7}{ann:>8.2f}%{mdd:>7.1f}%{n:>9}")

    anns = [g[3] for g in grid]
    print(f"\n{len(grid)}개 조합 중 연환산>=10%: {sum(1 for a in anns if a >= 10)}개, "
          f"평균 {sum(anns)/len(anns):.2f}%, 중앙값 {sorted(anns)[len(anns)//2]:.2f}%, "
          f"최소 {min(anns):.2f}%, 최대 {max(anns):.2f}%")


if __name__ == "__main__":
    run()
