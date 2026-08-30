"""코인간 상대모멘텀 로테이션(선물 롱숏) 라이브 루프 — 백테스트에서 검증된 설정
(lookback 14일 / 3일마다 리밸런스 / 상위·하위 8개, 연환산 29.16%, MDD 18.6%)을 그대로 사용.

실주문 없음(백테스트/페이퍼 모드) — 바이낸스 공개 시세 API만으로 가상 포지션·손익을
추적한다. API 키 불필요, 실계좌 리스크 전혀 없음. 나중에 실거래로 전환하려면 이 루프의
판단 로직은 그대로 두고 주문 실행부만 추가하면 된다(kr/us_swing_loop과 동일 원칙).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd

from app.futures_data import fetch_perp_ohlcv
from app.momentum_state import load_state, log_event, now_iso, save_state

UNIVERSE = [
    "BTC", "ETH", "BNB", "XRP", "SOL", "TRX", "DOGE", "ZEC", "LINK", "XMR",
    "ADA", "XLM", "BCH", "LTC", "HBAR", "AVAX", "SUI", "UNI", "NEAR",
    "TAO", "AAVE", "ONDO", "THETA", "DOT", "ENA", "WLD", "ICP", "ETC",
    "POL", "QNT", "ALGO", "ATOM", "RENDER", "JUP", "ARB", "FIL", "VET",
    "CAKE", "TON", "MKR", "LDO", "CRV", "INJ", "OP", "APT", "IMX", "STX",
]

LOOKBACK_DAYS = 14
REBALANCE_EVERY_DAYS = 3
TOP_K = 8
COMMISSION_PCT = 0.04  # 편도, 리밸런스 회전분에만 적용(백테스트와 동일 가정)
START_CAPITAL_USDT = float(os.environ.get("MOMENTUM_ROTATION_START_CAPITAL_USDT", "10000"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("MOMENTUM_ROTATION_CHECK_INTERVAL_SECONDS", "1800"))
SINCE_DAYS_FOR_MOMENTUM = LOOKBACK_DAYS + 10


def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"


def _fetch_current_prices() -> dict[str, float]:
    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    prices = {}
    for base in UNIVERSE:
        try:
            ticker = exchange.fetch_ticker(_perp_symbol(base))
            prices[base] = ticker["last"]
        except Exception as exc:  # noqa: BLE001
            print(f"  {base}: 시세조회 실패 ({exc})", flush=True)
    return prices


def _fetch_momentum_ranking() -> pd.Series:
    since_iso = (datetime.now(timezone.utc) - timedelta(days=SINCE_DAYS_FOR_MOMENTUM)).isoformat()
    momentum = {}
    for base in UNIVERSE:
        try:
            frame = fetch_perp_ohlcv(_perp_symbol(base), "1d", since_iso, None)
        except Exception as exc:  # noqa: BLE001
            print(f"  {base}: 일봉 조회 실패 ({exc})", flush=True)
            continue
        if len(frame) < LOOKBACK_DAYS + 1:
            continue
        closes = frame["Close"]
        momentum[base] = closes.iloc[-1] / closes.iloc[-1 - LOOKBACK_DAYS] - 1
    return pd.Series(momentum)


def _mark_to_market(state: dict, prices: dict[str, float]) -> float:
    """직전 리밸런스 이후 보유 포지션의 미실현손익 합계를 계산한다(자본 자체는 건드리지 않음)."""
    total = 0.0
    for symbol, pos in state["positions"].items():
        price = prices.get(symbol)
        if price is None or not pos.get("entry_price"):
            continue
        side_sign = 1.0 if pos["side"] == "long" else -1.0
        pnl = pos["notional_usdt"] * side_sign * (price / pos["entry_price"] - 1)
        pos["unrealized_pnl_usdt"] = pnl
        total += pnl
    return total


def _rebalance(state: dict, prices: dict[str, float]) -> None:
    momentum = _fetch_momentum_ranking()
    momentum = momentum[momentum.index.isin(prices.keys())]
    if len(momentum) < TOP_K * 2:
        log_event(state, f"모멘텀 데이터 부족({len(momentum)}종목) — 이번 리밸런스 건너뜀")
        return

    # 직전 사이클 미실현손익을 실현손익으로 확정하고 자본에 반영
    unrealized = sum(p.get("unrealized_pnl_usdt", 0.0) for p in state["positions"].values())
    state["cumulative_realized_pnl_usdt"] = state.get("cumulative_realized_pnl_usdt", 0.0) + unrealized
    capital_before = START_CAPITAL_USDT + state["cumulative_realized_pnl_usdt"] - state.get("cumulative_fee_usdt", 0.0)

    ranked = momentum.sort_values(ascending=False)
    longs = list(ranked.index[:TOP_K])
    shorts = list(ranked.index[-TOP_K:])
    weight_each = 0.5 / TOP_K

    old_symbols = set(state["positions"].keys())
    new_symbols = set(longs) | set(shorts)
    turnover_legs = len(old_symbols.symmetric_difference(new_symbols)) + len(old_symbols & new_symbols)
    # 회전(포지션이 바뀌거나 유지되며 재설정되는 경우 전부)에 왕복 아닌 편도 수수료를 적용 —
    # 백테스트(futures_momentum_rotation_probe.py)의 turnover 가정과 동일한 근사치.
    fee = abs(capital_before) * COMMISSION_PCT / 100 * (turnover_legs / max(len(new_symbols), 1))
    state["cumulative_fee_usdt"] = state.get("cumulative_fee_usdt", 0.0) + fee

    capital_after_fee = capital_before - fee
    new_positions = {}
    for symbol in longs:
        new_positions[symbol] = {
            "side": "long", "entry_price": prices[symbol],
            "notional_usdt": capital_after_fee * weight_each, "unrealized_pnl_usdt": 0.0,
        }
    for symbol in shorts:
        new_positions[symbol] = {
            "side": "short", "entry_price": prices[symbol],
            "notional_usdt": capital_after_fee * weight_each, "unrealized_pnl_usdt": 0.0,
        }
    state["positions"] = new_positions
    state["last_rebalance_ts"] = now_iso()
    state["equity_usdt"] = capital_after_fee

    log_event(
        state,
        f"리밸런스 완료 — 자본 {capital_after_fee:,.2f} USDT (직전 미실현 {unrealized:+,.2f} 실현반영, 수수료 -{fee:.2f}) "
        f"롱: {','.join(longs)} / 숏: {','.join(shorts)}",
    )


def run_cycle() -> None:
    state = load_state()
    if state.get("inception_ts") is None:
        state["inception_ts"] = now_iso()
        log_event(state, f"모멘텀 로테이션 백테스트(페이퍼) 루프 시작 — 가상자본 {START_CAPITAL_USDT:,.0f} USDT, "
                          f"{len(UNIVERSE)}종목, lookback {LOOKBACK_DAYS}일/리밸런스 {REBALANCE_EVERY_DAYS}일마다/상위·하위 {TOP_K}개")

    prices = _fetch_current_prices()
    if not prices:
        log_event(state, "시세 조회 전체 실패 — 이번 사이클 건너뜀")
        save_state(state)
        return

    due = state.get("last_rebalance_ts") is None
    if not due:
        last = datetime.fromisoformat(state["last_rebalance_ts"])
        due = datetime.now(timezone.utc) - last >= timedelta(days=REBALANCE_EVERY_DAYS)

    if due:
        _rebalance(state, prices)
    else:
        unrealized = _mark_to_market(state, prices)
        state["unrealized_pnl_usdt"] = unrealized
        state["equity_usdt"] = (
            START_CAPITAL_USDT + state.get("cumulative_realized_pnl_usdt", 0.0)
            - state.get("cumulative_fee_usdt", 0.0) + unrealized
        )

    total_pnl = state["equity_usdt"] - START_CAPITAL_USDT
    state["equity_history"] = (state.get("equity_history", []) + [
        {"ts": now_iso(), "total_pnl_usdt": total_pnl}
    ])[-2000:]

    # 종목별 차트용 — 이미 조회한 가격을 그대로 기록만 한다(추가 API 호출 없음). 지금 보유중인
    # 롱/숏 종목만 남긴다(47종목 전체를 다 남기면 상태파일이 불필요하게 커짐).
    symbol_history = state.setdefault("position_history", {})
    for symbol, pos in state["positions"].items():
        price = prices.get(symbol)
        if price is None:
            continue
        history = symbol_history.setdefault(symbol, [])
        history.append({"ts": now_iso(), "price": price, "unrealized_pnl_usdt": pos.get("unrealized_pnl_usdt", 0.0)})
        symbol_history[symbol] = history[-2000:]

    save_state(state)


def main() -> None:
    print(f"모멘텀 로테이션 페이퍼 루프 시작 (사이클 주기 {CHECK_INTERVAL_SECONDS}초)", flush=True)
    while True:
        try:
            run_cycle()
        except Exception:
            import traceback
            print("사이클 실행 중 오류 발생:", flush=True)
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
