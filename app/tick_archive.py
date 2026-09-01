"""틱 원본을 월단위 JSONL로 영구 보존 — tick_state.py/crypto_tick_state.py의 롤링버퍼(최근
N개만 유지, 라이브 매매판단용)와 별개로, 나중에 NAS로 옮겨 단타전략 백테스트에 쓸 원본
데이터를 계속 append한다. 한 달 지나면 파일명이 자연히 넘어가므로 개별 파일 크기는
스스로 제한된다. 압축은 안 한다 — plain JSONL이라야 NAS로 옮긴 뒤에도 아무 도구로나 바로
읽을 수 있다(gzip은 나중에 NAS 쪽에서 필요하면 그때 해도 됨).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

ARCHIVE_DIR = Path(os.environ.get("TICK_ARCHIVE_DIR", "/app/archive"))


def append_batch(category: str, records: Iterable[dict]) -> None:
    """records 각 항목은 최소 "ts"(ISO8601 문자열) 키를 가져야 한다. 같은 flush 안에서도
    월이 걸치는 드문 경우까지 대비해 ts 기준으로 파일을 나눠 쓴다."""
    by_month: dict[str, list[dict]] = {}
    for record in records:
        month = record["ts"][:7]  # "YYYY-MM"
        by_month.setdefault(month, []).append(record)

    if not by_month:
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for month, month_records in by_month.items():
        path = ARCHIVE_DIR / f"{category}_{month}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for record in month_records:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
