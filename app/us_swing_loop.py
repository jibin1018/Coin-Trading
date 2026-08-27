"""미국주식 스윙 자동매매 메인 루프 — kr_swing_loop.py와 동일 이중주기 구조, America/New_York
시간대 기준으로 스케줄링한다(서머타임 자동 반영).
  - 장중(09:30~16:00 ET, 평일) 매분: us_intraday_risk.check_once()
  - 장마감후(16:15 ET 이후, 하루 1회): us_daily_scan.run_once()
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

from app.kis_auth import issue_token
from app.us_daily_scan import run_once as run_daily_scan
from app.us_intraday_risk import check_once as run_intraday_risk
from app.us_state import load_state, log_event, save_state

ET = ZoneInfo("America/New_York")
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE_FOR_RISK = dt.time(15, 55)
SCAN_AFTER = dt.time(16, 15)
LOOP_SLEEP_SECONDS = 60
TOKEN_TTL_SECONDS = 12 * 3600


def main() -> None:
    state = load_state()
    log_event(state, "미국주식 스윙 자동매매 루프 시작")
    save_state(state)

    token = issue_token()
    token_issued_at = time.monotonic()

    while True:
        try:
            if time.monotonic() - token_issued_at > TOKEN_TTL_SECONDS:
                token = issue_token()
                token_issued_at = time.monotonic()

            now = dt.datetime.now(ET)
            is_weekday = now.weekday() < 5

            if is_weekday and MARKET_OPEN <= now.time() <= MARKET_CLOSE_FOR_RISK:
                run_intraday_risk(token, state)
            elif is_weekday and now.time() >= SCAN_AFTER:
                run_daily_scan(token)
        except Exception as exc:  # noqa: BLE001
            state = load_state()
            log_event(state, f"[오류] 루프 사이클 실패: {exc}")
            save_state(state)

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
