"""국장/미장 롱온리 top-N 모멘텀 로테이션 백테스트 — 매수&보유(등가중)를 이기는지 확인.

코인 momentum_rotation 은 달러중립 롱숏이지만 국내/미국 주식은 개인 공매도가 사실상 막혀
있으므로 롱온리로만 돌린다. 매 리밸런스에 최근 L일 수익률 상위 N종목 등가중 보유,
(옵션) 등가중 지수가 자기 200일선 아래면 전액 현금.

벤치마크: 유니버스 전체 등가중 매수&보유(EW-B&H). 알파 = 전략 CAGR - EW-B&H CAGR.

데이터는 app/kis_ohlcv_cache.py 로 종목당 1회만 받아 CSV 캐시(볼륨) 재사용.

실행:
  docker run --rm --entrypoint python --env-file ../.env \
    -v "$PWD:/app/app" -v "$PWD/../.kis-cache:/app/cache" \
    -e ROTATION_MARKET=KR ochestration-trading-momentum-rotation -m app.kr_us_rotation_backtest
"""
from __future__ import annotations

import itertools
import os

import numpy as np
import pandas as pd
import yfinance as yf

from app.kis_auth import issue_token
from app.kis_ohlcv_cache import warm_cache

MARKET = os.environ.get("ROTATION_MARKET", "KR").upper()
SINCE = os.environ.get("ROTATION_SINCE", "2018-01-01")
UNTIL = os.environ.get("ROTATION_UNTIL") or None
COST_PER_SIDE_PCT = float(os.environ.get("ROTATION_COST_PCT", "0.10"))

LOOKBACKS = [20, 60, 120]
REBALS = [5, 10, 20]
TOP_NS = [3, 5, 8, 10]
REGIMES = ["none", "ew_sma200"]

# 국장 쪽이 연회전율 35배(상위 조합 기준)로 과매매에다 MDD도 39~63%로 컸다 — 리밸런스
# 주기와 무관하게 매일 확인하는 손절(포지션이 다음 리밸런스까지 무방비였던 게 MDD 주범)과,
# 순위 버퍼(상위N 밖으로 살짝 밀렸다고 바로 안 파는 히스테리시스로 회전율을 줄임)를 추가.
STOP_LOSS_PCT = float(os.environ.get("ROTATION_STOP_LOSS_PCT", "0.15"))
RANK_BUFFER = int(os.environ.get("ROTATION_RANK_BUFFER", "2"))

# 워크포워드 검증에서 국장 OOS MDD가 58.9%(IS 27.5%)로 튄 걸 보고 손실 최소화 추가 검토.
# app/portfolio_guard.py의 계좌 전체 킬스위치 개념(종목별 손절과 별개로 전체 드로다운이
# 임계값을 넘으면 전량 청산+정지)과, 등가중 대신 변동성이 큰 종목 비중을 줄이는 인버스
# 변동성 가중을 둘 다 옵션으로 추가 — 기본값은 꺼짐(이전 결과와 비교 가능하게).
PORTFOLIO_KILL_DD = float(os.environ.get("ROTATION_KILL_DD", "0")) or None  # 0/미설정=꺼짐
KILL_COOLDOWN_DAYS = int(os.environ.get("ROTATION_KILL_COOLDOWN_DAYS", "20"))
VOL_WEIGHTED = os.environ.get("ROTATION_VOL_WEIGHTED", "false").lower() == "true"
VOL_LOOKBACK = int(os.environ.get("ROTATION_VOL_LOOKBACK", "20"))


def _universe() -> list[tuple]:
    if MARKET == "KR":
        from app.ema_cross_watchlist import STOCK_UNIVERSE
        return [("KR", code) for code, _ in STOCK_UNIVERSE]
    from app.us_swing_search import STOCK_UNIVERSE
    return [("US", sym, excd) for sym, excd, _ in STOCK_UNIVERSE]


