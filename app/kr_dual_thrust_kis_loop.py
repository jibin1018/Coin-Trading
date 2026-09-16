"""국내주식 Dual Thrust 돌파매매 — KIS 모의투자 계좌 실연동.

app/dual_thrust_paper_loop.py(코인, yfinance/로컬 JSON 시뮬레이션)와 신호 로직은 같지만,
로컬 시뮬레이션이 아니라 **실제 한국투자증권 모의투자 계좌에 주문이 들어간다**
(app/kis_order.py, kr_swing_loop.py가 실거래에 쓰는 것과 동일한 검증된 API).
지금은 kis_auth.py가 모의투자 도메인만 쓰도록 고정돼 있어 실전 전환은 아직 안 되지만,
실전 앱키/도메인이 준비되면 kis_auth.py만 바꿔서 이 봇도 그대로 실거래로 넘어갈 수 있는 구조.

단순화한 점:
  - 숏 없음(롱온리) — KR 개인계좌는 공매도에 별도 자격/증거금이 필요해서 복잡도를 피함.
    상단 돌파 시 매수, 하단 돌파 시 전량 매도(현금화)만 한다.
  - 워치리스트 5종목 고정 — 모의투자 계좌 초당 호출 제한이 빡빡해서(kis_auth.py 참고)
    가격조회를 매 사이클 여러 종목 반복하는 구조상 5개 이상은 위험.
  - Dual Thrust 상/하단선은 하루 1회만 계산해서 상태에 캐싱(매 사이클 재계산하면 일봉 조회가
    쌓여 위 호출제한에 바로 걸림).

실행 방법: python -m app.kr_dual_thrust_kis_loop
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.kis_auth import issue_token
from app.kis_data import fetch_ohlcv_kis
from app.kis_order import inquire_balance, inquire_price, place_order
from app.kr_watchlist import ACNT_PRDT_CD, CANO
from app.portfolio_guard import check_drawdown_kill, check_stop_loss_cooldown, record_stop_loss
from app.watchdog import run_with_timeout

KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 20)

# 유동성 크고 가격대가 낮아 소액 예산으로도 최소 1주는 살 수 있는 대형주 위주로 구성
UNIVERSE = [
    ("005930", "삼성전자"), ("035720", "카카오"), ("024110", "기업은행"),
    ("030200", "KT"), ("017670", "SK텔레콤"),
]

K1 = 0.5
K2 = 0.5
# 다른 신규 페이퍼봇들(kr_hybrid 등)은 10만원 기준이지만, 이 봇은 정수 주 단위만 매수해서
# (실제 KIS 주문 제약) 10만원/5종목 균등분배로는 최저가 종목(기업은행 ~2만원)도 1주를 못 산다
# — 그래서 예외적으로 100만원 유지.
CAPITAL_BUDGET_KRW = float(os.environ.get("KR_DUAL_THRUST_CAPITAL_KRW", "1000000"))  # 100만원 (예외)
POSITION_SIZE_PCT = 1.0 / len(UNIVERSE)  # 5종목 균등배분
LOOP_SLEEP_SECONDS = int(os.environ.get("KR_DUAL_THRUST_LOOP_SLEEP_SECONDS", "60"))
CYCLE_TIMEOUT_SECONDS = 60
TOKEN_TTL_SECONDS = 12 * 3600

KILL_DD = float(os.environ.get("KR_DUAL_THRUST_KILL_DD", "0.25"))
RESET_HALT = os.environ.get("KR_DUAL_THRUST_RESET_HALT", "false").lower() == "true"
STOP_COOLDOWN_DAYS = float(os.environ.get("KR_DUAL_THRUST_STOP_COOLDOWN_DAYS", "7"))
STOP_COOLDOWN_MAX = int(os.environ.get("KR_DUAL_THRUST_STOP_COOLDOWN_MAX", "2"))
# 개별 손절 없이 하단선 이탈=청산이라 손절이랑 별개지만, 청산 자체가 반복되면 같은 가드로 잡는다.
LOSS_EXIT_COOLDOWN_PCT = -0.03  # 청산이 진입가 대비 -3% 이상 손실이면 "손절"로 집계

STATE_PATH = Path(os.environ.get("KR_DUAL_THRUST_STATE_PATH", "/app/data/kr_dual_thrust_state.json"))
_DEFAULT_STATE: dict = {
    "positions": {},  # {symbol: {entry_price, qty, notional_krw}}
    "bounds": {},      # {symbol: {"date": "YYYY-MM-DD", "buy_line": float, "sell_line": float}}
    "cumulative_realized_pnl_krw": 0.0,
    "trade_log": [],
    "equity_history": [],
    "position_history": {},
    "hwm_krw": None,
    "drawdown": 0.0,
    "halted": False,
    "stop_loss_events": [],
    "inception_ts": None,
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
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
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


def _compute_bounds(token: str, symbol: str) -> tuple[float, float] | None:
    since = (dt.date.today() - dt.timedelta(days=10)).isoformat() + "T00:00:00Z"
    frame = fetch_ohlcv_kis(symbol, token, since, None)
    if len(frame) < 4:
        return None
    window = frame.iloc[-4:-1]  # 오늘 제외 최근 3일
    hh, hc = window["High"].max(), window["Close"].max()
    lc, ll = window["Close"].min(), window["Low"].min()
    range_val = max(hh - lc, hc - ll)
    today_open = frame["Open"].iloc[-1]
    return today_open + K1 * range_val, today_open - K2 * range_val


def _refresh_bounds_if_needed(token: str, state: dict) -> None:
    today = _today_kst()
    bounds = state.setdefault("bounds", {})
    for symbol, _name in UNIVERSE:
        if bounds.get(symbol, {}).get("date") == today:
            continue
        computed = _compute_bounds(token, symbol)
        if computed is None:
            continue
        buy_line, sell_line = computed
        bounds[symbol] = {"date": today, "buy_line": buy_line, "sell_line": sell_line}
        log_event(state, f"[{symbol}] 오늘 상/하단선 계산 — 상단 {buy_line:.0f} / 하단 {sell_line:.0f}")


def run_cycle(token: str) -> None:
    state = load_state()
    if state.get("inception_ts") is None:
        state["inception_ts"] = now_iso()
        log_event(state, f"KR Dual Thrust(KIS 모의투자) 봇 시작 — 예산 {CAPITAL_BUDGET_KRW:,.0f}원, {len(UNIVERSE)}종목")

    _refresh_bounds_if_needed(token, state)

    balance = inquire_balance(token, CANO, ACNT_PRDT_CD)
    try:
        available_krw = float(balance["output2"][0]["dnca_tot_amt"])
    except (KeyError, IndexError, ValueError, TypeError):
        log_event(state, f"잔고조회 실패, 이번 사이클 건너뜀: {balance.get('msg1', balance)}")
        save_state(state)
        return

    positions = state.get("positions", {})
    prices: dict[str, float] = {}
    for symbol, name in UNIVERSE:
        try:
            prices[symbol] = inquire_price(token, symbol)
        except Exception as exc:  # noqa: BLE001
            log_event(state, f"[{symbol}] 현재가 조회 실패 ({exc}) — 이번 사이클 건너뜀")

    equity = available_krw + sum(p["qty"] * prices.get(s, p["entry_price"]) for s, p in positions.items())
    state["equity_krw"] = equity

    if RESET_HALT and state.get("halted"):
        state["halted"] = False
        state["hwm_krw"] = equity
        log_event(state, "halt 수동 해제 — hwm 재설정")

    guard = check_drawdown_kill(state, equity, KILL_DD, hwm_key="hwm_krw")

    if state.get("halted"):
        log_event(state, f"halt 상태 — 거래 중단 중 (dd {guard['dd']*100:.1f}%). "
                          f"해제하려면 KR_DUAL_THRUST_RESET_HALT=true 로 재기동")
        save_state(state)
        return

    if guard["kill"]:
        log_event(state, f"킬 스위치 발동 — dd {guard['dd']*100:.1f}% >= {KILL_DD*100:.0f}%. 전량 청산 후 정지.")
        for symbol, pos in list(positions.items()):
            price = prices.get(symbol)
            if price is None:
                continue
            resp = place_order(token, CANO, ACNT_PRDT_CD, symbol, "sell", pos["qty"])
            if resp.get("rt_cd") == "0":
                pnl = pos["qty"] * price - pos["notional_krw"]
                state["cumulative_realized_pnl_krw"] = state.get("cumulative_realized_pnl_krw", 0.0) + pnl
                log_event(state, f"[{symbol}] 킬스위치 청산 — 순손익 {pnl:+,.0f}원")
                del positions[symbol]
        state["halted"] = True
        save_state(state)
        return

    bounds = state.get("bounds", {})

    # 1) 보유중 종목 — 하단선 이탈 시 청산
    for symbol, pos in list(positions.items()):
        price = prices.get(symbol)
        b = bounds.get(symbol)
        if price is None or b is None:
            continue
        if price < b["sell_line"]:
            resp = place_order(token, CANO, ACNT_PRDT_CD, symbol, "sell", pos["qty"])
            if resp.get("rt_cd") != "0":
                log_event(state, f"[{symbol}] 매도 주문 실패: {resp.get('msg1')}")
                continue
            pnl = pos["qty"] * price - pos["notional_krw"]
            state["cumulative_realized_pnl_krw"] = state.get("cumulative_realized_pnl_krw", 0.0) + pnl
            log_event(state, f"[{symbol}] 하단선({b['sell_line']:.0f}) 이탈 청산 @ {price:.0f}, 순손익 {pnl:+,.0f}원")
            if pnl < pos["notional_krw"] * LOSS_EXIT_COOLDOWN_PCT:
                record_stop_loss(state)
            del positions[symbol]

    # 2) 미보유 종목 — 상단선 돌파 시 진입 (손절 반복 시 신규진입 보류)
    in_cooldown = check_stop_loss_cooldown(state, STOP_COOLDOWN_DAYS, STOP_COOLDOWN_MAX)
    if in_cooldown:
        log_event(state, f"최근 {STOP_COOLDOWN_DAYS:.0f}일 내 손절 {STOP_COOLDOWN_MAX}회 이상 — 신규진입 보류")
    else:
        for symbol, name in UNIVERSE:
            if symbol in positions:
                continue
            price = prices.get(symbol)
            b = bounds.get(symbol)
            if price is None or b is None:
                continue
            if price > b["buy_line"]:
                notional = CAPITAL_BUDGET_KRW * POSITION_SIZE_PCT
                qty = math.floor(notional / price)
                if qty < 1:
                    log_event(state, f"[{symbol}] 상단선 돌파했으나 예산 부족(1주 {price:.0f}원 > 배정예산 {notional:,.0f}원) — 건너뜀")
                    continue
                if available_krw < qty * price:
                    log_event(state, f"[{symbol}] 상단선 돌파했으나 실제 예수금 부족 — 건너뜀")
                    continue
                resp = place_order(token, CANO, ACNT_PRDT_CD, symbol, "buy", qty)
                if resp.get("rt_cd") != "0":
                    log_event(state, f"[{symbol}] 매수 주문 실패: {resp.get('msg1')}")
                    continue
                positions[symbol] = {"entry_price": price, "qty": qty, "notional_krw": qty * price}
                available_krw -= qty * price
                log_event(state, f"[{symbol}] 상단선({b['buy_line']:.0f}) 돌파 매수 — {qty}주 @ {price:.0f}")

    # 상태 기록
    total_pnl = state.get("cumulative_realized_pnl_krw", 0.0)
    for symbol, pos in positions.items():
        price = prices.get(symbol, pos["entry_price"])
        unrealized = pos["qty"] * price - pos["notional_krw"]
        total_pnl += unrealized
        hist = state.setdefault("position_history", {}).setdefault(symbol, [])
        hist.append({"ts": now_iso(), "price": price, "unrealized_pnl_krw": unrealized})
        state["position_history"][symbol] = hist[-20000:]
    state["equity_history"] = (state.get("equity_history", []) + [{"ts": now_iso(), "total_pnl_krw": total_pnl}])[-20000:]

    log_event(state, f"평가자산 {equity:,.0f}원 (보유 {len(positions)}종목, 드로다운 {guard['dd']*100:.1f}%)")
    save_state(state)


def main() -> None:
    log_event(load_state(), "KR Dual Thrust(KIS 모의투자) 루프 프로세스 기동")
    token = issue_token()
    token_issued_at = time.monotonic()

    while True:
        try:
            if time.monotonic() - token_issued_at > TOKEN_TTL_SECONDS:
                token = issue_token()
                token_issued_at = time.monotonic()

            now = dt.datetime.now(KST)
            if now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE:
                run_with_timeout(
                    lambda: run_cycle(token), CYCLE_TIMEOUT_SECONDS,
                    on_timeout=lambda: print(f"[경고] 사이클이 {CYCLE_TIMEOUT_SECONDS}초 넘게 안 끝나 hang으로 보고 포기", flush=True),
                )
        except Exception as exc:  # noqa: BLE001 — 한 사이클 실패로 루프 전체가 죽지 않게
            state = load_state()
            log_event(state, f"[오류] 루프 사이클 실패: {exc}")
            save_state(state)

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
