#!/usr/bin/env python3
"""
Crypto Coke Bot — AI 对话 + 雷达信号 Bot
- Telegram Webhook (python-telegram-bot)
- AI 对话 (DeepSeek API)
- 后台扫描线程 (币安合约 OI + 费率)
"""

import json
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify

# ============ 导入扫描模块 ============
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from crypto_radar import (
    scan_funding_reversal, scan_heat_radar,
    format_funding_alert, format_heat_alert,
    send_tg, load_history, save_history, mark_alerted, is_duplicate
)

# ============ 配置 ============
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
AI_BASE_URL = os.environ.get('AI_BASE_URL', 'https://api.deepseek.com')
AI_MODEL = os.environ.get('AI_MODEL', 'deepseek-chat')
TG_CHAT_ID = os.environ.get('TG_CHAT_ID', '')

# ============ Flask App ============
app = Flask(__name__)

# ============ 共享数据 ============
latest_data = {
    'signals': [],
    'heat_list': [],
    'total_coins': 0,
    'today_signals': 0,
    'last_scan': None,
}

# ============ DeepSeek AI 对话 ============
def ai_chat(user_message: str) -> str:
    """调用 DeepSeek API 生成回复"""
    if not AI_API_KEY:
        return "🤖 AI 未配置，请联系管理员设置 AI_API_KEY"

    system_prompt = """你是 Crypto Coke Bot，一个专业的加密货币交易助手。
你可以回答关于加密货币、区块链、交易策略的问题。
当前你有以下能力：
1. 币安合约 OI + 资金费率扫描
2. 热度做多雷达
3. 加密货币知识问答

请用中文回复，语言风格亲切专业。
当用户问及市场行情或信号时，引导他们使用 /signals 或 /heat 命令。"""

    try:
        import requests as req
        resp = req.post(
            f"{AI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "stream": False,
                "max_tokens": 1024,
                "temperature": 0.7,
            },
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()['choices'][0]['message']['content']
        else:
            return f"🤖 AI 请求失败: HTTP {resp.status_code}"
    except Exception as e:
        return f"🤖 AI 对话出错: {str(e)}"


