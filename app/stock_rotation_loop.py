"""국장/미장 top-N 상대모멘텀 로테이션 페이퍼봇 (KIS 모의투자).

코인 momentum_rotation_loop 과 같은 논리: 매 리밸런스에 최근 L일 수익률 상위 K종목
등가중 보유, 나머지 처분. 개인 공매도가 막혀 있으니 롱온리. (옵션) 등가중 유니버스
지수가 자기 200일선 아래면 전액 현금.

KIS 모의는 장마감 직후 주문을 거부하므로(40580000 실측), kr_swing 과 같은 2단계 방식:
  1) 장마감 후 하루 1회: 목표 바스켓 계산 → pending_sells/pending_buys 큐 적재 (plan)
  2) 다음 장중: 큐를 시장가(국내)/마켓터블리밋(해외)로 소비 (consume)

일봉 시세는 app/kis_ohlcv_cache.py 로컬 CSV 캐시(볼륨) 재사용 — 매 리밸런스마다 마지막
봉 5일치만 증분 조회. 실제 보유/현금은 KIS 잔고조회가 진실 소스.

env:
  ROTATION_MARKET      KR | US
  ROTATION_LOOKBACK    모멘텀 룩백일 (KR 기본 20, US 기본 120)
  ROTATION_REBAL_DAYS  리밸런스 간격일 (기본 10 / US 20)
  ROTATION_TOP_K       보유 종목 수 (기본 8)
  ROTATION_REGIME      none | ew_sma200 (기본 ew_sma200)
  ROTATION_BUDGET      운용 예산 (KR KRW, US USD). 미지정 시 watchlist 기본예산.
  ROTATION_STATE_PATH  상태파일 경로
"""
from __future__ import annotations

import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

import pandas as pd

from app.kis_auth import issue_token
from app.kis_ohlcv_cache import cached_ohlcv
from app.stock_rotation_state import load_state, log_event, now_iso, save_state

MARKET = os.environ.get("ROTATION_MARKET", "KR").upper()
TOP_K = int(os.environ.get("ROTATION_TOP_K", "8"))
REGIME = os.environ.get("ROTATION_REGIME", "ew_sma200")
LOOP_SLEEP_SECONDS = int(os.environ.get("ROTATION_LOOP_SLEEP_SECONDS", "60"))
TOKEN_TTL_SECONDS = 12 * 3600
HISTORY_SINCE = "2018-01-01"

if MARKET == "KR":
    LOOKBACK = int(os.environ.get("ROTATION_LOOKBACK", "20"))
    REBAL_DAYS = int(os.environ.get("ROTATION_REBAL_DAYS", "10"))
    TZ = ZoneInfo("Asia/Seoul")
    ORDER_WINDOW = (dt.time(9, 5), dt.time(15, 15))
    PLAN_AFTER = dt.time(15, 40)
else:
    LOOKBACK = int(os.environ.get("ROTATION_LOOKBACK", "120"))
    REBAL_DAYS = int(os.environ.get("ROTATION_REBAL_DAYS", "20"))
    TZ = ZoneInfo("America/New_York")
    ORDER_WINDOW = (dt.time(9, 40), dt.time(15, 50))
    PLAN_AFTER = dt.time(16, 5)


# ----- 시장별 어댑터 -------------------------------------------------------------

def _universe() -> list[tuple[str, str, str]]:
    """[(symbol, name, excd)] — 국내는 excd="" ."""
    if MARKET == "KR":
        from app.ema_cross_watchlist import STOCK_UNIVERSE
        return [(code, name, "") for code, name in STOCK_UNIVERSE]
    from app.us_swing_search import STOCK_UNIVERSE
    return [(sym, name, excd) for sym, excd, name in STOCK_UNIVERSE]


def _default_budget() -> float:
    if MARKET == "KR":
        from app.kr_watchlist import CAPITAL_BUDGET_KRW
        return CAPITAL_BUDGET_KRW
    from app.us_watchlist import CAPITAL_BUDGET_USD
    return CAPITAL_BUDGET_USD


