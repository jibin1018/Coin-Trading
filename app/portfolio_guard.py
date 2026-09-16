"""고점 대비 손실이 임계값을 넘으면 매매를 멈추는 공용 킬스위치.

momentum_rotation_loop.py 에서 검증된 패턴(고점 추적 + 드로다운 임계값 → 전량청산·정지,
수동 리셋 전까지 재진입 금지)을 다른 실거래 봇에서도 재사용할 수 있도록 뽑아낸 것.
"equity 를 어떻게 계산할지"는 봇마다 다르므로(지갑 공유 여부, 레버리지 유무 등) 호출부 책임이고,
이 모듈은 "고점 추적 → 드로다운 계산 → 임계값 판정"과 "최근 손절 반복 감지"만 담당한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def check_drawdown_kill(state: dict, equity: float, kill_dd: float, hwm_key: str = "hwm_usdt") -> dict:
    """고점(hwm) 대비 equity 하락률을 추적하고 킬스위치 여부를 판정한다.

    state[hwm_key] 와 state["drawdown"] 를 in-place 로 갱신한다. halted 플래그와 리셋 로직은
    봇마다 원하는 동작이 다를 수 있어(예: 리셋 시 hwm 을 현재 equity 로 재설정할지 등)
    호출부가 직접 관리한다.

    반환: {"hwm": float, "dd": float, "kill": bool}
    """
    hwm = max(float(state.get(hwm_key) or equity), equity)
    state[hwm_key] = hwm
    dd = 1.0 - equity / hwm if hwm > 0 else 0.0
    state["drawdown"] = dd
    return {"hwm": hwm, "dd": dd, "kill": dd >= kill_dd}


def record_stop_loss(state: dict, log_key: str = "stop_loss_events") -> None:
    """손절이 발생할 때마다 호출 — check_stop_loss_cooldown 판단용 이력을 남긴다."""
    events = state.setdefault(log_key, [])
    events.append(datetime.now(timezone.utc).isoformat())
    state[log_key] = events[-100:]


def check_stop_loss_cooldown(
    state: dict, lookback_days: float, max_stops: int, log_key: str = "stop_loss_events"
) -> bool:
    """최근 lookback_days 일 내 손절이 max_stops 회 이상이면 True(재진입 금지) 반환.

    횡보장에서 손절 → 재진입 → 손절이 반복되는 휩쏘 구간을, 계좌 전체 드로다운이 커지기
    전에 먼저 잡아내기 위한 보조 장치(freqtrade의 StoplossGuard와 같은 개념)."""
    events = state.get(log_key, [])
    if not events:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    recent = [e for e in events if datetime.fromisoformat(e) >= cutoff]
    return len(recent) >= max_stops