def _load_closes_yf() -> pd.DataFrame:
    """HANTOO_TEST_KEY가 없을 때 쓰는 대체 경로 — KIS 캐시 대신 yfinance 공개 시세로
    같은 모양(날짜 x 종목, Close가)의 DataFrame을 만든다. KR은 6자리 코드에 .KS를
    먼저 시도하고 데이터가 없으면 .KQ(코스닥)로 재시도한다."""
    if MARKET == "KR":
        from app.ema_cross_watchlist import STOCK_UNIVERSE
        tickers, suffixes = [code for code, _ in STOCK_UNIVERSE], (".KS", ".KQ")
    else:
        from app.us_swing_search import STOCK_UNIVERSE
        tickers, suffixes = [sym for sym, _, _ in STOCK_UNIVERSE], ("",)

    series: dict[str, pd.Series] = {}
    for ticker in tickers:
        for suffix in suffixes:
            try:
                hist = yf.Ticker(f"{ticker}{suffix}").history(start=SINCE, end=UNTIL, auto_adjust=True)
            except Exception as exc:
                print(f"  {ticker}{suffix}: 조회 실패 ({exc})")
                continue
            if not hist.empty:
                series[ticker] = hist["Close"].tz_localize(None)
                break
        else:
            print(f"  {ticker}: 데이터 없음 (시도: {[s or 'KIS기본' for s in suffixes]})")
    return pd.DataFrame(series).sort_index()


def _load_closes() -> pd.DataFrame:
    if os.environ.get("HANTOO_TEST_KEY"):
        token = issue_token()
        frames = warm_cache(_universe(), token, SINCE, UNTIL)
        closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()}).sort_index()
    else:
        print("[안내] HANTOO_TEST_KEY가 없어 KIS 대신 yfinance 공개 시세로 대체 수집합니다.")
        closes = _load_closes_yf()
    # 거래일 기준 정렬 — 최소 60% 종목이 값 있는 날만
    closes = closes[closes.notna().mean(axis=1) >= 0.6]
    return closes


def _metrics(equity_curve: pd.Series, daily: pd.Series) -> dict:
    years = (equity_curve.index[-1] - equity_curve.index[0]).days / 365.25
    final = equity_curve.iloc[-1]
    cagr = (final ** (1 / years) - 1) * 100 if final > 0 and years > 0 else -100.0
    peak = equity_curve.cummax()
    mdd = ((peak - equity_curve) / peak).max() * 100
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    calmar = cagr / mdd if mdd > 0 else float("inf")
    yearly = {}
    for yr, grp in equity_curve.groupby(equity_curve.index.year):
        yearly[int(yr)] = round((grp.iloc[-1] / grp.iloc[0] - 1) * 100, 1)
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe, "calmar": calmar,
            "final": final, "yearly": yearly}


