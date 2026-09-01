"""틱 원본을 일단위 JSONL로 영구 보존 — tick_state.py/crypto_tick_state.py의 롤링버퍼(최근
N개만 유지, 라이브 매매판단용)와 별개로, 나중에 NAS로 옮겨 단타전략 백테스트에 쓸 원본
데이터를 계속 append한다. 월단위 파일 하나로 몰면 파일 하나가 깨졌을 때 그 달 전체를
잃으므로, 연/월/일 디렉토리로 나눠 파일 하나의 손상 범위를 하루치로 제한한다. 압축은
안 한다 — plain JSONL이라야 NAS로 옮긴 뒤에도 아무 도구로나 바로 읽을 수 있다(gzip은
나중에 NAS 쪽에서 필요하면 그때 해도 됨).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

ARCHIVE_DIR = Path(os.environ.get("TICK_ARCHIVE_DIR", "/app/archive"))


def _day_path(category: str, day: str) -> Path:
    year, month, date = day.split("-")
    return ARCHIVE_DIR / year / month / date / f"{category}.jsonl"


def append_batch(category: str, records: Iterable[dict]) -> None:
    """records 각 항목은 최소 "ts"(ISO8601 문자열) 키를 가져야 한다. 같은 flush 안에서도
    자정을 걸치는 드문 경우까지 대비해 ts 기준으로 날짜별 파일을 나눠 쓴다.

    ARCHIVE_DIR가 NAS(SMB) 마운트 위일 수 있다 — 마운트가 끊기면 이 호출이 예외를 던지는데,
    이걸 그대로 전파하면 tick_stream.py/crypto_tick_stream.py의 flush 루프 전체가 죽어서
    라이브 매매판단용 상태파일(tick_state.json 등) 갱신까지 같이 멈춘다. 아카이브는 어디까지나
    부가기능이므로 여기서 삼키고 경고만 남긴다 — 다음 flush 때 재시도된다."""
    by_day: dict[str, list[dict]] = {}
    for record in records:
        day = record["ts"][:10]  # "YYYY-MM-DD"
        by_day.setdefault(day, []).append(record)

    for day, day_records in by_day.items():
        path = _day_path(category, day)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for record in day_records:
                    f.write(json.dumps(record, ensure_ascii=False))
                    f.write("\n")
        except OSError as exc:
            print(f"[틱아카이브 쓰기실패] {path}: {exc} — 이번 배치 유실, 다음 flush 계속", flush=True)
