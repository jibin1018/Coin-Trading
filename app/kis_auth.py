"""한국투자증권 Open API 모의투자 인증 — 토큰 발급/캐시.

모의투자 전용 도메인만 쓴다 (실전 도메인은 모의투자 앱키를 거부한다 — 실측 확인됨).
토큰 발급은 앱키당 분당 1회로 제한되므로(EGW00133) 프로세스 내에서 한 번만 발급해 재사용한다.
"""
from __future__ import annotations

import os
import time

import requests

VTS_BASE_URL = "https://openapivts.koreainvestment.com:29443"


def app_credentials() -> tuple[str, str]:
    return os.environ["HANTOO_TEST_KEY"], os.environ["HANTOO_TEST_SECRET"]


def issue_token(max_retries: int = 3) -> str:
    app_key, app_secret = app_credentials()
    last_error: Exception | None = None
    for attempt in range(max_retries):
        resp = requests.post(
            f"{VTS_BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()["access_token"]
        last_error = RuntimeError(f"token issue failed: {resp.status_code} {resp.text}")
        if "EGW00133" in resp.text:  # 분당 1회 제한 — 대기 후 재시도
            time.sleep(65)
            continue
        time.sleep(3)
    raise last_error  # type: ignore[misc]
