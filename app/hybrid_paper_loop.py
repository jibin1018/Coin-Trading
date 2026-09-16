"""차트(Freqtrade) + AI 뉴스(Vibe-Trading) 하이브리드 모의투자 봇

기술적 분석(차트)과 기본적 분석(뉴스)을 결합하여 승률을 극대화하는 하이브리드 전략입니다.

핵심 융합 로직:
1. 타이밍(5분 주기): 볼린저 밴드 하단 돌파 + RSI 과매도 구간에서 매수 '타이밍'을 잡습니다.
2. 필터링(1시간 주기): AI가 최신 뉴스를 읽고 현재 코인 시장의 '센티먼트(분위기)'를 판단합니다.
3. 융합 결정:
   - 매수 진입: 차트 매수 시그널이 떴을 때, AI가 악재(Sell)라고 판단한 상태면 진입을 포기(필터링)합니다. 호재/중립일 때만 안전하게 들어갑니다.
   - 포지션 청산: 차트상 매도 시그널(볼린저 중앙선, RSI 과매수)이 뜨거나, AI가 갑자기 악재(Sell) 뉴스를 감지하면 즉시 청산(빤스런)합니다.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import pandas_ta as ta
import requests

from app.futures_data import fetch_perp_ohlcv
from app.momentum_state import append_equity_point, now_iso

# 환경변수 및 설정
UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
START_CAPITAL_USDT = float(os.environ.get("HYBRID_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 5  # 5분마다 차트 체크
AI_CHECK_INTERVAL_SECONDS = 60 * 60  # 1시간마다 뉴스 체크
POSITION_SIZE_PCT = 0.2

# 기술적 지표 설정
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30
RSI_SELL_THRESHOLD = 70
BB_LENGTH = 20
BB_STD = 2.0

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "ollama")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gemma4:e4b")

def _fetch_crypto_news() -> list[str]:
    news_titles = []
    try:
        url = "https://cointelegraph.com/rss"
        response = requests.get(url, timeout=10)
        root = ET.fromstring(response.content)
        for item in root.findall('./channel/item')[:10]:
            title = item.find('title')
            if title is not None and title.text:
                news_titles.append(title.text)
    except Exception as exc:
        print(f"[News] 뉴스 수집 에러: {exc}")
    return news_titles

def _analyze_news_with_ai(news_titles: list[str]) -> dict[str, str]:
    if not news_titles:
        return {}

    if not OPENAI_API_KEY:
        # 키가 없으면 기본적으로 중립(hold)으로 간주하되 더미 로직 유지
        signals = {coin: "hold" for coin in UNIVERSE}
        text = " ".join(news_titles).lower()
        if "crash" in text or "hack" in text or "ban" in text:
            for coin in UNIVERSE: signals[coin] = "sell"
        return signals

    prompt = f"""
    아래는 최신 암호화폐 뉴스 헤드라인입니다.
    {news_titles}
    
    이 뉴스들을 바탕으로 다음 코인들의 단기 센티먼트를 분석하세요.
    명확한 호재면 "buy", 악재면 "sell", 뉴스가 없거나 애매하면 "hold"로 답하세요.
    대상 코인: {UNIVERSE}
    
    오직 유효한 JSON 형식으로만 제출하세요.
    예시: {{"BTC": "buy", "ETH": "hold", "SOL": "sell", "XRP": "hold", "DOGE": "hold"}}
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
        content = content.replace("```json", "").replace("```", "").strip()
        return json.loads(content)
    except Exception as exc:
        print(f"[AI] 분석 실패: {exc}")
        return {}

def _perp_symbol(base: str) -> str:
    return f"{base}/USDT:USDT"

def _get_technical_signals() -> dict[str, str]:
    signals = {}
    since_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    
    for base in UNIVERSE:
        try:
            df = fetch_perp_ohlcv(_perp_symbol(base), "5m", since_iso, None)
            if len(df) < BB_LENGTH + 1:
                continue
            
            df["rsi"] = ta.rsi(df["Close"], length=RSI_PERIOD)
            bbands = ta.bbands(df["Close"], length=BB_LENGTH, std=BB_STD)
            df = pd.concat([df, bbands], axis=1)
            
            last, curr = df.iloc[-2], df.iloc[-1]
            # pandas-ta 버전에 따라 컬럼명이 BBL_20_2.0 이거나 BBL_20_2.0_2.0 이거나 달라서 접두어로 찾는다
            lower_band = last[next(c for c in bbands.columns if c.startswith("BBL_"))]
            mid_band = last[next(c for c in bbands.columns if c.startswith("BBM_"))]
            
            # 차트 기반 진입 시그널
            if curr["Close"] < lower_band and curr["rsi"] < RSI_BUY_THRESHOLD:
                signals[base] = "buy"
            # 차트 기반 청산 시그널
            elif curr["Close"] > mid_band or curr["rsi"] > RSI_SELL_THRESHOLD:
                signals[base] = "sell"
                
        except Exception:
            pass
    return signals

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

