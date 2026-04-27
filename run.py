#!/usr/bin/env python3
"""
Crypto Radar 循环运行守护脚本
每分钟运行一次扫描
"""

import subprocess
import time
import sys
from pathlib import Path

if __name__ == '__main__':
    script = Path(__file__).parent / 'crypto_radar.py'
    while True:
        try:
            subprocess.run([sys.executable, str(script)], check=True)
        except KeyboardInterrupt:
            print("\n🛑 停止")
            break
        except:
            print("[!] 出错, 30秒后重试...")
            time.sleep(30)
            continue
        print("等待5分钟...")
        time.sleep(300)
