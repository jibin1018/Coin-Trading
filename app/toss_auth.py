"""토스증권 Open API 인증 스캐폴드 — 아직 뼈대만 있음.

토스증권 Open API는 2026년에 출시됐고, OAuth 2.0 client-credentials 방식으로 토큰을 받는다는
것까지는 공개된 정보로 확인했지만, 실제 엔드포인트 경로/요청 스키마는 아직 확보하지 못했다.
사용자가 토스 Open API 이용 승인을 받고 실제 개발자 문서를 공유해주면, 아래 TODO 부분을
실제 스펙으로 채워 완성한다 — 추측으로 엔드포인트를 만들어서 돈이 걸린 주문 코드를 짜는 건
더 위험하다고 판단해 일단 비워둔다.

app/kis_auth.py의 토큰 캐싱/쓰로틀링 패턴을 그대로 따른다.
"""
from __future__ import annotations

import os
import time

import requests

# TODO: 실제 토스증권 Open API 베이스 URL로 교체 (사용자 승인 후 문서 확인 필요)
TOSS_BASE_URL = "https://open-api.tossinvest.com"

_MIN_REQUEST_INTERVAL_SECONDS = 0.5
_last_request_monotonic = 0.0

_cached_token: str | None = None
_cached_token_expiry: float = 0.0


def app_credentials() -> tuple[str, str]:
    return os.environ["TOSS_APP_KEY"], os.environ["TOSS_APP_SECRET"]


def throttled_request(method: str, url: str, max_retries: int = 3, timeout: int = 15, **kwargs) -> requests.Response:
    """KIS 쪽과 동일한 패턴 — 호출 전 최소 간격을 강제한다. 토스 쪽 구체적인 레이트리밋
    응답 코드는 아직 확인 못해서, 일반적인 429 상태코드 기준으로만 재시도한다(TODO: 실제
    에러 코드 확인되면 app/kis_auth.py의 _RATE_LIMIT_MSG_CODES처럼 구체화)."""
    global _last_request_monotonic
    response: requests.Response | None = None
    for attempt in range(max_retries):
        wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
        response = requests.request(method, url, timeout=timeout, **kwargs)
        _last_request_monotonic = time.monotonic()
        if response.status_code == 429 and attempt < max_retries - 1:
            time.sleep(2 * (attempt + 1))
            continue
        return response
    return response  # type: ignore[return-value]


def issue_token() -> str:
    """OAuth2 client-credentials 토큰 발급. 만료 전까지 캐시해서 재사용한다.

    TODO: 아래는 OAuth2 client-credentials 표준 형태로 짠 추정 요청이다 — 토스 Open API
    실제 문서(토큰 엔드포인트 경로, 파라미터명, 응답 필드명)를 확인하면 맞게 고쳐야 한다.
    """
    global _cached_token, _cached_token_expiry
    if _cached_token and time.monotonic() < _cached_token_expiry:
        return _cached_token

    app_key, app_secret = app_credentials()
    raise NotImplementedError(
        "토스증권 Open API 실제 토큰 발급 엔드포인트가 아직 확인되지 않았습니다. "
        "토스 Open API 승인을 받고 개발자 문서를 공유해주시면 완성합니다."
    )