BUDGET = float(os.environ.get("ROTATION_BUDGET", "0")) or _default_budget()
_EXCD = {s: e for s, _, e in _universe()}
_NAMES = {s: n for s, n, _ in _universe()}


def _held(token: str) -> dict[str, int]:
    if MARKET == "KR":
        from app.kis_order import inquire_balance
        from app.kr_watchlist import ACNT_PRDT_CD, CANO
        body = inquire_balance(token, CANO, ACNT_PRDT_CD)
        if body.get("rt_cd") != "0":
            raise RuntimeError(f"잔고조회 실패: {body.get('msg_cd')} {body.get('msg1')}")
        return {r["pdno"]: int(r["hldg_qty"]) for r in body.get("output1", []) if int(r.get("hldg_qty", "0")) > 0}
    from app.kis_overseas_order import inquire_balance
    from app.us_watchlist import ACNT_PRDT_CD, CANO
    out: dict[str, int] = {}
    for excd in ("NASD", "NYSE"):
        body = inquire_balance(token, CANO, ACNT_PRDT_CD, excd)
        if body.get("rt_cd") != "0":
            raise RuntimeError(f"해외잔고조회 실패({excd}): {body.get('msg_cd')} {body.get('msg1')}")
        for r in body.get("output1", []):
            qty = int(float(r.get("ovrs_cblc_qty", "0")))
            if qty > 0:
                out[r["ovrs_pdno"]] = qty
    return out


def _price(token: str, symbol: str) -> float:
    if MARKET == "KR":
        from app.kis_order import inquire_price
        return inquire_price(token, symbol)
    from app.kis_overseas_order import inquire_price
    return inquire_price(token, symbol, _EXCD[symbol])


def _cap_qty(token: str, symbol: str, qty: int, price: float) -> int:
    """해외는 모의계좌 주문가능금액 심사가 근사예산과 어긋나 거부되므로 실제 한도로 클램프."""
    if MARKET == "KR" or qty < 1:
        return qty
    from app.kis_overseas_order import MARKETABLE_LIMIT_BUFFER, inquire_psamount
    from app.us_watchlist import ACNT_PRDT_CD, CANO
    limit = round(price * (1 + MARKETABLE_LIMIT_BUFFER), 2)
    try:
        body = inquire_psamount(token, CANO, ACNT_PRDT_CD, _EXCD[symbol], symbol, limit)
        allowed = int(float(body.get("output", {}).get("max_ord_psbl_qty", "0")))
    except Exception:  # noqa: BLE001
        return qty
    return max(0, min(qty, allowed))


def _buy(token: str, symbol: str, qty: int, price: float) -> dict:
    if MARKET == "KR":
        from app.kis_order import place_order
        from app.kr_watchlist import ACNT_PRDT_CD, CANO
        return place_order(token, CANO, ACNT_PRDT_CD, symbol, "buy", qty)
    from app.kis_overseas_order import MARKETABLE_LIMIT_BUFFER, place_order
    from app.us_watchlist import ACNT_PRDT_CD, CANO
    limit = round(price * (1 + MARKETABLE_LIMIT_BUFFER), 2)
    return place_order(token, CANO, ACNT_PRDT_CD, symbol, _EXCD[symbol], "buy", qty, limit)


def _sell(token: str, symbol: str, qty: int, price: float) -> dict:
    if MARKET == "KR":
        from app.kis_order import place_order
        from app.kr_watchlist import ACNT_PRDT_CD, CANO
        return place_order(token, CANO, ACNT_PRDT_CD, symbol, "sell", qty)
    from app.kis_overseas_order import MARKETABLE_LIMIT_BUFFER, place_order
    from app.us_watchlist import ACNT_PRDT_CD, CANO
    limit = round(price * (1 - MARKETABLE_LIMIT_BUFFER), 2)
    return place_order(token, CANO, ACNT_PRDT_CD, symbol, _EXCD[symbol], "sell", qty, limit)


