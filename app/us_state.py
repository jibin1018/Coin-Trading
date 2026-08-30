"""미국주식 스윙 자동매매 상태 저장소 — kr_state.py와 동일 패턴, 통화만 USD."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path(os.environ.get("US_SWING_STATE_PATH", "/app/data/us_swing_state.json"))

_DEFAULT_STATE = {
    "stop_price": {},        # {symbol: stop_price(USD)}
    "entry_cost": {},        # {symbol: 매수원가(USD)}
    "realized_pnl_usd": 0.0,  # 누적 실현손익 — 예산이 이 값만큼 늘거나 줄어든다
    "pending_entries": [],
    "pending_exits": [],
    "last_scan_date": None,  # 미국 동부시간(America/New_York) 기준 "YYYY-MM-DD"
    "trade_log": [],
    "equity_history": [],    # [{ts, total_pnl_usd}] — 대시보드 기간별(일/주/월/전체) 손익 차트용
    "position_history": {},  # {symbol: [{ts, price, unrealized_pnl_usd}]} — 종목별 차트용
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
