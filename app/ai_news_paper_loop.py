"""Vibe-Trading 스타일 AI 뉴스 분석 전략 (페이퍼 트레이딩 전용)

AI(LLM)가 실시간 뉴스를 읽고 시장의 센티먼트(호재/악재)를 분석하여
매수/매도 시그널을 생성하는 모의투자 봇입니다.

로직:
1. 최신 암호화폐 뉴스 RSS 피드에서 뉴스 제목들을 가져옵니다.
2. OpenAI API를 호출하여 뉴스 제목들을 분석하고 각 코인별 센티먼트 점수(Bullish/Bearish)를 추출합니다.
3. 호재(Bullish) 시그널이 나오면 매수(Long), 악재(Bearish) 시그널이 나오면 매도(Short/청산) 합니다.

준비물:
.env 파일에 OPENAI_API_KEY 셋팅 필요
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import ccxt
import requests

from app.momentum_state import append_equity_point, log_event, now_iso

# 환경변수 및 설정
UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
START_CAPITAL_USDT = float(os.environ.get("AI_NEWS_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 60  # 1시간마다 체크 (뉴스는 너무 자주 체크하면 API 비용 발생)
POSITION_SIZE_PCT = 0.2

# LLM API 키 및 Ollama 설정
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "ollama")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gemma4:e4b")

def _fetch_crypto_news() -> list[str]:
    """공용 RSS 피드에서 최신 암호화폐 뉴스를 긁어옵니다."""
    news_titles = []
    try:
        # 코인텔레그래프 RSS 피드 예시 (API 키 불필요)
        url = "https://cointelegraph.com/rss"
        response = requests.get(url, timeout=10)
        root = ET.fromstring(response.content)
        
        # 최신 뉴스 10개만 추출
        for item in root.findall('./channel/item')[:10]:
            title = item.find('title')
            if title is not None and title.text:
                news_titles.append(title.text)
    except Exception as exc:
        print(f"뉴스 수집 에러: {exc}")
    
    return news_titles

def _analyze_news_with_ai(news_titles: list[str]) -> dict[str, str]:
    """
    OpenAI API를 사용하여 뉴스를 분석하고 코인별 시그널을 리턴합니다.
    API 키가 없으면 시뮬레이션(Dummy) 결과를 반환합니다.
    """
    if not news_titles:
        return {}

    if not OPENAI_API_KEY:
        print("[경고] OPENAI_API_KEY가 없습니다! 가짜(Dummy) AI 분석 결과를 반환합니다.")
        # 가짜 로직: 'Bitcoin' 단어가 제목에 있으면 BTC buy, 'Ethereum' 있으면 ETH buy
        signals = {}
        text = " ".join(news_titles).lower()
        if "bitcoin" in text: signals["BTC"] = "buy"
        if "ethereum" in text: signals["ETH"] = "buy"
        if "crash" in text or "hack" in text:
            for coin in UNIVERSE: signals[coin] = "sell"
        return signals

    # 실제 OpenAI API 호출 로직 (API 키가 있을 경우)
    print("🧠 AI가 최신 뉴스를 분석 중입니다...")
    prompt = f"""
    아래는 최신 암호화폐 뉴스 헤드라인들입니다.
    {news_titles}
    
    이 뉴스들을 분석하여 다음 코인들 중 명확한 호재(buy) 또는 악재(sell)가 있는 코인을 식별하세요.
    대상 코인: {UNIVERSE}
    
    응답은 오직 유효한 JSON 형식으로만 제출하세요. 설명은 생략하세요.
    예시: {{"BTC": "buy", "ETH": "sell", "SOL": "hold"}}
    """
    
    try:
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            },
            timeout=60  # 로컬 Ollama를 여러 봇이 동시에 쓰면 대기열이 생겨 15초로는 자주 타임아웃남
        )
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        # 코드 블록 마크다운 제거
        content = content.replace("```json", "").replace("```", "").strip()
        signals = json.loads(content)
        return signals
    except Exception as exc:
        print(f"AI 분석 실패: {exc}")
        return {}

def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"

def _fetch_current_prices(exchange: ccxt.binance) -> dict[str, float]:
    prices = {}
    want = {_perp_symbol(b): b for b in UNIVERSE}
    try:
        tickers = exchange.fetch_tickers(list(want))
        for symbol, base in want.items():
            last = (tickers.get(symbol) or {}).get("last")
            if last:
                prices[base] = float(last)
    except Exception:
        pass
    return prices

def run_cycle() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ai_news_paper_state.json")
    
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            state = json.load(f)
    else:
        state = {"equity_usdt": START_CAPITAL_USDT, "positions": {}, "cumulative_realized_pnl_usdt": 0.0}

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    prices = _fetch_current_prices(exchange)
    
    if not prices:
        return

    print(f"\n[{now_iso()}] AI 뉴스 분석 사이클 시작")
    news_titles = _fetch_crypto_news()
    if news_titles:
        print(f"📰 수집된 최신 뉴스 {len(news_titles)}건:")
        for i, t in enumerate(news_titles[:3]):
            print(f"  - {t}")
        if len(news_titles) > 3: print("  - ...")

    signals = _analyze_news_with_ai(news_titles)
    if signals:
        print(f"🤖 AI 분석 시그널: {signals}")

    # 1. 청산 (Sell) 로직
    for base, pos in list(state["positions"].items()):
        current_price = prices.get(base)
        if not current_price: continue
            
        pnl = pos["notional_usdt"] * (current_price / pos["entry_price"] - 1)
        
        # AI가 악재(sell)라고 판단했거나, -10% 이상 손실 시 청산
        if signals.get(base) == "sell" or pnl < -pos["notional_usdt"] * 0.10:
            reason = "AI 악재 분석" if signals.get(base) == "sell" else "손절라인 이탈"
            print(f"🔴 청산 ({reason}): {base} @ {current_price:.4f} (손익: {pnl:+.2f} USDT)")
            state["cumulative_realized_pnl_usdt"] += pnl
            state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
            del state["positions"][base]

    # 2. 진입 (Buy) 로직
    for base, signal in signals.items():
        if signal == "buy" and base not in state["positions"]:
            current_price = prices.get(base)
            if not current_price: continue
                
            invest_amount = START_CAPITAL_USDT * POSITION_SIZE_PCT
            if state["equity_usdt"] >= invest_amount:
                print(f"🟢 진입 (AI 호재 분석): {base} @ {current_price:.4f} (투자금: {invest_amount:.2f} USDT)")
                state["positions"][base] = {
                    "side": "long",
                    "entry_price": current_price,
                    "notional_usdt": invest_amount,
                    "ts": now_iso()
                }
                state["equity_usdt"] -= invest_amount  # 진입 원금만큼 가용잔고에서 차감

    # 3. 평가금 업데이트
    unrealized_pnl = 0
    for base, pos in state["positions"].items():
        if prices.get(base):
            pos["unrealized_pnl_usdt"] = pos["notional_usdt"] * (prices[base] / pos["entry_price"] - 1)
            unrealized_pnl += pos["unrealized_pnl_usdt"]
            
    current_total_equity = state["equity_usdt"] + unrealized_pnl
    print(f"--- AI 봇 현재 평가자산: {current_total_equity:,.2f} USDT (보유: {len(state['positions'])}종목) ---")
    append_equity_point(state, current_total_equity)

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def main() -> None:
    print(f"Vibe-Trading 스타일 AI 뉴스 페이퍼 트레이딩 시작 (자본금: {START_CAPITAL_USDT} USDT)")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
