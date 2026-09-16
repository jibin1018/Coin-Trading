"""국내주식 Dual Thrust 돌파매매 — yfinance 기반 로컬 페이퍼 시뮬레이션.

app/kr_dual_thrust_kis_loop.py(한국투자증권 모의투자 계좌 실연동)와 신호 로직은 동일하지만,
KIS API 키가 없어도(발급 대기 중이어도) 바로 돌려볼 수 있게 yfinance 시세 + 로컬 JSON
상태파일로만 동작한다. 나중에 KIS 앱키가 나오면 kr_dual_thrust_kis_loop.py로 갈아타면 됨
(전략 로직은 그대로, 데이터/주문 계층만 다름).

롱온리(숏 없음) — 상단선 돌파 매수, 하단선 이탈 매도.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from app.portfolio_guard import check_drawdown_kill, check_stop_loss_cooldown, record_stop_loss

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 20)

UNIVERSE = [
    ("005930.KS", "삼성전자"), ("035720.KS", "카카오"), ("024110.KS", "기업은행"),
    ("030200.KS", "KT"), ("017670.KS", "SK텔레콤"),
]

K1 = 0.5
K2 = 0.5
# 다른 신규 페이퍼봇들(kr_hybrid 등)은 10만원 기준이지만, 이 봇은 정수 주 단위만 매수하도록
# 일부러 실제 KIS 주문 제약을 그대로 흉내내고 있어서(나중에 실거래 전환 목적) 10만원/5종목
# 균등분배로는 최저가 종목(기업은행 ~2만원)도 1주를 못 산다 — 그래서 예외적으로 100만원 유지.
CAPITAL_BUDGET_KRW = float(os.environ.get("KR_DUAL_THRUST_PAPER_CAPITAL_KRW", "1000000"))  # 100만원 (예외)
POSITION_SIZE_PCT = 1.0 / len(UNIVERSE)
LOOP_SLEEP_SECONDS = int(os.environ.get("KR_DUAL_THRUST_PAPER_LOOP_SLEEP_SECONDS", "60"))

KILL_DD = float(os.environ.get("KR_DUAL_THRUST_PAPER_KILL_DD", "0.25"))
RESET_HALT = os.environ.get("KR_DUAL_THRUST_PAPER_RESET_HALT", "false").lower() == "true"
STOP_COOLDOWN_DAYS = float(os.environ.get("KR_DUAL_THRUST_PAPER_STOP_COOLDOWN_DAYS", "7"))
STOP_COOLDOWN_MAX = int(os.environ.get("KR_DUAL_THRUST_PAPER_STOP_COOLDOWN_MAX", "2"))
LOSS_EXIT_COOLDOWN_PCT = -0.03

STATE_PATH = Path(os.environ.get("KR_DUAL_THRUST_PAPER_STATE_PATH", "kr_dual_thrust_paper_state.json"))
_DEFAULT_STATE: dict = {
    "equity_krw": CAPITAL_BUDGET_KRW,
    "positions": {},  # {symbol: {entry_price, qty, notional_krw}}
    "bounds": {},      # {symbol: {"date": "YYYY-MM-DD", "buy_line": float, "sell_line": float}}
    "cumulative_realized_pnl_krw": 0.0,
    "trade_log": [],
    "hwm_krw": None,
    "drawdown": 0.0,
    "halted": False,
    "stop_loss_events": [],
    "inception_ts": None,
    "equity_history": [],
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_state() -> dict:
    if not STATE_PATH.exists():
        return json.loads(json.dumps(_DEFAULT_STATE))
    state = json.loads(STATE_PATH.read_text())
    for key, default in _DEFAULT_STATE.items():
        state.setdefault(key, json.loads(json.dumps(default)))
    return state


def save_state(state: dict) -> None:
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp_path, STATE_PATH)


def log_event(state: dict, message: str) -> None:
    entry = {"ts": now_iso(), "message": message}
    state.setdefault("trade_log", []).append(entry)
    state["trade_log"] = state["trade_log"][-2000:]
    print(f"[{entry['ts']}] {message}", flush=True)


def _today_kst() -> str:
    return dt.datetime.now(KST).date().isoformat()


def _get_current_price(ticker: str) -> float:
    try:
        data = yf.Ticker(ticker).history(period="1d", interval="1m")
        if not data.empty:
            return float(data["Close"].iloc[-1])
    except Exception as e:
        print(f"[{ticker}] 현재가 조회 실패: {e}")
    return 0.0


def _compute_bounds(ticker: str) -> tuple[float, float] | None:
    try:
        daily = yf.Ticker(ticker).history(period="10d", interval="1d")
    except Exception as e:
        print(f"[{ticker}] 일봉 조회 실패: {e}")
        return None
    if len(daily) < 4:
        return None
    window = daily.iloc[-4:-1]  # 오늘 제외 최근 3일
    hh, hc = window["High"].max(), window["Close"].max()
    lc, ll = window["Close"].min(), window["Low"].min()
    range_val = max(hh - lc, hc - ll)
    today_open = daily["Open"].iloc[-1]
    return float(today_open + K1 * range_val), float(today_open - K2 * range_val)


def _refresh_bounds_if_needed(state: dict) -> None:
    today = _today_kst()
    bounds = state.setdefault("bounds", {})
    for ticker, name in UNIVERSE:
        if bounds.get(ticker, {}).get("date") == today:
            continue
        computed = _compute_bounds(ticker)
        if computed is None:
            continue
        buy_line, sell_line = computed
        bounds[ticker] = {"date": today, "buy_line": buy_line, "sell_line": sell_line}
        log_event(state, f"[{name}] 오늘 상/하단선 계산 — 상단 {buy_line:,.0f} / 하단 {sell_line:,.0f}")


def run_cycle() -> None:
    state = load_state()
    if state.get("inception_ts") is None:
        state["inception_ts"] = now_iso()
        log_event(state, f"KR Dual Thrust(yfinance 페이퍼) 봇 시작 — 예산 {CAPITAL_BUDGET_KRW:,.0f}원, {len(UNIVERSE)}종목")

    _refresh_bounds_if_needed(state)

    positions = state.get("positions", {})
    prices = {ticker: _get_current_price(ticker) for ticker, _name in UNIVERSE}
    prices = {k: v for k, v in prices.items() if v > 0}

    equity = state.get("equity_krw", CAPITAL_BUDGET_KRW) + sum(
        p["qty"] * prices.get(s, p["entry_price"]) for s, p in positions.items()
    )

    if RESET_HALT and state.get("halted"):
        state["halted"] = False
        state["hwm_krw"] = equity
        log_event(state, "halt 수동 해제 — hwm 재설정")

    guard = check_drawdown_kill(state, equity, KILL_DD, hwm_key="hwm_krw")

    if state.get("halted"):
        log_event(state, f"halt 상태 — 거래 중단 중 (dd {guard['dd']*100:.1f}%). "
                          f"해제하려면 KR_DUAL_THRUST_PAPER_RESET_HALT=true 로 재기동")
        save_state(state)
        return

    if guard["kill"]:
        log_event(state, f"킬 스위치 발동 — dd {guard['dd']*100:.1f}% >= {KILL_DD*100:.0f}%. 전량 청산 후 정지.")
        for ticker, pos in list(positions.items()):
            price = prices.get(ticker)
            if price is None:
                continue
            pnl = pos["qty"] * price - pos["notional_krw"]
            state["cumulative_realized_pnl_krw"] = state.get("cumulative_realized_pnl_krw", 0.0) + pnl
            state["equity_krw"] = state.get("equity_krw", 0.0) + pos["notional_krw"] + pnl
            log_event(state, f"[{ticker}] 킬스위치 청산 — 순손익 {pnl:+,.0f}원")
            del positions[ticker]
        state["halted"] = True
        save_state(state)
        return

    bounds = state.get("bounds", {})

    # 1) 보유중 — 하단선 이탈 시 청산
    for ticker, pos in list(positions.items()):
        price = prices.get(ticker)
        b = bounds.get(ticker)
        if price is None or b is None:
            continue
        if price < b["sell_line"]:
            pnl = pos["qty"] * price - pos["notional_krw"]
            state["cumulative_realized_pnl_krw"] = state.get("cumulative_realized_pnl_krw", 0.0) + pnl
            state["equity_krw"] = state.get("equity_krw", 0.0) + pos["notional_krw"] + pnl
            log_event(state, f"[{ticker}] 하단선({b['sell_line']:,.0f}) 이탈 청산 @ {price:,.0f}, 순손익 {pnl:+,.0f}원")
            if pnl < pos["notional_krw"] * LOSS_EXIT_COOLDOWN_PCT:
                record_stop_loss(state)
            del positions[ticker]

    # 2) 미보유 — 상단선 돌파 시 진입
    in_cooldown = check_stop_loss_cooldown(state, STOP_COOLDOWN_DAYS, STOP_COOLDOWN_MAX)
    if in_cooldown:
        log_event(state, f"최근 {STOP_COOLDOWN_DAYS:.0f}일 내 손절 {STOP_COOLDOWN_MAX}회 이상 — 신규진입 보류")
    else:
        for ticker, name in UNIVERSE:
            if ticker in positions:
                continue
            price = prices.get(ticker)
            b = bounds.get(ticker)
            if price is None or b is None:
                continue
            if price > b["buy_line"]:
                notional = CAPITAL_BUDGET_KRW * POSITION_SIZE_PCT
                qty = math.floor(notional / price)
                if qty < 1:
                    log_event(state, f"[{name}] 상단선 돌파했으나 예산 부족(1주 {price:,.0f}원 > 배정예산 {notional:,.0f}원) — 건너뜀")
                    continue
                if state.get("equity_krw", 0.0) < qty * price:
                    log_event(state, f"[{name}] 상단선 돌파했으나 가용잔고 부족 — 건너뜀")
                    continue
                positions[ticker] = {"entry_price": price, "qty": qty, "notional_krw": qty * price}
                state["equity_krw"] = state.get("equity_krw", 0.0) - qty * price
                log_event(state, f"[{name}] 상단선({b['buy_line']:,.0f}) 돌파 매수 — {qty}주 @ {price:,.0f}")

    # 이 사이클에서 체결된 매수/매도를 반영한 최종 평가자산 (위 equity는 사이클 시작 시점 값)
    final_equity = state.get("equity_krw", CAPITAL_BUDGET_KRW) + sum(
        p["qty"] * prices.get(s, p["entry_price"]) for s, p in positions.items()
    )
    state["equity_history"] = (state.get("equity_history", []) + [
        {"ts": now_iso(), "equity": final_equity}
    ])[-1000:]

    log_event(state, f"평가자산 {final_equity:,.0f}원 (보유 {len(positions)}종목, 드로다운 {guard['dd']*100:.1f}%)")
    save_state(state)


def main() -> None:
    print(f"KR Dual Thrust(yfinance 페이퍼) 봇 가동! (예산 {CAPITAL_BUDGET_KRW:,.0f}원, {len(UNIVERSE)}종목)")
    while True:
        now = dt.datetime.now(KST)
        if now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE:
            try:
                run_cycle()
            except Exception as exc:  # noqa: BLE001
                print(f"[오류] 사이클 실패: {exc}", flush=True)
        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
