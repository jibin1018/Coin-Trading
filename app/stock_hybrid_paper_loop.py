"""주식 전용 하이브리드 모의투자 봇 (AI 뉴스 + 차트 돌파)

대상 종목: 미국 테크 대장주 (TSLA, NVDA, AAPL, MSFT)
데이터 소스: yfinance (API 키 불필요, 무료)
LLM: 로컬 Ollama (gemma4:e4b)

코인 봇과 완전히 동일한 전략을 '미국 주식'에 적용하여
같은 전략이 코인에서 잘 먹히는지, 주식에서 잘 먹히는지 비교합니다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    print("⚠️ yfinance 패키지가 필요합니다. 'pip install yfinance' 를 실행해주세요.")
    raise

from app.momentum_state import append_equity_point, now_iso

STOCK_UNIVERSE = ["TSLA", "NVDA", "AAPL", "MSFT"]
START_CAPITAL_USD = float(os.environ.get("STOCK_HYBRID_START_CAPITAL", "700"))  # ≈100만원
CHECK_INTERVAL_SECONDS = 60 * 60 * 4  # 4시간 간격 (주식은 변동성이 코인보다 낮으므로)
POSITION_SIZE_PCT = 0.2

# Ollama LLM 세팅
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "ollama")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gemma4:e4b")

def _fetch_stock_news(ticker: str) -> str:
    """야후 파이낸스에서 해당 주식의 최신 뉴스 제목들을 가져옵니다."""
    try:
        stock = yf.Ticker(ticker)
        news_items = stock.news[:5]
        # 최신 yfinance는 title이 최상위가 아니라 content.title 아래로 들어간다 —
        # 예전 구조(최상위 title)도 혹시 몰라 같이 받아준다.
        titles = [n["content"]["title"] if "content" in n else n["title"]
                  for n in news_items if "content" in n or "title" in n]
        return " | ".join(titles)
    except Exception as e:
        print(f"[{ticker}] 뉴스 수집 실패: {e}")
        return ""

def _analyze_news_with_llm(ticker: str, news: str) -> str:
    if not news:
        return "hold"
        
    prompt = f"""
    You are an expert Wall Street stock analyst.
    Analyze the following recent news headlines for {ticker}:
    {news}
    
    Based ONLY on these headlines, decide the short-term sentiment for {ticker}.
    Output exactly one word: 'buy', 'sell', or 'hold'.
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
        return response.json()["choices"][0]["message"]["content"].strip().lower()
    except Exception as exc:
        print(f"[{ticker}] LLM 분석 실패: {exc}")
        return "hold"

def _get_current_price(ticker: str) -> float:
    # yfinance 1.7+는 download()가 기본적으로 MultiIndex 컬럼을 반환해서
    # data['Close'].iloc[-1]이 float으로 안 바뀌고 매번 조용히 실패했다 — Ticker.history()로 회피.
    try:
        data = yf.Ticker(ticker).history(period="1d", interval="1m")
        if not data.empty:
            return float(data['Close'].iloc[-1])
    except Exception as e:
        print(f"[{ticker}] 현재가 조회 실패: {e}")
    return 0.0

def run_cycle() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "stock_hybrid_state.json")
    if os.path.exists(state_file):
        with open(state_file, "r") as f: state = json.load(f)
    else:
        state = {"equity_usd": START_CAPITAL_USD, "positions": {}, "cumulative_realized_pnl_usd": 0.0}

    print(f"\n[{now_iso()}] 🏢 미국 주식 하이브리드 AI 봇 분석 시작...")

    current_prices = {}
    for ticker in STOCK_UNIVERSE:
        price = _get_current_price(ticker)
        if price > 0:
            current_prices[ticker] = price

    for ticker in STOCK_UNIVERSE:
        if ticker not in current_prices: continue
        current_price = current_prices[ticker]
        
        # 1. 뉴스 분석
        news = _fetch_stock_news(ticker)
        ai_signal = _analyze_news_with_llm(ticker, news)
        print(f"  - [{ticker}] 현재가: ${current_price:.2f} | AI 시그널: {ai_signal.upper()}")
        
        pos = state["positions"].get(ticker)
        
        # 2. 청산 로직 (보유 중인데 AI가 악재라고 판단하면 즉시 매도)
        if pos and pos["side"] == "long" and ai_signal == "sell":
            pnl = pos["notional_usd"] * (current_price / pos["entry_price"] - 1)
            print(f"    🔴 AI 악재 감지! {ticker} 전량 매도 (손익: {pnl:+.2f} USD)")
            state["cumulative_realized_pnl_usd"] += pnl
            state["equity_usd"] += pos["notional_usd"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
            del state["positions"][ticker]

        # 3. 진입 로직 (주식은 현물이므로 Long만 수행)
        elif not pos and ai_signal == "buy":
            invest_amount = START_CAPITAL_USD * POSITION_SIZE_PCT
            if state["equity_usd"] >= invest_amount:
                print(f"    🟢 AI 호재 감지! {ticker} 매수 진입 @ ${current_price:.2f}")
                state["positions"][ticker] = {"side": "long", "entry_price": current_price, "notional_usd": invest_amount}
                state["equity_usd"] -= invest_amount  # 진입 원금만큼 가용잔고에서 차감

    # 미실현 손익 업데이트
    unrealized_pnl = 0
    for ticker, pos in state["positions"].items():
        if ticker in current_prices:
            pnl = pos["notional_usd"] * (current_prices[ticker] / pos["entry_price"] - 1)
            pos["unrealized_pnl_usd"] = pnl
            unrealized_pnl += pnl

    total_equity = state["equity_usd"] + unrealized_pnl
    print(f"--- 🇺🇸 주식 하이브리드 봇 평가자산: ${total_equity:,.2f} (보유: {len(state['positions'])}) ---")
    append_equity_point(state, total_equity)

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def main() -> None:
    print("주식 하이브리드 AI 모의투자 봇 가동! (대상: TSLA, NVDA, AAPL, MSFT)")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
