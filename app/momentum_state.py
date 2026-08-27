"""모멘텀 로테이션 전략(app/momentum_rotation_loop.py) 상태 저장소.

실주문을 넣지 않는 가상(백테스트 모드) 전략이라 kr_state.py/us_state.py와 달리 브로커
잔고를 조회할 필요가 없다 — 가상 자본(START_CAPITAL_USDT)에서 시작해 리밸런스마다
실현손익/수수료만 반영해서 자체적으로 자본을 불려/줄여나간다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get("MOMENTUM_ROTATION_STATE_PATH", "/app/data/momentum_rotation_state.json"))

_DEFAULT_STATE = {
    "positions": {},  # symbol -> {side, weight, entry_price, notional_usdt}
    "trade_log": [],
    "equity_usdt": 0.0,
    "cumulative_realized_pnl_usdt": 0.0,
    "cumulative_fee_usdt": 0.0,
    "unrealized_pnl_usdt": 0.0,
    "inception_ts": None,
    "last_rebalance_ts": None,
    "equity_history": [],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