# ----- 전략 --------------------------------------------------------------------

def _load_closes(token: str) -> pd.DataFrame:
    series = {}
    for symbol, _, excd in _universe():
        try:
            df = cached_ohlcv("KR" if MARKET == "KR" else "US", symbol, token,
                              HISTORY_SINCE, excd=excd or "NAS")
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol} 캐시조회 실패: {exc}", flush=True)
            continue
        if not df.empty:
            series[symbol] = df["Close"]
    closes = pd.DataFrame(series).sort_index()
    return closes[closes.notna().mean(axis=1) >= 0.6]


def _target_basket(closes: pd.DataFrame) -> tuple[list[str], bool]:
    """반환: (상위 K 종목, 레짐이 전액현금인지)."""
    if len(closes) < max(LOOKBACK + 1, 200):
        return [], False
    rets = closes.pct_change()
    ew_index = (1 + rets.mean(axis=1).fillna(0.0)).cumprod()
    cash = REGIME == "ew_sma200" and ew_index.iloc[-1] < ew_index.rolling(200).mean().iloc[-1]
    if cash:
        return [], True
    mom = closes.iloc[-1] / closes.iloc[-1 - LOOKBACK] - 1
    mom = mom.dropna()
    mom = mom[closes.iloc[-1].notna()]
    if len(mom) < TOP_K:
        return [], False
    return list(mom.sort_values(ascending=False).index[:TOP_K]), False


def _market_today() -> dt.date:
    return dt.datetime.now(TZ).date()


def plan_rebalance(token: str, state: dict) -> None:
    today = _market_today().isoformat()
    if state.get("last_plan_date") == today:
        return

    last_rb = state.get("last_rebalance_date")
    if last_rb:
        gap = (_market_today() - dt.date.fromisoformat(last_rb)).days
        if gap < REBAL_DAYS:
            state["last_plan_date"] = today
            save_state(state)
            return

    log_event(state, f"[{MARKET}] 리밸런스 계획 — 룩백 {LOOKBACK}일, top {TOP_K}, 레짐 {REGIME}")
    closes = _load_closes(token)
    if closes.empty:
        log_event(state, f"[{MARKET}] 시세 캐시 비어 계획 보류")
        return
    target, regime_cash = _target_basket(closes)
    if not target and not regime_cash:
        log_event(state, f"[{MARKET}] 데이터 부족 — 계획 보류 ({closes.shape})")
        return

    held = set(_held(token))
    tgt = set(target)
    state["pending_sells"] = sorted(held - tgt)
    state["pending_buys"] = [s for s in target if s not in held]
    state["target_basket"] = target
    state["regime_cash"] = regime_cash
    state["last_plan_date"] = today
    state["symbol_names"] = _NAMES
    log_event(state, f"[{MARKET}] 계획 완료 — 목표 {target or '전액현금'} / "
                     f"매도 {state['pending_sells']} / 매수 {state['pending_buys']}")
    save_state(state)


def _record_equity(token: str, state: dict, held: dict[str, int]) -> None:
    unrealized = 0.0
    hist = state.setdefault("position_history", {})
    for symbol, qty in held.items():
        try:
            price = _price(token, symbol)
        except Exception:  # noqa: BLE001
            continue
        cost = state["entry_cost"].get(symbol, price * qty)
        pnl = price * qty - cost
        unrealized += pnl
        hist.setdefault(symbol, []).append({"ts": now_iso(), "price": price, "unrealized_pnl": pnl})
        hist[symbol] = hist[symbol][-20000:]
    total = state.get("realized_pnl", 0.0) + unrealized
    state["equity_history"] = (state.get("equity_history", []) + [
        {"ts": now_iso(), "total_pnl": total, "equity": BUDGET + total}
    ])[-20000:]


