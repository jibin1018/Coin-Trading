"""프리미엄 퀀트 대시보드 (Web UI) - 탭 분리 & 자동 실행 컨트롤러 포함

Tailwind CSS와 Chart.js를 사용하며, 
코인/미국주식/한국주식을 탭(Tab)으로 나누어 보여주고
대시보드 내에서 직접 봇을 백그라운드로 실행할 수 있는 기능(Process Manager)을 포함합니다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# 탭(Tab)별 봇 설정 및 실행 대상 스크립트
BOTS_CONFIG = {
    "Crypto": {
        "title": "🪙 Crypto (코인)",
        "bots": {
            "기본 모멘텀 로테이션": {"file": "momentum_state.json", "script": "momentum_rotation_loop"},
            "Freqtrade 차트 봇": {"file": "freqtrade_paper_state.json", "script": "freqtrade_paper_loop"},
            "AI 뉴스 트레이딩 봇": {"file": "ai_news_paper_state.json", "script": "ai_news_paper_loop"},
            "하이브리드 (차트+AI) 봇": {"file": "hybrid_paper_state.json", "script": "hybrid_paper_loop"},
            "페어 트레이딩 (통계적 차익)": {"file": "pairs_paper_state.json", "script": "pairs_paper_loop"},
            "Dual Thrust (추세 돌파)": {"file": "dual_thrust_state.json", "script": "dual_thrust_paper_loop"},
            "다중 에이전트 토론 봇": {"file": "agents_paper_state.json", "script": "multi_agent_paper_loop"},
            "머신러닝(ML) 예측 봇": {"file": "ml_paper_state.json", "script": "ml_paper_loop"},
        }
    },
    "US_Stock": {
        "title": "🇺🇸 US Stock (미국 주식)",
        "bots": {
            "미국 주식 AI 하이브리드": {"file": "stock_hybrid_state.json", "script": "stock_hybrid_paper_loop"},
            # 향후 추가될 봇들을 위해 예약됨
        }
    },
    "KR_Stock": {
        "title": "🇰🇷 KR Stock (한국 주식)",
        "bots": {
            "한국 주식 AI 하이브리드": {"file": "kr_hybrid_state.json", "script": "kr_hybrid_paper_loop"},
            "Dual Thrust (yfinance 페이퍼, KIS 키 불필요)": {"file": "kr_dual_thrust_paper_state.json", "script": "kr_dual_thrust_paper_loop"},
            "Dual Thrust (KIS 모의투자 실연동)": {"file": "kr_dual_thrust_state.json", "script": "kr_dual_thrust_kis_loop"},
        }
    }
}

ROOT_DIR = os.path.dirname(os.path.dirname(__file__))

# 각 상태파일의 시작 자본(봇 코드 안의 START_CAPITAL 기본값과 반드시 맞춰줘야 수익률 %가 정확함)
INITIAL_CAPITAL_BY_FILE = {
    "momentum_state.json": 10000.0,  # 기존부터 운영해온 값, 안 건드림
    "kr_hybrid_state.json": 1000000.0,  # 100만원
    "stock_hybrid_state.json": 700.0,  # ≈100만원
    "kr_dual_thrust_state.json": 1000000.0,  # 100만원 (KIS 실연동, app/kr_dual_thrust_kis_loop.py 예산과 맞춤)
    "kr_dual_thrust_paper_state.json": 1000000.0,  # 100만원 (yfinance 페이퍼 버전, 위와 동일 예산)
}
DEFAULT_INITIAL_CAPITAL = 70.0  # ≈10만원, 나머지 신규 페이퍼봇 공통

# 현재 실행 중인 서브프로세스를 추적하기 위한 딕셔너리
running_processes = {}

def get_bot_status(filename: str, script_name: str) -> dict:
    filepath = os.path.join(ROOT_DIR, filename)
    is_running = script_name in running_processes and running_processes[script_name].poll() is None
    
    if not os.path.exists(filepath):
        return {"status": "online" if is_running else "offline", "equity": 0, "pnl": 0, "positions": {}}
    
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
        
        initial_capital = INITIAL_CAPITAL_BY_FILE.get(filename, DEFAULT_INITIAL_CAPITAL)


        equity = data.get("equity_usdt", data.get("equity_usd", data.get("equity_krw", initial_capital)))
        # 포지션에 들어간 원금(notional)도 총자산에 포함해야 한다 — equity는 진입 시 이미
        # 원금만큼 차감된 "가용 현금"이라, 포지션 가치(원금+미실현손익)를 안 더하면 그만큼
        # 화면에서 증발한 것처럼 보인다.
        position_value = sum(
            p.get("notional_usdt", p.get("notional_usd", p.get("notional_krw", 0)))
            + p.get("unrealized_pnl_usdt", p.get("unrealized_pnl_usd", p.get("unrealized_pnl_krw", 0)))
            for p in data.get("positions", {}).values()
        )
        total_equity = equity + position_value
        pnl_pct = ((total_equity / initial_capital) - 1) * 100
        
        return {
            "status": "online" if is_running else "offline (데이터만 존재)",
            "equity": total_equity,
            "pnl": pnl_pct,
            "positions": data.get("positions", {})
        }
    except Exception as e:
        return {"status": "error", "equity": 0, "pnl": 0, "positions": {}}

class DashboardHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        parsed = urllib.parse.parse_qs(post_data)
        
        if self.path == '/start_category':
            category_id = parsed.get('category', [''])[0]
            if category_id in BOTS_CONFIG:
                for bot_name, bot_info in BOTS_CONFIG[category_id]["bots"].items():
                    script_name = bot_info["script"]
                    # 이미 실행 중인지 확인
                    if script_name not in running_processes or running_processes[script_name].poll() is not None:
                        # 백그라운드 프로세스로 봇 실행 (표준 입출력은 무시)
                        # sys.executable: 대시보드를 띄운 것과 같은 파이썬(venv 등)을 그대로 재사용
                        # -u: stdout이 파일로 리다이렉트되면 기본이 블록버퍼링이라 print가 즉시 안 보임 — 강제로 언버퍼링
                        log_file = open(os.path.join(ROOT_DIR, f"{script_name}.log"), "a")
                        p = subprocess.Popen(
                            [sys.executable, "-u", "-m", f"app.{script_name}"],
                            cwd=ROOT_DIR,
                            stdout=log_file,
                            stderr=log_file
                        )
                        running_processes[script_name] = p
                        print(f"[Dashboard] Started {script_name} (PID: {p.pid})")
                
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            return

        if self.path == '/stop_category':
            category_id = parsed.get('category', [''])[0]
            stopped = []
            if category_id in BOTS_CONFIG:
                for bot_name, bot_info in BOTS_CONFIG[category_id]["bots"].items():
                    script_name = bot_info["script"]
                    p = running_processes.get(script_name)
                    if p is not None and p.poll() is None:
                        p.terminate()
                        stopped.append(script_name)
                        print(f"[Dashboard] Stopped {script_name} (PID: {p.pid})")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(f"OK ({len(stopped)}개 정지)".encode('utf-8'))
            return

    def do_GET(self):
        if self.path == '/favicon.ico':
            self.send_response(200)
            self.end_headers()
            return
            
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        
        results = {cat_id: {} for cat_id in BOTS_CONFIG.keys()}
        for cat_id, cat_info in BOTS_CONFIG.items():
            for bot_name, bot_info in cat_info["bots"].items():
                results[cat_id][bot_name] = get_bot_status(bot_info["file"], bot_info["script"])
            
        html = self.generate_html(results)
        self.wfile.write(html.encode('utf-8'))

    def generate_html(self, results: dict) -> str:
        tabs_nav_html = ""
        tabs_content_html = ""
        scripts_html = ""
        
        for idx, (cat_id, cat_info) in enumerate(BOTS_CONFIG.items()):
            title = cat_info["title"]
            is_active = "true" if idx == 0 else "false"
            active_class = "border-emerald-500 text-emerald-400" if idx == 0 else "border-transparent text-gray-400 hover:text-gray-300 hover:border-gray-300"
            hidden_class = "" if idx == 0 else "hidden"
            
            # 탭 네비게이션
            tabs_nav_html += f"""
                <button class="tab-btn w-1/3 py-4 text-center border-b-2 font-medium text-sm sm:text-base {active_class} transition-colors" data-target="tab-{cat_id}">
                    {title}
                </button>
            """
            
            # 카테고리별 데이터 준비
            labels = []
            pnl_data = []
            bg_colors = []
            cards_html = ""
            
            for name, data in results[cat_id].items():
                labels.append(name)
                pnl = data["pnl"]
                pnl_data.append(round(pnl, 2))
                bg_colors.append("'rgba(16, 185, 129, 0.8)'" if pnl >= 0 else "'rgba(239, 68, 68, 0.8)'")
                
                if "online" in data["status"]:
                    status_badge = '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900 text-green-300"><span class="w-2 h-2 mr-1.5 bg-green-400 rounded-full animate-pulse"></span>Running</span>'
                    border_cls = "border-blue-500/30"
                    pnl_cls = "text-emerald-400" if pnl >= 0 else "text-rose-400"
                    pnl_sign = "+" if pnl > 0 else ""
                else:
                    status_badge = '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-800 text-gray-400">Offline</span>'
                    border_cls = "border-gray-700"
                    pnl_cls = "text-gray-500"
                    pnl_sign = ""
                    
                pos_html = ""
                if data["positions"]:
                    for coin, p in data["positions"].items():
                        side = "UP" if p.get("side") == "long" else "DOWN"
                        side_color = "text-emerald-400" if side == "UP" else "text-rose-400"
                        upnl = p.get('unrealized_pnl_usdt', p.get('unrealized_pnl_usd', p.get('unrealized_pnl_krw', 0)))
                        upnl_color = "text-emerald-400" if upnl >= 0 else "text-rose-400"
                        symbol = "₩" if "KR" in cat_id else "$"
                        # f-string 포맷 스펙 안에는 if/else 조건식을 못 넣는다 — 미리 문자열로 만들어둔다
                        upnl_fmt = f"{upnl:+,.0f}" if "KR" in cat_id else f"{upnl:+.2f}"
                        pos_html += f"""
                        <div class="flex justify-between items-center py-2 border-b border-gray-700/50 last:border-0">
                            <div>
                                <span class="font-bold text-gray-200">{coin}</span>
                                <span class="text-xs ml-2 font-mono {side_color} bg-gray-800 px-1 rounded">{side}</span>
                            </div>
                            <div class="font-mono text-sm {upnl_color}">{upnl_fmt} {symbol}</div>
                        </div>
                        """
                else:
                    pos_html = '<div class="text-gray-500 text-sm py-2 italic text-center">관망 중 (포지션 없음)</div>'
                
                currency_symbol = "₩" if "KR" in cat_id else "$"
                equity_fmt = f"{data['equity']:,.0f}" if "KR" in cat_id else f"{data['equity']:,.2f}"
                
                cards_html += f"""
                <div class="bg-gray-800/40 backdrop-blur-md rounded-xl border {border_cls} p-6 shadow-2xl transition-all hover:bg-gray-800/60">
                    <div class="flex justify-between items-start mb-4">
                        <h3 class="text-lg font-bold text-gray-100">{name}</h3>
                        {status_badge}
                    </div>
                    <div class="mb-6">
                        <div class="text-sm text-gray-400 mb-1">Total Equity</div>
                        <div class="text-2xl font-mono font-bold text-white">{currency_symbol}{equity_fmt}</div>
                        <div class="text-sm font-mono mt-1 {pnl_cls}">{pnl_sign}{pnl:.2f}%</div>
                    </div>
                    <div class="bg-gray-900/50 rounded-lg p-4 border border-gray-700/50">
                        <div class="text-xs uppercase text-gray-500 font-bold mb-2 tracking-wider">Active Positions</div>
                        {pos_html}
                    </div>
                </div>
                """
                
            labels_str = "[" + ", ".join(f"'{l}'" for l in labels) + "]"
            data_str = "[" + ", ".join(str(d) for d in pnl_data) + "]"
            colors_str = "[" + ", ".join(bg_colors) + "]"
            chart_id = f"pnlChart_{cat_id}"
            
            # 각 탭의 콘텐츠 영역
            tabs_content_html += f"""
            <div id="tab-{cat_id}" class="tab-content {hidden_class} animate-fade-in">
                <div class="flex justify-between items-center mb-6">
                    <h2 class="text-2xl font-extrabold text-white">{title} 전략 보드</h2>
                    <div class="flex gap-2">
                        <button onclick="startCategory('{cat_id}')" class="bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-lg shadow-lg font-bold flex items-center transition-transform active:scale-95">
                            <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                            해당 리그 봇 전체 가동
                        </button>
                        <button onclick="stopCategory('{cat_id}')" class="bg-rose-600 hover:bg-rose-500 text-white px-4 py-2 rounded-lg shadow-lg font-bold flex items-center transition-transform active:scale-95">
                            <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 10h6v4H9z"></path></svg>
                            전체 정지
                        </button>
                    </div>
                </div>
                
                <div class="bg-gray-800/40 backdrop-blur-md rounded-xl border border-gray-700 p-6 mb-8 shadow-2xl">
                    <h3 class="text-lg font-bold text-gray-200 mb-4 text-center">전략별 수익률 비교 (ROI %)</h3>
                    <div class="h-64 w-full">
                        <canvas id="{chart_id}"></canvas>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
                    {cards_html}
                </div>
            </div>
            """
            
            scripts_html += f"""
                new Chart(document.getElementById('{chart_id}').getContext('2d'), {{
                    type: 'bar',
                    data: {{
                        labels: {labels_str},
                        datasets: [{{
                            label: '수익률 (%)',
                            data: {data_str},
                            backgroundColor: {colors_str},
                            borderRadius: 6,
                            borderWidth: 0,
                            barThickness: 'flex',
                            maxBarThickness: 40
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        animation: false,
                        plugins: {{ legend: {{ display: false }} }},
                        scales: {{
                            y: {{ grid: {{ color: 'rgba(51, 65, 85, 0.5)', drawBorder: false }}, ticks: {{ color: '#94a3b8' }} }},
                            x: {{ grid: {{ display: false }}, ticks: {{ color: '#94a3b8' }} }}
                        }}
                    }}
                }});
            """

        return f"""
        <!DOCTYPE html>
        <html lang="ko" class="dark">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Quant Trading Terminal</title>
            <meta http-equiv="refresh" content="10">
            <script src="https://cdn.tailwindcss.com"></script>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                body {{ background-color: #0f172a; background-image: radial-gradient(circle at top right, #1e293b, #0f172a); min-height: 100vh; }}
                .animate-fade-in {{ animation: fadeIn 0.3s ease-in-out; }}
                @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}
            </style>
        </head>
        <body class="text-gray-200 font-sans p-4 md:p-8">
            <div class="max-w-7xl mx-auto">
                <header class="mb-8 text-center md:text-left flex flex-col md:flex-row justify-between items-center">
                    <div>
                        <h1 class="text-3xl md:text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-emerald-400 tracking-tight">
                            Quant Trading Terminal
                        </h1>
                        <p class="text-gray-400 mt-2 text-sm">서버 내장형 자동 실행 & 실시간 모니터링 시스템</p>
                    </div>
                </header>

                <!-- 탭 네비게이션 -->
                <div class="border-b border-gray-700 mb-8">
                    <nav class="flex -mb-px" aria-label="Tabs">
                        {tabs_nav_html}
                    </nav>
                </div>

                <!-- 탭 컨텐츠 -->
                <div id="tabs-container">
                    {tabs_content_html}
                </div>
            </div>

            <script>
                // URL 해시값에 따라 탭 상태 복구 (새로고침 시 유지)
                document.addEventListener('DOMContentLoaded', () => {{
                    let activeTab = window.location.hash.substring(1) || 'tab-Crypto';
                    switchTab(activeTab);
                }});

                // 탭 전환 로직
                const tabBtns = document.querySelectorAll('.tab-btn');
                const tabContents = document.querySelectorAll('.tab-content');

                tabBtns.forEach(btn => {{
                    btn.addEventListener('click', () => {{
                        const targetId = btn.getAttribute('data-target');
                        window.location.hash = targetId; // URL 업데이트 (새로고침 방어)
                        switchTab(targetId);
                    }});
                }});

                function switchTab(targetId) {{
                    tabContents.forEach(content => content.classList.add('hidden'));
                    tabBtns.forEach(b => {{
                        b.classList.remove('border-emerald-500', 'text-emerald-400');
                        b.classList.add('border-transparent', 'text-gray-400');
                    }});

                    const activeContent = document.getElementById(targetId);
                    if(activeContent) activeContent.classList.remove('hidden');
                    
                    const activeBtn = document.querySelector(`[data-target="${{targetId}}"]`);
                    if(activeBtn) {{
                        activeBtn.classList.remove('border-transparent', 'text-gray-400');
                        activeBtn.classList.add('border-emerald-500', 'text-emerald-400');
                    }}
                }}

                // Start 버튼 로직
                function startCategory(catId) {{
                    fetch('/start_category', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: 'category=' + encodeURIComponent(catId)
                    }}).then(res => {{
                        if(res.ok) {{
                            alert(catId + " 리그의 모든 봇 가동 명령을 전송했습니다!\\n백그라운드에서 순차적으로 실행됩니다.");
                            // 새로고침을 잠깐 미뤄서 서버가 켜질 시간을 줌
                            setTimeout(() => window.location.reload(), 1500);
                        }} else {{
                            alert("실행 실패!");
                        }}
                    }});
                }}

                // Stop 버튼 로직
                function stopCategory(catId) {{
                    fetch('/stop_category', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                        body: 'category=' + encodeURIComponent(catId)
                    }}).then(res => {{
                        if(res.ok) {{
                            alert(catId + " 리그의 모든 봇 정지 명령을 전송했습니다.");
                            setTimeout(() => window.location.reload(), 1000);
                        }} else {{
                            alert("정지 실패!");
                        }}
                    }});
                }}

                {scripts_html}
            </script>
        </body>
        </html>
        """

def main():
    port = 8501
    server_address = ('', port)
    httpd = HTTPServer(server_address, DashboardHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()

if __name__ == '__main__':
    main()