# ============ Telegram Bot (PTB v20 Webhook) ============
def setup_ptb():
    """设置 python-telegram-bot Application 用于处理 webhook"""
    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

    if not TG_BOT_TOKEN:
        print("[Bot] TG_BOT_TOKEN 未配置")
        return None

    application = Application.builder().token(TG_BOT_TOKEN).build()

    # /start 命令
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome = (
            "🚀 *Crypto Coke Bot 已上线！*\n\n"
            "我是你的加密货币交易助手，支持：\n\n"
            "🤖 *AI 对话* — 直接发消息跟我聊天\n"
            "📡 */signals* — 查看最新费率转负信号\n"
            "📊 */heat* — 查看热度做多雷达排行\n"
            "📈 */btc* — BTC 实时行情\n"
            "💡 */help* — 查看所有命令\n\n"
            "随便问我什么，开始吧！"
        )
        await update.message.reply_text(welcome, parse_mode='Markdown')

    # /help 命令
    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "📋 *可用命令：*\n\n"
            "/start — 启动机器人\n"
            "/signals — 查看最新费率转负信号\n"
            "/heat — 查看热度做多雷达\n"
            "/btc — BTC 实时行情\n"
            "/help — 显示此帮助\n\n"
            "或者直接跟我聊天，我会 AI 回复你！"
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')

    # /signals 命令
    async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📡 正在扫描费率转负信号，请稍候...")
        try:
            signals = scan_funding_reversal()
            if signals:
                strong = [s for s in signals if s['current_fr'] < 0 and s.get('oi_rising')]
                if strong:
                    msg = format_funding_alert(strong)
                    if msg:
                        await update.message.reply_text(msg, parse_mode='Markdown')
                        return
            await update.message.reply_text("✅ 当前未发现新的费率转负信号")
        except Exception as e:
            await update.message.reply_text(f"❌ 扫描出错: {str(e)}")

    # /heat 命令
    async def cmd_heat(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📊 正在扫描热度雷达，请稍候...")
        try:
            hot_list = scan_heat_radar()
            if hot_list:
                msg = format_heat_alert(hot_list[:8])
                if msg:
                    await update.message.reply_text(msg, parse_mode='Markdown')
                    return
            await update.message.reply_text("📊 暂无热度数据")
        except Exception as e:
            await update.message.reply_text(f"❌ 扫描出错: {str(e)}")

    # /btc 命令
    async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📈 正在获取 BTC 行情...")
        try:
            import requests as req
            ticker = req.get(
                'https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT',
                timeout=10
            ).json()
            price = float(ticker['lastPrice'])
            chg = float(ticker['priceChangePercent'])
            high = float(ticker['highPrice'])
            low = float(ticker['lowPrice'])
            vol = float(ticker['quoteVolume'])

            emoji = "🟢" if chg >= 0 else "🔴"
            msg = (
                f"📈 *BTC/USDT 永续合约*\n\n"
                f"价格: ${price:,.2f}\n"
                f"24h涨跌: {emoji} {chg:+.2f}%\n"
                f"24h最高: ${high:,.2f}\n"
                f"24h最低: ${low:,.2f}\n"
                f"成交额: ${vol/1e9:.2f}B"
            )
            await update.message.reply_text(msg, parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ 获取行情失败: {str(e)}")

    # AI 对话处理 — 普通消息
    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_text = update.message.text
        # 如果是命令，跳过
        if user_text.startswith('/'):
            return
        # 发送"正在输入"状态
        await update.message.chat.send_action(action="typing")
        # 调用 AI
        reply = ai_chat(user_text)
        await update.message.reply_text(reply, parse_mode='Markdown')

    # 注册 handler
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("signals", cmd_signals))
    application.add_handler(CommandHandler("heat", cmd_heat))
    application.add_handler(CommandHandler("btc", cmd_btc))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return application


# ============ Webhook 端点 ============
application = None

@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram bot webhook"""
    global application
    if not application:
        return jsonify({"ok": False, "error": "Bot not initialized"}), 500
    try:
        update_data = request.get_json(force=True)
        from telegram import Update
        update = Update.de_json(update_data, application.bot)
        import asyncio
        asyncio.run(application.process_update(update))
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[Webhook] 处理错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/status')
def api_status():
    """状态 API"""
    return jsonify({
        'status': 'running',
        'bot_name': 'Crypto Coke Bot',
        'ai_configured': bool(AI_API_KEY),
        **latest_data
    })


@app.route('/')
def index():
    """Dashboard 页面"""
    html_path = SCRIPT_DIR / 'dashboard.html'
    if html_path.exists():
        return html_path.read_text()
    return "<h1>Crypto Coke Bot is running!</h1><p>AI对话 + 雷达信号 Bot</p>"


# ============ 后台扫描线程 ============
def run_scanner():
    """后台线程: 定期运行扫描并推送到 TG"""
    global latest_data
    print("[Scanner] 启动扫描线程")
    time.sleep(5)  # 等 Web 先启动

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
                if strong and TG_BOT_TOKEN and TG_CHAT_ID:
                    msg = format_funding_alert(strong)
                    if msg:
                        send_tg(msg)
                        print(f"[Scanner] 推送 {len(strong)} 个信号")
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
                    if msg and TG_BOT_TOKEN and TG_CHAT_ID:
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
                info = requests.get(
                    'https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=5
                ).json()
                latest_data['total_coins'] = len([
                    s for s in info['symbols']
                    if s['contractType'] == 'PERPETUAL'
                    and s['quoteAsset'] == 'USDT'
                    and s['status'] == 'TRADING'
                ])
            except:
                pass

            latest_data['last_scan'] = time.strftime('%H:%M:%S')

        except Exception as e:
            print(f"[Scanner] 错误: {e}")

        # 等5分钟
        for _ in range(300):
            time.sleep(1)


# ============ 设置 Webhook URL ============
def set_webhook():
    """设置 Telegram webhook 指向本服务"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not render_url:
        print("[Webhook] RENDER_EXTERNAL_URL 未设置，跳过 webhook 设置")
        return False
    webhook_url = f"{render_url}/webhook"
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setWebhook",
            json={
                "url": webhook_url,
                "allowed_updates": ["message", "callback_query"]
            },
            timeout=10
        )
        result = resp.json()
        if result.get('ok'):
            print(f"[Webhook] ✅ 设置成功: {webhook_url}")
        else:
            print(f"[Webhook] ❌ 设置失败: {result}")
        return result.get('ok', False)
    except Exception as e:
        print(f"[Webhook] 设置出错: {e}")
        return False


# ============ 主入口 ============
if __name__ == '__main__':
    print("🚀 Crypto Coke Bot 启动中...")
    print(f"TG Bot: {'已配置' if TG_BOT_TOKEN else '未配置'}")
    print(f"AI: {'已配置' if AI_API_KEY else '未配置 (将使用模板回复)'}")
    print(f"{'='*50}")

    # 初始化 PTB Application
    if TG_BOT_TOKEN:
        application = setup_ptb()
        if application:
            print("[Bot] PTB Application 初始化完成")

    # 启动后台扫描线程
    scanner = threading.Thread(target=run_scanner, daemon=True)
    scanner.start()

    # 尝试设置 webhook
    set_webhook()

    # 启动 Flask
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Web 服务: http://0.0.0.0:{port}")
    print(f"🤖 Webhook: /webhook")
    print(f"📊 Dashboard: /")
    print(f"📡 API: /api/status")
    app.run(host='0.0.0.0', port=port)