def _ew_buy_hold(closes: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    rets = closes.pct_change().fillna(0.0)
    # 시작 시점 존재 종목 등가중, 이후 드리프트 허용(진짜 매수&보유)
    valid0 = closes.iloc[0].notna()
    w0 = valid0 / valid0.sum()
    shares = w0  # 가격=정규화 아님; 단순히 등가중 인덱스로 근사
    port_daily = (rets * w0).sum(axis=1)  # 등가중 리밸런스 근사(일간). B&H 하한으로 충분
    eq = (1 + port_daily).cumprod()
    return eq, port_daily


def _sim(closes: pd.DataFrame, lookback: int, rebal: int, top_n: int, regime: str,
          eval_start: "pd.Timestamp | None" = None) -> dict:
    """eval_start가 주어지면 그 날짜 이전은 신호 생성용 웜업으로만 쓰고(계속 리밸런스는
    돌아가되) 성과 지표는 eval_start 이후 구간만으로 계산한다 — 워크포워드(아웃오브샘플)
    검증용."""
    rets = closes.pct_change()
    mom = closes.pct_change(lookback)
    vol = rets.rolling(VOL_LOOKBACK).std()
    ew_index = (1 + rets.mean(axis=1).fillna(0.0)).cumprod()
    ew_sma = ew_index.rolling(200).mean()

    dates = closes.index
    start = max(lookback + 1, 200)
    w = pd.Series(0.0, index=closes.columns)
    last_w = w.copy()
    entry_price: dict[str, float] = {}
    daily = []
    idx = []
    turnovers = []

    equity = 1.0
    peak = 1.0
    halted_until = -1

    for i in range(start, len(dates)):
        d = dates[i]
        g = (w * rets.loc[d].fillna(0.0)).sum()

        # 일일 손절 — 리밸런스 주기와 무관하게 매일 확인한다. 원래는 포지션이 다음
        # 리밸런스까지 무방비로 노출돼 있어서(며칠~몇주) MDD를 키우는 주범이었다.
        stopped = [
            sym for sym in entry_price
            if w.get(sym, 0.0) > 0 and pd.notna(closes.loc[d, sym])
            and closes.loc[d, sym] <= entry_price[sym] * (1 - STOP_LOSS_PCT)
        ]
        if stopped:
            for sym in stopped:
                w[sym] = 0.0
                del entry_price[sym]
            turn = len(stopped) / top_n
            turnovers.append(turn)
            g -= turn * COST_PER_SIDE_PCT / 100

        equity *= (1 + g)
        peak = max(peak, equity)

        # 계좌 전체 킬스위치(app/portfolio_guard.py와 같은 개념) — 종목별 손절과 별개로,
        # 전체 드로다운이 임계값을 넘으면 전량 청산하고 일정 기간 재진입을 금지한다.
        # 쿨다운 동안은 현금이라 equity가 그대로라 peak를 트리거 시점으로 리셋해둔다
        # (그래야 "회복"을 현금 상태에서 기다리는 데드락이 안 생긴다).
        if PORTFOLIO_KILL_DD is not None and halted_until < i:
            dd = 1 - equity / peak
            if dd >= PORTFOLIO_KILL_DD:
                turn = w.abs().sum()
                if turn > 0:
                    cost = turn * COST_PER_SIDE_PCT / 100
                    g -= cost
                    equity *= (1 - cost)
                    turnovers.append(turn)
                w = pd.Series(0.0, index=closes.columns)
                last_w = w.copy()
                entry_price.clear()
                halted_until = i + KILL_COOLDOWN_DAYS
                peak = equity

        daily.append(g)
        idx.append(d)

        if halted_until >= i:
            continue  # 쿨다운 중엔 리밸런스도 건너뜀(현금 유지)

        if (i - start) % rebal == 0:
            cash = regime == "ew_sma200" and ew_index.iloc[i] < ew_sma.iloc[i]
            m = mom.loc[d].dropna()
            m = m[closes.loc[d].notna()]
            nw = pd.Series(0.0, index=closes.columns)
            if not cash and len(m) >= top_n:
                ranked = list(m.sort_values(ascending=False).index)
                # 순위 버퍼(히스테리시스) — 기존 보유종목이 top_n 밖으로 살짝만 밀렸으면
                # (top_n+RANK_BUFFER 안이면) 그대로 유지해서 노이즈성 회전을 줄인다.
                buffer_zone = set(ranked[:top_n + RANK_BUFFER])
                held = {s for s in w.index if w[s] > 0}
                keep = held & buffer_zone
                new_entries = [s for s in ranked if s not in keep][:max(top_n - len(keep), 0)]
                selected = keep | set(new_entries)
                if selected:
                    if VOL_WEIGHTED:
                        # 변동성이 큰 종목 비중을 줄인다(역변동성 가중) — 등가중이면
                        # 가장 출렁이는 종목이 드로다운을 불균형하게 키운다.
                        sel_vol = vol.loc[d, list(selected)].replace(0, np.nan)
                        inv_vol = (1.0 / sel_vol).fillna(0.0)
                        if inv_vol.sum() > 0:
                            nw[inv_vol.index] = inv_vol / inv_vol.sum()
                        else:
                            nw[list(selected)] = 1.0 / len(selected)
                    else:
                        nw[list(selected)] = 1.0 / len(selected)
            turn = (nw - last_w).abs().sum()
            turnovers.append(turn)
            # 비용은 다음 날 반영되도록 당일 수익에서 차감
            daily[-1] -= turn * COST_PER_SIDE_PCT / 100
            for sym in nw.index:
                if nw[sym] > 0 and sym not in entry_price:
                    entry_price[sym] = closes.loc[d, sym]
                elif nw[sym] == 0 and sym in entry_price:
                    del entry_price[sym]
            w, last_w = nw, nw.copy()

    dseries = pd.Series(daily, index=idx)
    if eval_start is not None:
        dseries = dseries[dseries.index >= eval_start]
    eq = (1 + dseries).cumprod()
    r = _metrics(eq, dseries)
    r["ann_turn"] = float(np.sum(turnovers)) / ((idx[-1] - idx[0]).days / 365.25)
    r["daily"] = dseries  # 멀티전략 블렌딩용(app/multi_strategy_blend_backtest.py) — 기존 호출부엔 영향 없음
    return r


def run() -> None:
    print(f"[{MARKET}] 캐시 워밍/로드 (since {SINCE})...")
    closes = _load_closes()
    print(f"유효 {closes.shape[1]}종목, {closes.index[0].date()} ~ {closes.index[-1].date()} "
          f"({closes.shape[0]}거래일)\n")

    bh_eq, bh_daily = _ew_buy_hold(closes)
    bh = _metrics(bh_eq, bh_daily)
    print(f"[벤치마크] 등가중 매수&보유: CAGR {bh['cagr']:+.1f}%, MDD {bh['mdd']:.1f}%, "
          f"Sharpe {bh['sharpe']:.2f}, Calmar {bh['calmar']:.2f}")
    print(f"  연도별 {bh['yearly']}\n")

    rows = []
    for lb, rb, tn, rg in itertools.product(LOOKBACKS, REBALS, TOP_NS, REGIMES):
        r = _sim(closes, lb, rb, tn, rg)
        r.update(lb=lb, rb=rb, tn=tn, rg=rg, alpha=r["cagr"] - bh["cagr"])
        rows.append(r)

    rows.sort(key=lambda x: -x["calmar"])
    print(f"{'룩백':>5} {'리밸':>5} {'TopN':>5} {'레짐':>10} | {'CAGR':>8} {'MDD':>7} "
          f"{'Sharpe':>7} {'Calmar':>7} {'알파':>8} {'연회전':>7}")
    print("-" * 90)
    for r in rows[:20]:
        print(f"{r['lb']:>5} {r['rb']:>5} {r['tn']:>5} {r['rg']:>10} | {r['cagr']:>+7.1f}% "
              f"{r['mdd']:>6.1f}% {r['sharpe']:>7.2f} {r['calmar']:>7.2f} {r['alpha']:>+7.1f}% "
              f"{r['ann_turn']:>6.1f}x")

    beat = [r for r in rows if r["alpha"] > 0]
    print(f"\nEW-B&H 이긴 조합: {len(beat)}/{len(rows)}")
    print("\n[상위 5 조합 연도별]")
    for r in rows[:5]:
        print(f"  룩백{r['lb']}/리밸{r['rb']}/top{r['tn']}/{r['rg']}: {r['yearly']}")
    best = rows[0]
    print(f"\n최고 Calmar: 룩백{best['lb']}/리밸{best['rb']}/top{best['tn']}/{best['rg']} "
          f"CAGR {best['cagr']:+.1f}% vs B&H {bh['cagr']:+.1f}% (알파 {best['alpha']:+.1f}%), "
          f"MDD {best['mdd']:.1f}% vs {bh['mdd']:.1f}%")
    # yfinance 경로는 auto_adjust=True라 배당이 과거 종가에 이미 역산 반영돼 있다(배당 재투자
    # 기준 조정종가) — KIS 경로(fetch_ohlcv_kis)는 원종가라 배당 미반영. 데이터소스에 따라
    # 캐비어를 다르게 출력한다.
    dividend_note = "배당 재투자 반영(조정종가)" if not os.environ.get("HANTOO_TEST_KEY") else "배당 미반영"
    print(f"\n비용가정: 편도 {COST_PER_SIDE_PCT}% (수수료+세금+슬리피지). "
          f"{dividend_note}, 생존편향 있음(현재 유니버스 고정).")


def run_walkforward() -> None:
    """IS(인샘플) 구간만으로 최고 Calmar 조합을 고른 다음, 그 조합을 OOS(아웃오브샘플,
    인샘플 종료 이후) 구간에서만 평가한다 — 72조합 스윕으로 고른 "최고"가 전체 기간에
    커브핏된 게 아닌지 확인하는 워크포워드 검증. README 미해결 문제 4번
    ("마하세븐 포트폴리오는 워크포워드 검증 전")과 같은 우려가 Top-N 로테이션에도
    적용될 수 있어 먼저 이걸로 검증한다."""
    is_end = pd.Timestamp(os.environ.get("ROTATION_IS_END", "2022-12-31"))
    print(f"[{MARKET}] 캐시 워밍/로드 (since {SINCE})...")
    closes = _load_closes()
    print(f"유효 {closes.shape[1]}종목, {closes.index[0].date()} ~ {closes.index[-1].date()}\n")

    closes_is = closes[closes.index <= is_end]
    print(f"인샘플(IS) 구간: {closes_is.index[0].date()} ~ {closes_is.index[-1].date()} "
          f"({len(closes_is)}거래일) — 이 구간만 보고 최고 조합을 고른다.\n")

    rows = []
    for lb, rb, tn, rg in itertools.product(LOOKBACKS, REBALS, TOP_NS, REGIMES):
        r = _sim(closes_is, lb, rb, tn, rg)
        r.update(lb=lb, rb=rb, tn=tn, rg=rg)
        rows.append(r)
    rows.sort(key=lambda x: -x["calmar"])
    best = rows[0]
    print(f"IS 최고 Calmar: 룩백{best['lb']}/리밸{best['rb']}/top{best['tn']}/{best['rg']} "
          f"— IS 성과 CAGR {best['cagr']:+.1f}%, MDD {best['mdd']:.1f}%, Calmar {best['calmar']:.2f}\n")

    oos_start = is_end + pd.Timedelta(days=1)
    oos_closes = closes[closes.index > is_end]
    oos_r = _sim(closes, best["lb"], best["rb"], best["tn"], best["rg"], eval_start=oos_start)
    bh_eq, bh_daily = _ew_buy_hold(oos_closes)
    bh_oos = _metrics(bh_eq, bh_daily)

    print(f"OOS 구간: {oos_start.date()} ~ {closes.index[-1].date()} ({len(oos_closes)}거래일)")
    print(f"  전략(IS에서 고른 그 조합 그대로): CAGR {oos_r['cagr']:+.1f}%, MDD {oos_r['mdd']:.1f}%, "
          f"Sharpe {oos_r['sharpe']:.2f}, Calmar {oos_r['calmar']:.2f}")
    print(f"  벤치마크(OOS 구간 등가중 매수&보유): CAGR {bh_oos['cagr']:+.1f}%, MDD {bh_oos['mdd']:.1f}%, "
          f"Sharpe {bh_oos['sharpe']:.2f}")
    alpha_oos = oos_r["cagr"] - bh_oos["cagr"]
    verdict = "B&H를 이김 — 커브핏 아닐 가능성 높음" if alpha_oos > 0 else "B&H에 못 미침 — 커브핏 의심, 주의 필요"
    print(f"  OOS 알파: {alpha_oos:+.1f}%p -> {verdict}")


def _sim_long_short(closes: pd.DataFrame, lookback: int, rebal: int, top_n: int,
                     eval_start: "pd.Timestamp | None" = None) -> dict:
    """코인 momentum_rotation과 같은 달러중립 롱숏 구조 — 상위 top_n 롱 + 하위 top_n 숏,
    각각 0.5/top_n씩 배분(합쳐서 시장노출 0에 가깝게). 백테스트 전용 — 국내는 개인
    공매도가 사실상 막혀있고 KIS API에도 숏 주문 TR ID가 없어서 실거래 전환은 별도 검토
    필요(대주 가능 여부, 차입비용 등 여기서는 반영 안 함).

    롱온리 버전(_sim)의 손절/순위버퍼/킬스위치를 롱+숏 양쪽에 그대로 적용한다 — 숏은
    방향이 반대라 손절 조건(가격이 올라야 손절)만 부호를 뒤집는다. 레짐필터는 안 쓴다
    (달러중립 북이라 시장 방향 타이밍에 덜 의존해야 의미가 있음)."""
    rets = closes.pct_change()
    mom = closes.pct_change(lookback)

    dates = closes.index
    start = max(lookback + 1, 200)
    w = pd.Series(0.0, index=closes.columns)
    last_w = w.copy()
    entry_price: dict[str, float] = {}
    daily = []
    idx = []
    turnovers = []

    equity = 1.0
    peak = 1.0
    halted_until = -1

    for i in range(start, len(dates)):
        d = dates[i]
        g = (w * rets.loc[d].fillna(0.0)).sum()

        stopped = []
        for sym, side_w in w.items():
            if side_w == 0 or sym not in entry_price or pd.isna(closes.loc[d, sym]):
                continue
            price = closes.loc[d, sym]
            if side_w > 0 and price <= entry_price[sym] * (1 - STOP_LOSS_PCT):
                stopped.append(sym)
            elif side_w < 0 and price >= entry_price[sym] * (1 + STOP_LOSS_PCT):
                stopped.append(sym)
        if stopped:
            for sym in stopped:
                w[sym] = 0.0
                del entry_price[sym]
            turn = len(stopped) / (top_n * 2)
            turnovers.append(turn)
            g -= turn * COST_PER_SIDE_PCT / 100

        equity *= (1 + g)
        peak = max(peak, equity)

        if PORTFOLIO_KILL_DD is not None and halted_until < i:
            dd = 1 - equity / peak
            if dd >= PORTFOLIO_KILL_DD:
                turn = w.abs().sum()
                if turn > 0:
                    cost = turn * COST_PER_SIDE_PCT / 100
                    g -= cost
                    equity *= (1 - cost)
                    turnovers.append(turn)
                w = pd.Series(0.0, index=closes.columns)
                last_w = w.copy()
                entry_price.clear()
                halted_until = i + KILL_COOLDOWN_DAYS
                peak = equity

        daily.append(g)
        idx.append(d)

        if halted_until >= i:
            continue

        if (i - start) % rebal == 0:
            m = mom.loc[d].dropna()
            m = m[closes.loc[d].notna()]
            nw = pd.Series(0.0, index=closes.columns)
            if len(m) >= top_n * 2:
                ranked = list(m.sort_values(ascending=False).index)
                long_buffer = set(ranked[:top_n + RANK_BUFFER])
                short_buffer = set(ranked[-(top_n + RANK_BUFFER):])
                held_long = {s for s in w.index if w[s] > 0}
                held_short = {s for s in w.index if w[s] < 0}

                keep_long = held_long & long_buffer
                new_long = [s for s in ranked if s not in keep_long][:max(top_n - len(keep_long), 0)]
                sel_long = keep_long | set(new_long)

                keep_short = held_short & short_buffer
                new_short_candidates = [s for s in reversed(ranked) if s not in keep_short and s not in sel_long]
                new_short = new_short_candidates[:max(top_n - len(keep_short), 0)]
                sel_short = keep_short | set(new_short)

                if sel_long:
                    nw[list(sel_long)] = 0.5 / len(sel_long)
                if sel_short:
                    nw[list(sel_short)] = -0.5 / len(sel_short)
            turn = (nw - last_w).abs().sum()
            turnovers.append(turn)
            daily[-1] -= turn * COST_PER_SIDE_PCT / 100
            for sym in nw.index:
                if nw[sym] != 0 and sym not in entry_price:
                    entry_price[sym] = closes.loc[d, sym]
                elif nw[sym] == 0 and sym in entry_price:
                    del entry_price[sym]
            w, last_w = nw, nw.copy()

    dseries = pd.Series(daily, index=idx)
    if eval_start is not None:
        dseries = dseries[dseries.index >= eval_start]
    eq = (1 + dseries).cumprod()
    r = _metrics(eq, dseries)
    r["ann_turn"] = float(np.sum(turnovers)) / ((idx[-1] - idx[0]).days / 365.25)
    r["daily"] = dseries  # 멀티전략 블렌딩용(app/multi_strategy_blend_backtest.py) — 기존 호출부엔 영향 없음
    return r


def run_long_short() -> None:
    """백테스트 전용 롱숏 탐색 — 실거래 전환은 공매도 가능 여부(특히 국내)를 먼저
    확인해야 한다. 롱온리 버전과 같은 비용가정/유니버스로 직접 비교할 수 있게 짰다."""
    print(f"[{MARKET}] 캐시 워밍/로드 (since {SINCE})...")
    closes = _load_closes()
    print(f"유효 {closes.shape[1]}종목, {closes.index[0].date()} ~ {closes.index[-1].date()}\n")

    bh_eq, bh_daily = _ew_buy_hold(closes)
    bh = _metrics(bh_eq, bh_daily)
    print(f"[벤치마크] 등가중 매수&보유: CAGR {bh['cagr']:+.1f}%, MDD {bh['mdd']:.1f}%\n")

    rows = []
    for lb, rb, tn in itertools.product(LOOKBACKS, REBALS, TOP_NS):
        r = _sim_long_short(closes, lb, rb, tn)
        r.update(lb=lb, rb=rb, tn=tn, alpha=r["cagr"] - bh["cagr"])
        rows.append(r)
    rows.sort(key=lambda x: -x["calmar"])

    print(f"{'룩백':>5} {'리밸':>5} {'TopN':>5} | {'CAGR':>8} {'MDD':>7} "
          f"{'Sharpe':>7} {'Calmar':>7} {'알파':>8} {'연회전':>7}")
    print("-" * 80)
    for r in rows[:15]:
        print(f"{r['lb']:>5} {r['rb']:>5} {r['tn']:>5} | {r['cagr']:>+7.1f}% "
              f"{r['mdd']:>6.1f}% {r['sharpe']:>7.2f} {r['calmar']:>7.2f} {r['alpha']:>+7.1f}% "
              f"{r['ann_turn']:>6.1f}x")

    beat = [r for r in rows if r["alpha"] > 0]
    print(f"\nEW-B&H 이긴 조합: {len(beat)}/{len(rows)}")
    best = rows[0]
    print(f"\n최고 Calmar: 룩백{best['lb']}/리밸{best['rb']}/top{best['tn']} "
          f"CAGR {best['cagr']:+.1f}% vs B&H {bh['cagr']:+.1f}% (알파 {best['alpha']:+.1f}%), "
          f"MDD {best['mdd']:.1f}% vs {bh['mdd']:.1f}%")
    print("\n주의: 백테스트 전용(공매도 체결/대주 가능여부/차입비용 미반영). "
          "배당 재투자 반영(조정종가), 생존편향 있음.")


if __name__ == "__main__":
    if os.environ.get("LONG_SHORT", "false").lower() == "true":
        run_long_short()
    elif os.environ.get("WALKFORWARD", "false").lower() == "true":
        run_walkforward()
    else:
        run()
