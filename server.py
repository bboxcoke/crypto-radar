#!/usr/bin/env python3
"""
Crypto Radar Web Server
提供 Dashboard 页面和 API 接口
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).parent

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
            # 读取最近一次的扫描结果
            signals = []
            heat_list = []
            total_coins = 0
            today_signals = 0
            
            alert_file = SCRIPT_DIR / 'alert_history.json'
            if alert_file.exists():
                history = json.loads(alert_file.read_text())
                today_signals = len(history)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'running',
                'total_coins': total_coins,
                'today_signals': today_signals,
                'signals': signals,
                'heat_list': heat_list,
                'last_scan': None
            }).encode())
            return
        
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b'Not Found')
    
    def log_message(self, format, *args):
        # 安静模式
        pass

def main():
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"🚀 Dashboard: http://0.0.0.0:{port}")
    server.serve_forever()

if __name__ == '__main__':
    main()
