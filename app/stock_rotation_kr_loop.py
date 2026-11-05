"""app/stock_rotation_loop.py의 국장(KR) 전용 진입점.

ROTATION_MARKET이 모듈 임포트 시점에 읽히는 상수라, 대시보드에서 같은 스크립트를
KR/US 두 설정으로 동시에 띄우려면(프로세스 추적/로그파일이 스크립트 이름 기준이라
스크립트 자체를 나눠야 한다 — kr_swing_loop.py/us_swing_loop.py와 같은 기존 관례) 이렇게
market 고정 진입점을 따로 둔다.
"""
from __future__ import annotations

import os

os.environ.setdefault("ROTATION_MARKET", "KR")
os.environ.setdefault(
    "ROTATION_STATE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "stock_rotation_kr_state.json"),
)

from app.stock_rotation_loop import main  # noqa: E402

if __name__ == "__main__":
    main()
