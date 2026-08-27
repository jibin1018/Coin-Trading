"""미국 장중 분단위 루프 — kr_intraday_risk.py와 동일 구조/예산 로직, 통화만 USD.
모의투자는 지정가만 되므로 매수는 현재가+1%, 매도는 현재가-1%로 넣어 즉시체결을 유도한다."""
from __future__ import annotations

from app.kis_order import inquire_balance as inquire_balance_domestic
from app.kis_overseas_order import MARKETABLE_LIMIT_BUFFER, inquire_balance, inquire_price, place_order
from app.us_state import log_event, save_state
from app.us_watchlist import (
    ACNT_PRDT_CD, CANO, CAPITAL_BUDGET_USD, FX_KRW_PER_USD, MAX_CONCURRENT_POSITIONS,
    MAX_POSITION_FRACTION, RISK_PER_TRADE, STOCK_UNIVERSE, STOP_PCT,
)
from app.strategies import _risk_sized_fraction

_EXCD_BY_SYMBOL = {symbol: excd for symbol, excd, _ in STOCK_UNIVERSE}


def _balance(token: str) -> tuple[dict[str, int], float]:
    """held는 해외잔고조회(VTTS3012R)에서, cash는 계좌가 공용이라 국내 예수금(KRW)을
    대략환율로 환산해서 구한다 — 해외잔고조회 응답엔 현금(예수금) 필드가 없음(실측 확인됨,
    output2는 손익요약만 줌)."""
    held: dict[str, int] = {}
    for excd in ("NASD", "NYSE"):
        body = inquire_balance(token, CANO, ACNT_PRDT_CD, excd)
        if body.get("rt_cd") != "0":
            raise RuntimeError(f"해외잔고조회 실패({excd}): {body.get('msg_cd')} {body.get('msg1')}")
        for row in body.get("output1", []):
            qty = int(float(row.get("ovrs_cblc_qty", "0")))
            if qty > 0:
                held[row["ovrs_pdno"]] = qty

    domestic_body = inquire_balance_domestic(token, CANO, ACNT_PRDT_CD)
    if domestic_body.get("rt_cd") != "0":
        raise RuntimeError(f"예수금(원화) 조회 실패: {domestic_body.get('msg_cd')} {domestic_body.get('msg1')}")
    cash_krw = float(domestic_body["output2"][0]["dnca_tot_amt"]) if domestic_body.get("output2") else 0.0
    cash_usd = cash_krw / FX_KRW_PER_USD
    return held, cash_usd


def _sell_all(token: str, state: dict, symbol: str, qty: int, price: float, reason: str) -> None:
    excd = _EXCD_BY_SYMBOL.get(symbol)
    if excd is None:
        log_event(state, f"[매도실패:{reason}] {symbol}: 워치리스트에 없는 종목(거래소 불명)")
        return
    limit_price = price * (1 - MARKETABLE_LIMIT_BUFFER)
    result = place_order(token, CANO, ACNT_PRDT_CD, symbol, excd, "sell", qty, limit_price)
    if result.get("rt_cd") == "0":
        entry_cost = state["entry_cost"].pop(symbol, qty * price)
        realized = qty * price - entry_cost
        state["realized_pnl_usd"] = state.get("realized_pnl_usd", 0.0) + realized
        log_event(state, f"[매도:{reason}] {symbol} {qty}주 지정가${limit_price:,.2f} 실현손익${realized:+,.2f} "
                          f"(누적실현손익 ${state['realized_pnl_usd']:+,.2f})")
        state["stop_price"].pop(symbol, None)
    else:
        log_event(state, f"[매도실패:{reason}] {symbol}: {result.get('msg_cd')} {result.get('msg1')}")


def check_once(token: str, state: dict) -> None:
    held, _ = _balance(token)

    for symbol, qty in held.items():
        excd = _EXCD_BY_SYMBOL.get(symbol)
        if excd is None:
            continue
        if symbol not in state["stop_price"] or symbol not in state["entry_cost"]:
            try:
                price = inquire_price(token, symbol, excd)
            except Exception as exc:  # noqa: BLE001
                log_event(state, f"[현재가조회실패] {symbol}: {exc}")
                continue
            state["stop_price"].setdefault(symbol, price * (1 - STOP_PCT))
            state["entry_cost"].setdefault(symbol, price * qty)
            log_event(state, f"[상태복구] {symbol}: 손절가/원가 기억 없어 현재가 기준 재설정")

    for symbol in list(state["pending_exits"]):
        excd = _EXCD_BY_SYMBOL.get(symbol)
        if symbol in held and excd:
            try:
                price = inquire_price(token, symbol, excd)
            except Exception as exc:  # noqa: BLE001
                log_event(state, f"[현재가조회실패] {symbol}: {exc}")
                continue
            _sell_all(token, state, symbol, held[symbol], price, "일봉신호")
        state["pending_exits"].remove(symbol)

    for symbol, stop in list(state["stop_price"].items()):
        excd = _EXCD_BY_SYMBOL.get(symbol)
        if symbol not in held or excd is None:
            continue
        try:
            price = inquire_price(token, symbol, excd)
        except Exception as exc:  # noqa: BLE001
            log_event(state, f"[현재가조회실패] {symbol}: {exc}")
            continue
        if price <= stop:
            _sell_all(token, state, symbol, held[symbol], price, f"손절@{stop:.2f}")

    held, cash = _balance(token)
    budget = CAPITAL_BUDGET_USD + state.get("realized_pnl_usd", 0.0)
    deployed = sum(state["entry_cost"].get(s, 0.0) for s in held)
    spendable = min(cash, max(0.0, budget - deployed))
    slots = MAX_CONCURRENT_POSITIONS - len(held)
    for symbol in list(state["pending_entries"]):
        if slots <= 0:
            break
        excd = _EXCD_BY_SYMBOL.get(symbol)
        if symbol in held or excd is None:
            state["pending_entries"].remove(symbol)
            continue
        try:
            price = inquire_price(token, symbol, excd)
        except Exception as exc:  # noqa: BLE001
            log_event(state, f"[현재가조회실패] {symbol}: {exc}")
            continue
        stop = state["stop_price"].get(symbol, price * (1 - STOP_PCT))
        fraction = _risk_sized_fraction(price, stop, RISK_PER_TRADE, MAX_POSITION_FRACTION)
        qty = int((spendable * fraction) // price) if fraction else 0
        state["pending_entries"].remove(symbol)
        if qty < 1:
            log_event(state, f"[진입스킵] {symbol}: 주문가능수량 0 (예산잔여 ${spendable:,.2f}/${budget:,.2f}, 가격 ${price:,.2f})")
            state["stop_price"].pop(symbol, None)
            continue
        limit_price = price * (1 + MARKETABLE_LIMIT_BUFFER)
        result = place_order(token, CANO, ACNT_PRDT_CD, symbol, excd, "buy", qty, limit_price)
        if result.get("rt_cd") == "0":
            cost = qty * price
            state["stop_price"][symbol] = stop
            state["entry_cost"][symbol] = cost
            log_event(state, f"[매수] {symbol} {qty}주 지정가${limit_price:,.2f} 손절가${stop:,.2f} "
                              f"(미장예산 ${deployed + cost:,.2f}/${budget:,.2f} 사용)")
            spendable -= cost
            deployed += cost
            slots -= 1
        else:
            log_event(state, f"[매수실패] {symbol}: {result.get('msg_cd')} {result.get('msg1')}")

    save_state(state)
