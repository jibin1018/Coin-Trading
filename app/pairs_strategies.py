"""마켓 뉴트럴 페어 트레이딩(통계적 차익거래) 근사 — 지금까지 테스트한 전략들과 근본적으로
다른 메커니즘이다: 개별 자산의 방향(오를지 내릴지)을 맞힐 필요 없이, 두 자산의 상대가격
비율이 평균으로 회귀한다는 데만 베팅한다. app/pairs_data.py가 만든 합성 비율 시리즈를 그대로
거래한다.

실측 백테스트(2022~)에서 수익률 -65%~-92%(매수보유 대비 -229%p~-302%p, MDD 70~93%)로
크게 실패했다 — 근본 원인은 BTC/ETH/SOL 가격 비율이 이 기간 동안 평균회귀하는 정상
시계열이 아니라는 것(알트시즌/BTC 강세 사이클로 수년간 구조적으로 추세를 탄다). 그래서
ADF(Augmented Dickey-Fuller) 검정으로 "지금 이 비율이 통계적으로 평균회귀 상태인지"를
먼저 확인하고, 통과할 때만 신규 진입을 허용하도록 바꿨다. 포지션 크기도 검증 안 된
베팅이라 줄였고, 레짐전환으로 영원히 복귀하지 않는 경우를 막기 위해 최대 보유기간도 뒀다.

레짐 변화로 스프레드가 영구적으로 벌어지는("pairs trade blowup") 리스크를 STOP_Z와
MAX_HOLD_BARS로 방어한다.
"""
from __future__ import annotations

import pandas as pd
from backtesting import Strategy
from statsmodels.tsa.stattools import adfuller


def rolling_adf_pvalue(close: pd.Series, window: int) -> pd.Series:
    """구간별로 ADF 검정을 돌려 p-value를 매긴다 — p-value가 낮을수록(기본 임계값 0.1
    미만) 그 구간의 시리즈가 평균회귀하는 정상 시계열일 가능성이 높다는 뜻이다.
    adfuller는 벡터화가 안 돼서 창마다 호출해야 한다(백테스트 1회성 계산이라 허용)."""
    values = close.to_numpy()
    pvalues = [float("nan")] * len(values)
    for i in range(window, len(values)):
        segment = values[i - window : i]
        try:
            pvalues[i] = adfuller(segment, maxlag=1, result_object=False)[1]
        except Exception:
            pvalues[i] = float("nan")
    return pd.Series(pvalues, index=close.index)


class PairsMeanReversionStrategy(Strategy):
    LOOKBACK = 60
    ENTRY_Z = 2.0
    EXIT_Z = 0.5
    STOP_Z = 3.5
    POSITION_FRACTION = 0.3  # 공적분 미검증 상태로 0.95를 썼던 게 과도한 베팅이었음
    WARMUP_BARS = 65
    ADF_WINDOW = 250
    ADF_PVALUE_MAX = 0.1
    MAX_HOLD_BARS = 40  # 타임스탑 — 구조적 레짐전환으로 영원히 안 돌아오는 경우 방지

    def init(self):
        close = pd.Series(self.data.Close)
        rolling_mean = close.rolling(self.LOOKBACK).mean()
        rolling_std = close.rolling(self.LOOKBACK).std()
        self.zscore = self.I(lambda: ((close - rolling_mean) / rolling_std).values, name="zscore")
        self.adf_ok = self.I(
            lambda: (rolling_adf_pvalue(close, self.ADF_WINDOW) < self.ADF_PVALUE_MAX).values,
            name="adf_ok",
        )
        self.entry_bar: int | None = None

    def next(self):
        i = len(self.data.Close) - 1
        if i < self.WARMUP_BARS:
            return
        z = self.zscore[-1]
        if z != z:  # NaN
            return

        if self.position:
            held = i - self.entry_bar if self.entry_bar is not None else 0
            timed_out = held >= self.MAX_HOLD_BARS
            if self.position.is_long and (z >= -self.EXIT_Z or z <= -self.STOP_Z or timed_out):
                self.position.close()
                self.entry_bar = None
            elif self.position.is_short and (z <= self.EXIT_Z or z >= self.STOP_Z or timed_out):
                self.position.close()
                self.entry_bar = None
            return

        if not self.adf_ok[-1]:  # 지금 구간이 평균회귀 상태로 검증 안 되면 신규진입 금지
            return

        if z <= -self.ENTRY_Z:
            self.buy(size=self.POSITION_FRACTION)
            self.entry_bar = i
        elif z >= self.ENTRY_Z:
            self.sell(size=self.POSITION_FRACTION)
            self.entry_bar = i
