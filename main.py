"""
Crypto Radar v1.0

币安合约雷达 — OI + 费率转负扫描 + 热度做多雷达

部署到 Render:
1. 推送到 GitHub
2. Render 新建 Worker
3. 设置环境变量:
   - TG_BOT_TOKEN: 你的 Telegram Bot Token
   - TG_CHAT_ID: 接收推送的 Chat ID
4. 自动会跑
"""

from crypto_radar import run_once

if __name__ == '__main__':
    # 单次运行(给 cronjob 用)
    run_once()