def main() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "hybrid_paper_state.json")
    
    if os.path.exists(state_file):
        with open(state_file, "r") as f:
            state = json.load(f)
    else:
        state = {
            "equity_usdt": START_CAPITAL_USDT, 
            "positions": {}, 
            "cumulative_realized_pnl_usdt": 0.0,
            "ai_sentiments": {},
            "last_ai_check_ts": 0
        }

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    print(f"🌟 하이브리드 모의투자 봇 가동! (차트 5분, AI 1시간 체크 / 자본금: {START_CAPITAL_USDT} USDT)")

    while True:
        current_time = time.time()
        prices = _fetch_current_prices(exchange)
        if not prices:
            time.sleep(10)
            continue

        # 1. AI 뉴스 분석 (1시간 주기)
        if current_time - state.get("last_ai_check_ts", 0) > AI_CHECK_INTERVAL_SECONDS:
            print(f"\n[{now_iso()}] 📰 AI 뉴스 센티먼트 분석 중...")
            news = _fetch_crypto_news()
            if news:
                sentiments = _analyze_news_with_ai(news)
                if sentiments:
                    state["ai_sentiments"] = sentiments
                    state["last_ai_check_ts"] = current_time
                    print(f"🤖 AI 현재 시장 분위기 판단: {sentiments}")
        
        ai_sentiments = state.get("ai_sentiments", {})

        # 2. 기술적 지표 시그널 추출 (5분 주기)
        tech_signals = _get_technical_signals()

        # 3. 융합 청산 (Sell) 로직
        for base, pos in list(state["positions"].items()):
            current_price = prices.get(base)
            if not current_price: continue
            
            pnl = pos["notional_usdt"] * (current_price / pos["entry_price"] - 1)
            ai_sentiment = ai_sentiments.get(base, "hold")
            tech_signal = tech_signals.get(base, "hold")
            
            # 차트상 과매수이거나, AI가 악재로 판단했거나, -5% 손절라인 이탈 시
            if tech_signal == "sell" or ai_sentiment == "sell" or pnl < -pos["notional_usdt"] * 0.05:
                if ai_sentiment == "sell": reason = "AI 악재 감지 (패닉셀)"
                elif tech_signal == "sell": reason = "차트 고점 도달 (수익실현)"
                else: reason = "손절라인 이탈"
                
                print(f"[{now_iso()}] 🔴 융합 청산 ({reason}): {base} @ {current_price:.4f} (손익: {pnl:+.2f} USDT)")
                state["cumulative_realized_pnl_usdt"] += pnl
                state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
                del state["positions"][base]

        # 4. 융합 진입 (Buy) 로직
        for base, tech_signal in tech_signals.items():
            ai_sentiment = ai_sentiments.get(base, "hold")
            
            # 차트상 완벽한 매수 타점(buy)이 왔을 때
            if tech_signal == "buy" and base not in state["positions"]:
                # ⛔ AI가 '악재(sell)'라고 판단한 코인이면 차트가 좋아도 진입하지 않음 (함정 회피)
                if ai_sentiment == "sell":
                    # print(f"[{now_iso()}] ⚠️ {base} 차트 매수 시그널 발생했으나, AI 악재 판단으로 진입 포기!")
                    continue
                
                current_price = prices.get(base)
                if not current_price: continue
                
                invest_amount = START_CAPITAL_USDT * POSITION_SIZE_PCT
                if state["equity_usdt"] >= invest_amount:
                    print(f"[{now_iso()}] 🟢 융합 진입 (차트매수 + AI통과): {base} @ {current_price:.4f}")
                    state["positions"][base] = {
                        "side": "long",
                        "entry_price": current_price,
                        "notional_usdt": invest_amount,
                        "ts": now_iso()
                    }
                    state["equity_usdt"] -= invest_amount  # 진입 원금만큼 가용잔고에서 차감

        # 상태 저장 및 출력
        unrealized_pnl = sum(
            pos["notional_usdt"] * (prices[b] / pos["entry_price"] - 1)
            for b, pos in state["positions"].items() if prices.get(b)
        )
        total_equity = state["equity_usdt"] + unrealized_pnl
        print(f"--- 🌟 하이브리드 봇 평가자산: {total_equity:,.2f} USDT (보유: {len(state['positions'])}종목) ---")
        append_equity_point(state, total_equity)

        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
