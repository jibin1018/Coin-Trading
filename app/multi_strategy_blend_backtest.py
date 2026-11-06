"""검증된 3개 전략(코인 모멘텀 로테이션, 국장/미장 Top-N 로테이션)을 한 포트폴리오로 섞었을
때 분산효과로 Sharpe/Calmar가 더 좋아지는지 확인한다. 각 전략의 실제 일별 수익률 시리즈를
그대로 가져와서(파라미터는 이번 세션에서 검증된 "현재 운영값") 동일 비중(1/3)으로 매일
재배분하는 식으로 블렌딩한다 — 세 시장(코인 24/7, 국장, 미장)이 거래일이 서로 달라서,
어느 한 시장이 쉬는 날은 그 시장의 수익률을 0%로 본다(실제로 그날은 그 전략 포지션이
안 움직였다는 뜻이라 합리적인 근사).

실행: python -m app.multi_strategy_blend_backtest
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import app.futures_momentum_rotation_sweep as coin_mod
import app.kr_us_rotation_backtest as stock_mod


def _metrics(daily: pd.Series) -> dict:
    eq = (1 + daily).cumprod()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-6)
    final = float(eq.iloc[-1])
    cagr = (final ** (1 / years) - 1) * 100 if final > 0 else -100.0
    peak = eq.cummax()
    mdd = float(((peak - eq) / peak).max() * 100)
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    calmar = cagr / mdd if mdd > 0 else float("inf")
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe, "calmar": calmar}


def _coin_daily() -> pd.Series:
    """현재 운영 기본값(룩백14/리밸3일/top8, 레버리지1x, 킬스위치 불필요— 이미 자체 MDD가
    낮아서 안 씀)으로 코인 모멘텀 로테이션 일별수익률을 가져온다."""
    coin_mod.LEVERAGE = 1.0
    coin_mod.KILL_DD = None
    closes = coin_mod._fetch_closes()
    _, _, _, _, daily = coin_mod.simulate(closes, 14, 3, 8)
    if daily.index.tz is not None:
        daily.index = daily.index.tz_localize(None)
    return daily.groupby(daily.index.normalize()).last()  # 코인은 매일 타임스탬프가 자정이 아닐 수 있어 날짜로 정규화


def _stock_daily(market: str, lookback: int, rebal: int, top_n: int) -> pd.Series:
    """검증된 킬스위치(25%)를 적용한 국장/미장 Top-N 로테이션 일별수익률."""
    stock_mod.MARKET = market
    stock_mod.PORTFOLIO_KILL_DD = 0.25
    stock_mod.VOL_WEIGHTED = False
    closes = stock_mod._load_closes()
    r = stock_mod._sim(closes, lookback, rebal, top_n, "none")
    return r["daily"]


def run() -> None:
    print("코인 모멘텀 로테이션 수익률 수집...")
    coin = _coin_daily()
    print("국장 Top-N 로테이션 수익률 수집...")
    kr = _stock_daily("KR", 120, 20, 5)
    print("미장 Top-N 로테이션 수익률 수집...\n")
    us = _stock_daily("US", 120, 10, 3)

    individual = {"코인 모멘텀": coin, "국장 로테이션": kr, "미장 로테이션": us}
    for name, series in individual.items():
        m = _metrics(series)
        print(f"[개별] {name:10s} CAGR {m['cagr']:+7.1f}%  MDD {m['mdd']:5.1f}%  "
              f"Sharpe {m['sharpe']:.2f}  Calmar {m['calmar']:.2f}  ({series.index[0].date()}~{series.index[-1].date()})")

    # 세 시장 거래일이 다 다르다 — 합집합 날짜에 맞추고, 어느 시장이 쉬는 날은 그 전략
    # 수익률을 0%로 본다(그날 그 포지션이 그냥 안 움직인 것으로 취급).
    all_dates = coin.index.union(kr.index).union(us.index)
    aligned = pd.DataFrame({
        "coin": coin.reindex(all_dates).fillna(0.0),
        "kr": kr.reindex(all_dates).fillna(0.0),
        "us": us.reindex(all_dates).fillna(0.0),
    }).sort_index()

    blended = aligned.mean(axis=1)  # 매일 1/3씩 동일비중 재배분
    m_blend = _metrics(blended)
    print(f"\n[블렌드 1/3씩] CAGR {m_blend['cagr']:+7.1f}%  MDD {m_blend['mdd']:5.1f}%  "
          f"Sharpe {m_blend['sharpe']:.2f}  Calmar {m_blend['calmar']:.2f}")

    corr = aligned.corr()
    print("\n상관관계(일별수익률):")
    print(corr.round(2).to_string())

    best_individual_sharpe = max(_metrics(s)["sharpe"] for s in individual.values())
    verdict = "분산효과 있음 — Sharpe가 개별 최고보다 높음" if m_blend["sharpe"] > best_individual_sharpe \
        else "분산효과 미미 — 개별 최고 전략 Sharpe를 못 넘음"
    print(f"\n개별 전략 중 최고 Sharpe: {best_individual_sharpe:.2f} vs 블렌드 Sharpe: {m_blend['sharpe']:.2f} -> {verdict}")


if __name__ == "__main__":
    run()
