"""국내주식 스윙 자동매매 메인 루프 — 단일 프로세스 안에서 이중 주기로 동작한다.
  - 장중(09:00~15:20 KST, 평일) 매분: kr_intraday_risk.check_once() — 보유종목 손절 +
    전일 스캔이 채운 매매대기열 소비
  - 장마감후(15:35 KST 이후, 하루 1회): kr_daily_scan.run_once() — 워치리스트 재스캔,
    다음날 소비할 대기열 갱신

공휴일은 별도로 다루지 않는다(휴장일엔 KIS가 주문/시세 오류를 뱉을 뿐 루프가 죽지는 않음).
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

from app.kis_auth import issue_token
from app.kr_daily_scan import run_once as run_daily_scan
from app.kr_intraday_risk import check_once as run_intraday_risk
from app.kr_state import load_state, log_event, save_state

KST = ZoneInfo("Asia/Seoul")
MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE_FOR_RISK = dt.time(15, 20)
SCAN_AFTER = dt.time(15, 35)
LOOP_SLEEP_SECONDS = 60
TOKEN_TTL_SECONDS = 12 * 3600


def main() -> None:
    state = load_state()
    log_event(state, "국내주식 스윙 자동매매 루프 시작")
    save_state(state)

    token = issue_token()
    token_issued_at = time.monotonic()

    while True:
        try:
            if time.monotonic() - token_issued_at > TOKEN_TTL_SECONDS:
                token = issue_token()
                token_issued_at = time.monotonic()

            now = dt.datetime.now(KST)
            is_weekday = now.weekday() < 5

            if is_weekday and MARKET_OPEN <= now.time() <= MARKET_CLOSE_FOR_RISK:
                run_intraday_risk(token, state)
            elif is_weekday and now.time() >= SCAN_AFTER:
                run_daily_scan(token)
        except Exception as exc:  # noqa: BLE001 — 한 사이클 실패로 루프 전체가 죽지 않게
            state = load_state()
            log_event(state, f"[오류] 루프 사이클 실패: {exc}")
            save_state(state)

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