def consume_queue(token: str, state: dict) -> None:
    did_something = False
    held = _held(token)

    for symbol in list(state.get("pending_sells", [])):
        if symbol not in held:
            state["pending_sells"].remove(symbol)
            continue
        try:
            price = _price(token, symbol)
        except Exception as exc:  # noqa: BLE001
            log_event(state, f"[{MARKET}] {symbol} 현재가 실패: {exc}")
            continue
        qty = held[symbol]
        res = _sell(token, symbol, qty, price)
        if res.get("rt_cd") == "0":
            cost = state["entry_cost"].pop(symbol, qty * price)
            realized = qty * price - cost
            state["realized_pnl"] = state.get("realized_pnl", 0.0) + realized
            log_event(state, f"[{MARKET} 매도] {symbol} {qty}주 @약{price:,.2f} 실현 {realized:+,.2f} "
                             f"(누적 {state['realized_pnl']:+,.2f})")
            state["pending_sells"].remove(symbol)
            did_something = True
        else:
            log_event(state, f"[{MARKET} 매도실패] {symbol}: {res.get('msg_cd')} {res.get('msg1')}")

    if state.get("pending_sells"):
        save_state(state)
        return  # 매도 체결 반영 후 다음 사이클에 매수

    held = _held(token)
    n_target = max(len(state.get("target_basket", [])), 1)
    per_name = BUDGET / n_target
    for symbol in list(state.get("pending_buys", [])):
        if symbol in held:
            state["pending_buys"].remove(symbol)
            continue
        try:
            price = _price(token, symbol)
        except Exception as exc:  # noqa: BLE001
            log_event(state, f"[{MARKET}] {symbol} 현재가 실패: {exc}")
            continue
        qty = _cap_qty(token, symbol, int(per_name // price), price)
        if qty < 1:
            log_event(state, f"[{MARKET} 매수스킵] {symbol}: 배정 {per_name:,.0f} / 1주 {price:,.2f} — 주문가능수량 0")
            state["pending_buys"].remove(symbol)
            continue
        res = _buy(token, symbol, qty, price)
        if res.get("rt_cd") == "0":
            state["entry_cost"][symbol] = qty * price
            log_event(state, f"[{MARKET} 매수] {symbol} {qty}주 @약{price:,.2f} (배정 {per_name:,.0f})")
            state["pending_buys"].remove(symbol)
            did_something = True
        else:
            log_event(state, f"[{MARKET} 매수실패] {symbol}: {res.get('msg_cd')} {res.get('msg1')}")
            state["pending_buys"].remove(symbol)

    if not state.get("pending_sells") and not state.get("pending_buys"):
        if state.get("last_plan_date") and state.get("last_rebalance_date") != state["last_plan_date"]:
            state["last_rebalance_date"] = state["last_plan_date"]
            log_event(state, f"[{MARKET}] 리밸런스 완료 — 보유 {sorted(_held(token))}")

    _record_equity(token, state, _held(token))
    save_state(state)
    _ = did_something


def main() -> None:
    state = load_state()
    log_event(state, f"[{MARKET}] 로테이션 페이퍼봇 시작 — 룩백 {LOOKBACK}/리밸 {REBAL_DAYS}일/"
                     f"top {TOP_K}/레짐 {REGIME}, 예산 {BUDGET:,.0f}")
    save_state(state)

    token = issue_token()
    token_at = time.monotonic()

    while True:
        try:
            if time.monotonic() - token_at > TOKEN_TTL_SECONDS:
                token = issue_token()
                token_at = time.monotonic()

            now = dt.datetime.now(TZ)
            if now.weekday() < 5:
                if ORDER_WINDOW[0] <= now.time() <= ORDER_WINDOW[1]:
                    state = load_state()
                    if state.get("pending_sells") or state.get("pending_buys"):
                        consume_queue(token, state)
                    else:
                        _record_equity(token, state, _held(token))
                        save_state(state)
                elif now.time() >= PLAN_AFTER:
                    state = load_state()
                    plan_rebalance(token, state)
        except Exception as exc:  # noqa: BLE001
            state = load_state()
            log_event(state, f"[{MARKET}] 루프 사이클 실패: {exc}")
            save_state(state)

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
