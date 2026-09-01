"""바이낸스 공개 시세 웹소켓 실시간 체결가/펀딩비 수신 — 3개 코인봇(펀딩비차익, TRX 스윙,
모멘텀 로테이션)이 쓰는 종목 전부를 한 프로세스에서 커버한다.

바이낸스 공개 마켓데이터 스트림은 API 키/인증 자체가 필요없다(KIS 웹소켓과 가장 큰 차이).
스팟(stream.binance.com)과 무기한선물(fstream.binance.com)은 호스트가 달라 커넥션을 둘로
나눈다 — 각각 독립된 재접속 루프를 돌려 한쪽이 끊겨도 다른쪽엔 영향 없게 한다.

  - 스팟 체결(@trade): TRX(스윙 실계좌 손절 반응용) + 펀딩비차익 현물레그(ETH/XRP/DOGE)
  - 선물 체결(@trade): 모멘텀 로테이션 47종목 + 펀딩비차익 선물레그
  - 선물 마크가격/펀딩비(@markPrice@1s): 펀딩비차익 3종목 — 실시간 예상 펀딩요율

지금은 수신·저장까지만 한다. 각 봇의 실제 매매 판단(진입/청산 로직)에 이 틱을 연결하는건
전략 검증 후 진행할 별도 작업 — TRX는 실계좌라 특히 그렇다.
"""
from __future__ import annotations

import asyncio
import json

import websockets

from app.crypto_tick_state import load_state, log_event, now_iso, save_state
from app.tick_archive import append_batch
from app.momentum_rotation_loop import UNIVERSE as MOMENTUM_UNIVERSE
from app.paper_funding_arb import SYMBOLS as FUNDING_ARB_BASES

SPOT_WS_URL = "wss://stream.binance.com:9443/stream"
FUTURES_WS_URL = "wss://fstream.binance.com/stream"

SPOT_BASES = sorted(set(FUNDING_ARB_BASES) | {"TRX"})
FUTURES_TRADE_BASES = sorted(set(FUNDING_ARB_BASES) | set(MOMENTUM_UNIVERSE))
FUNDING_BASES = sorted(set(FUNDING_ARB_BASES))

MAX_TICKS_PER_SYMBOL = 300
FLUSH_INTERVAL_SECONDS = 15
RECONNECT_BACKOFF_SECONDS = 5


def _stream_name(base: str, suffix: str) -> str:
    # 콤바인드 스트림(?streams=) 엔드포인트는 스트림명이 전부 소문자여야 함(단일 스트림
    # 엔드포인트와 달리 대소문자 섞이면 그 스트림만 조용히 무시됨 — 실측 확인된 버그).
    return f"{base.lower()}usdt{suffix}".lower()


def _combined_url(base_url: str, streams: list[str]) -> str:
    return f"{base_url}?streams={'/'.join(streams)}"


def _base_from_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


async def _run_spot(buffer: dict[str, list[dict]]) -> None:
    streams = [_stream_name(b, "@trade") for b in SPOT_BASES]
    url = _combined_url(SPOT_WS_URL, streams)
    while True:
        try:
            state = load_state()
            log_event(state, f"[스팟접속] {len(streams)}종목 구독: {SPOT_BASES}")
            save_state(state)
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    _handle_spot_message(raw, buffer)
        except Exception as exc:  # noqa: BLE001
            state = load_state()
            log_event(state, f"[스팟오류] 재접속: {exc}")
            save_state(state)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)


async def _run_futures(buffer: dict[str, list[dict]], funding_buffer: dict[str, list[dict]]) -> None:
    streams = [_stream_name(b, "@trade") for b in FUTURES_TRADE_BASES]
    streams += [_stream_name(b, "@markPrice@1s") for b in FUNDING_BASES]
    url = _combined_url(FUTURES_WS_URL, streams)
    while True:
        try:
            state = load_state()
            log_event(state, f"[선물접속] 체결 {len(FUTURES_TRADE_BASES)}종목 + 마크가격/펀딩비 {len(FUNDING_BASES)}종목 구독")
            save_state(state)
            async with websockets.connect(url) as ws:
                async for raw in ws:
                    _handle_futures_message(raw, buffer, funding_buffer)
        except Exception as exc:  # noqa: BLE001
            state = load_state()
            log_event(state, f"[선물오류] 재접속: {exc}")
            save_state(state)
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)


def _handle_spot_message(raw: str, buffer: dict[str, list[dict]]) -> None:
    msg = json.loads(raw)
    data = msg.get("data") or {}
    if data.get("e") != "trade":
        return
    base = _base_from_symbol(data["s"])
    try:
        price = float(data["p"])
    except (KeyError, ValueError):
        return
    buffer.setdefault(base, []).append({"ts": now_iso(), "price": price})


def _handle_futures_message(raw: str, buffer: dict[str, list[dict]], funding_buffer: dict[str, list[dict]]) -> None:
    msg = json.loads(raw)
    data = msg.get("data") or {}
    event = data.get("e")
    if event == "trade":
        base = _base_from_symbol(data["s"])
        try:
            price = float(data["p"])
        except (KeyError, ValueError):
            return
        buffer.setdefault(base, []).append({"ts": now_iso(), "price": price})
    elif event == "markPriceUpdate":
        base = _base_from_symbol(data["s"])
        try:
            mark_price = float(data["p"])
            funding_rate = float(data["r"])
        except (KeyError, ValueError):
            return
        funding_buffer.setdefault(base, []).append({"ts": now_iso(), "mark_price": mark_price, "funding_rate": funding_rate})


def _archive_records(base: str, points: list[dict]) -> list[dict]:
    return [{"symbol": base, **point} for point in points]


def _flush(spot_buffer: dict[str, list[dict]], perp_buffer: dict[str, list[dict]], funding_buffer: dict[str, list[dict]]) -> None:
    if not spot_buffer and not perp_buffer and not funding_buffer:
        return
    state = load_state()
    for key, buf, archive_category in (
        ("spot_ticks", spot_buffer, "crypto_spot"),
        ("perp_ticks", perp_buffer, "crypto_perp"),
        ("funding", funding_buffer, "crypto_funding"),
    ):
        store = state.setdefault(key, {})
        for base, points in buf.items():
            append_batch(archive_category, _archive_records(base, points))
            history = store.setdefault(base, [])
            history.extend(points)
            store[base] = history[-MAX_TICKS_PER_SYMBOL:]
    save_state(state)
    spot_buffer.clear()
    perp_buffer.clear()
    funding_buffer.clear()


async def _flush_loop(spot_buffer: dict[str, list[dict]], perp_buffer: dict[str, list[dict]], funding_buffer: dict[str, list[dict]]) -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        _flush(spot_buffer, perp_buffer, funding_buffer)


async def _main_async() -> None:
    spot_buffer: dict[str, list[dict]] = {}
    perp_buffer: dict[str, list[dict]] = {}
    funding_buffer: dict[str, list[dict]] = {}
    await asyncio.gather(
        _run_spot(spot_buffer),
        _run_futures(perp_buffer, funding_buffer),
        _flush_loop(spot_buffer, perp_buffer, funding_buffer),
    )


def main() -> None:
    state = load_state()
    log_event(state, "코인 틱 스트림 시작")
    save_state(state)
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
