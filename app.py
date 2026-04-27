#!/usr/bin/env python3
"""
Crypto Radar 总入口
同时启动 Web Dashboard + 后台扫描
"""

import json
import os
import sys
import time
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# 导入扫描模块
from crypto_radar import (
    scan_funding_reversal, scan_heat_radar,
    format_funding_alert, format_heat_alert,
    send_tg, load_history, save_history, mark_alerted, is_duplicate
)

# 共享数据
latest_data = {
    'signals': [],
    'heat_list': [],
    'total_coins': 0,
    'today_signals': 0,
    'last_scan': None,
}

def run_scanner():
    """后台线程: 定期运行扫描"""
    global latest_data
    print("[Scanner] 启动扫描线程")
    
    # 先等一会儿让 Web 先启动
    time.sleep(3)
    
    while True:
        try:
            ts = time.strftime('%m-%d %H:%M:%S')
            print(f"\n[Scanner] --- {ts} ---")
            
            history = load_history()
            latest_data['today_signals'] = len(history)
            
            # 1. 费率转负扫描
            signals = scan_funding_reversal()
            if signals:
                strong = [s for s in signals if s['current_fr'] < 0 and s.get('oi_rising')]
                if strong:
                    msg = format_funding_alert(strong)
                    if msg:
                        send_tg(msg)
                        print(f"[Scanner] 推送 {len(strong)} 个信号")
                        
                        # 记录去重
                        for s in strong:
                            if not is_duplicate(s['symbol'], history):
                                history = mark_alerted(s['symbol'], history)
                        save_history(history)
                        latest_data['today_signals'] = len(history)
                
                latest_data['signals'] = [{
                    'symbol': s['symbol'],
                    'coin': s['symbol'].replace('USDT', ''),
                    'price': s['price'],
                    'price_chg_24h': s['price_chg_24h'],
                    'volume': s['volume'],
                    'oi_change': s['oi_change'],
                    'current_fr': s['current_fr'],
                    'prev_fr': s['prev_fr'],
                } for s in signals]
            else:
                latest_data['signals'] = []
            
            # 2. 热度雷达 (每15分钟)
            current_min = datetime.now().minute
            if current_min % 15 < 5:
                hot_list = scan_heat_radar()
                if hot_list:
                    msg = format_heat_alert(hot_list[:8])
                    if msg:
                        send_tg(msg)
                        print(f"[Scanner] 推送热度 TOP8")
                    latest_data['heat_list'] = [{
                        'symbol': s['symbol'],
                        'coin': s['coin'],
                        'price': s['price'],
                        'price_chg_24h': s['price_chg_24h'],
                        'volume': s['volume'],
                        'oi_change_1h': s['oi_change_1h'],
                        'funding_rate': s['funding_rate'],
                        'heat_score': s['heat_score'],
                    } for s in hot_list[:15]]
            
            # 更新币种数
            try:
                import requests
                info = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=5).json()
                latest_data['total_coins'] = len([s for s in info['symbols'] 
                    if s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'])
            except:
                pass
            
            latest_data['last_scan'] = time.strftime('%H:%M:%S')
            
        except Exception as e:
            print(f"[Scanner] 错误: {e}")
        
        # 等待5分钟
        for _ in range(300):
            time.sleep(1)

# ============ Web Server ============
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        
        if path == '/':
            self.send_response(302)
            self.send_header('Location', '/dashboard.html')
            self.end_headers()
            return
        
        elif path == '/dashboard.html':
            html_path = SCRIPT_DIR / 'dashboard.html'
            if html_path.exists():
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html_path.read_bytes())
                return
        
        elif path == '/api/status':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'running',
                **latest_data
            }).encode())
            return
        
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not Found')
    
    def log_message(self, format, *args):
        pass

def main():
    port = int(os.environ.get('PORT', 10000))
    
    # 启动后台扫描线程
    scanner = threading.Thread(target=run_scanner, daemon=True)
    scanner.start()
    
    # 启动 Web
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"🚀 Crypto Radar 启动!")
    print(f"📊 Dashboard: http://0.0.0.0:{port}")
    print(f"📡 扫描间隔: 5分钟")
    print(f"🤖 TG推送: {'已启用' if os.environ.get('TG_BOT_TOKEN') else '未配置'}")
    server.serve_forever()

if __name__ == '__main__':
    from datetime import datetime
    main()
