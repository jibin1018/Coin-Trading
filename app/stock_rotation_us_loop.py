"""app/stock_rotation_loop.py의 미장(US) 전용 진입점. app/stock_rotation_kr_loop.py 참고."""
from __future__ import annotations

import os

os.environ.setdefault("ROTATION_MARKET", "US")
os.environ.setdefault(
    "ROTATION_STATE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "stock_rotation_us_state.json"),
)

from app.stock_rotation_loop import main  # noqa: E402

if __name__ == "__main__":
    main()
