"""바이낸스 실계좌 스팟/선물 클라이언트 팩토리.

스팟과 USDⓈ-M 선물 모두 하나의 API 키 쌍(BINANCE_API_KEY/BINANCE_SECRET_KEY)을 공유한다 —
테스트넷과 달리 실계좌는 스팟/선물 거래 권한을 같은 키에 함께 부여하기 때문이다.
"""
from __future__ import annotations

import os

import ccxt


def spot_client() -> ccxt.binance:
    return ccxt.binance({
        "apiKey": os.environ["BINANCE_API_KEY"],
        "secret": os.environ["BINANCE_SECRET_KEY"],
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })


def futures_client() -> ccxt.binance:
    return ccxt.binance({
        "apiKey": os.environ["BINANCE_API_KEY"],
        "secret": os.environ["BINANCE_SECRET_KEY"],
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
