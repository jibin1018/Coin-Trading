"""TRX 스윙 실계좌 봇 상시 실행 루프.

손절선이 -12%로 느슨해서(급락장이 아닌 이상) 국장/미장처럼 분단위로 급하게 체크할 필요는
없다 — 기본 5분 주기. 한 사이클에서 예외가 나도 루프 전체가 죽지 않도록 사이클 단위로
예외를 흡수한다.
"""
from __future__ import annotations

import os
import time
import traceback

from app.trx_swing_trade import run_cycle

CHECK_INTERVAL_SECONDS = int(os.environ.get("TRX_SWING_CHECK_INTERVAL_SECONDS", "300"))


def main() -> None:
    print(f"TRX 스윙 실계좌 봇 루프 시작 (사이클 주기 {CHECK_INTERVAL_SECONDS}초)", flush=True)
    while True:
        try:
            run_cycle()
        except Exception:
            print("사이클 실행 중 오류 발생:", flush=True)
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
