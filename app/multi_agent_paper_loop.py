"""다중 에이전트 토론 매매 봇 (TradingAgents 스타일)

3명의 서로 다른 역할을 가진 AI(낙관론자, 비관론자, 수석 트레이더)가
최신 뉴스를 보고 토론하여 만장일치(또는 다수결)로 방향을 결정할 때만
안전하게 진입하는 고승률 모의투자 봇입니다.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET

import ccxt
import requests

from app.momentum_state import now_iso

START_CAPITAL_USDT = float(os.environ.get("AGENTS_START_CAPITAL_USDT", "70"))  # ≈10만원
CHECK_INTERVAL_SECONDS = 60 * 60 * 4 # 4시간마다 회의
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "ollama")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gemma4:e4b")
TARGET_COIN = "BTC"

def _fetch_news() -> str:
    news_list = []
    try:
        url = "https://cointelegraph.com/rss"
        response = requests.get(url, timeout=10)
        root = ET.fromstring(response.content)
        for item in root.findall('./channel/item')[:5]:
            title = item.find('title')
            if title is not None and title.text:
                news_list.append(title.text)
    except:
        pass
    return " | ".join(news_list)

def _call_llm(role_prompt: str, news: str) -> str:
    if not OPENAI_API_KEY:
        return "hold" # 키가 없으면 무조건 관망
        
    try:
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": role_prompt},
                    {"role": "user", "content": f"Here is the latest news: {news}. Based on this, output ONLY ONE WORD: 'long', 'short', or 'hold'."}
                ],
                "temperature": 0.5
            },
            timeout=60  # 로컬 Ollama를 여러 봇이 동시에 쓰면 대기열이 생겨 15초로는 자주 타임아웃남
        )
        data = response.json()
        return data["choices"][0]["message"]["content"].strip().lower()
    except Exception as exc:
        print(f"LLM 에러: {exc}")
        return "hold"

def _debate_and_decide(news: str) -> str:
    print(f"\n[{now_iso()}] 🧑‍⚖️ AI 위원회 회의 소집 (안건: BTC 투자 방향)")
    
    optimist_prompt = "You are a highly optimistic crypto bull. You always look for reasons to buy (long). If news is completely devastating, output hold. Otherwise, output long."
    pessimist_prompt = "You are a highly pessimistic crypto bear. You always look for risks to short. If news is amazingly good, output hold. Otherwise, output short."
    trader_prompt = "You are a rational head trader. You weigh both sides. If news is solidly bullish, output long. If solidly bearish, output short. If mixed, output hold."
    
    op_decision = _call_llm(optimist_prompt, news)
    print(f"  - 낙관론자 AI 의견: {op_decision}")
    
    pe_decision = _call_llm(pessimist_prompt, news)
    print(f"  - 비관론자 AI 의견: {pe_decision}")
    
    tr_decision = _call_llm(trader_prompt, news)
    print(f"  - 수석 트레이더 AI 판단: {tr_decision}")
    
    # 만장일치 또는 수석의 판단 중시 로직
    if tr_decision == "long" and op_decision == "long":
        return "long"
    elif tr_decision == "short" and pe_decision == "short":
        return "short"
    
    return "hold"

def run_cycle() -> None:
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents_paper_state.json")
    if os.path.exists(state_file):
        with open(state_file, "r") as f: state = json.load(f)
    else:
        state = {"equity_usdt": START_CAPITAL_USDT, "positions": {}, "cumulative_realized_pnl_usdt": 0.0}

    news = _fetch_news()
    if not news: return

    decision = _debate_and_decide(news)
    print(f"📢 최종 위원회 결론: {TARGET_COIN} -> {decision.upper()}")

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    try:
        ticker = exchange.fetch_ticker(f"{TARGET_COIN}/USDT:USDT")
        current_price = float(ticker["last"])
    except:
        return

    # 기존 포지션 청산 로직 (결론이 달라지거나 hold가 나오면 청산)
    pos = state["positions"].get(TARGET_COIN)
    if pos:
        if pos["side"] != decision:
            direction = 1 if pos["side"] == "long" else -1
            pnl = pos["notional_usdt"] * direction * (current_price / pos["entry_price"] - 1)
            print(f"🔴 위원회 판단 변경으로 청산: {TARGET_COIN} (손익: {pnl:+.2f})")
            state["cumulative_realized_pnl_usdt"] += pnl
            state["equity_usdt"] += pos["notional_usdt"] + pnl  # 진입 때 뺐던 원금 반환 + 손익
            del state["positions"][TARGET_COIN]

    # 신규 진입 로직 — 예산 체크가 아예 없었음(항상 진입) — 추가
    if decision in ["long", "short"] and TARGET_COIN not in state["positions"]:
        invest_amount = START_CAPITAL_USDT * 0.3
        if state["equity_usdt"] >= invest_amount:
            print(f"🟢 위원회 결정으로 진입: {TARGET_COIN} {decision} @ {current_price:.2f}")
            state["positions"][TARGET_COIN] = {"side": decision, "entry_price": current_price, "notional_usdt": invest_amount}
            state["equity_usdt"] -= invest_amount

    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

def main() -> None:
    print("다중 에이전트 토론 매매 봇 가동! (4시간마다 회의 소집)")
    while True:
        run_cycle()
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
