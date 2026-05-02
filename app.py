#!/usr/bin/env python3
"""
Crypto Coke Bot — AI 对话 + 雷达信号 Bot
- Telegram Webhook (纯 requests, 不用 PTB)
- AI 对话 (DeepSeek API)
- 后台扫描线程 (币安合约 OI + 费率)
"""

import json
import os
import sys
import time
import threading
import re
from pathlib import Path
from datetime import datetime

import requests
from flask import Flask, request, jsonify

# ============ 导入扫描模块 ============
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from crypto_radar import (
    scan_funding_reversal, scan_heat_radar,
    scan_extreme_funding, scan_arbitrage_signals,
    format_funding_alert, format_heat_alert,
    format_extreme_funding_alert, format_arbitrage_alert,
    send_tg, load_history, save_history, mark_alerted, is_duplicate,
    auto_trade, AUTO_TRADE_ENABLED, BINANCE_API_KEY, recover_trade_state,
    FR_ALERT_THRESHOLD, FR_DEDUP_HOURS,
    simulate_auto_trade, SIMULATION_MODE
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
    'scanner_alive': False,
    'scanner_errors': [],
    'startup_time': time.strftime('%m-%d %H:%M:%S'),
}

# ============ Telegram API (纯 requests) ============
TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

def tg_send(chat_id, text, parse_mode='Markdown'):
    """发送消息到 Telegram"""
    if not TG_BOT_TOKEN:
        return
    try:
        # Markdown 太长或格式不对时回退到纯文本
        resp = requests.post(
            f"{TG_API}/sendMessage",
            json={
                'chat_id': chat_id,
                'text': text,
                'parse_mode': parse_mode
            },
            timeout=10
        )
        if resp.status_code != 200:
            # 回退到纯文本
            requests.post(
                f"{TG_API}/sendMessage",
                json={'chat_id': chat_id, 'text': text},
                timeout=10
            )
    except Exception as e:
        print(f"[TG] send error: {e}")

def tg_send_action(chat_id, action='typing'):
    """发送聊天动作"""
    try:
        requests.post(
            f"{TG_API}/sendChatAction",
            json={'chat_id': chat_id, 'action': action},
            timeout=5
        )
    except:
        pass


# ============ 命令处理 ============
def handle_command(chat_id, text):
    """处理命令消息，返回 True 如果被处理"""
    cmd = text.strip().lower().split()[0] if text else ''

    if cmd == '/start':
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
        tg_send(chat_id, welcome)
        return True

    elif cmd == '/help':
        help_text = (
            "📋 *可用命令：*\n\n"
            "/start — 启动机器人\n"
            "/signals — 查看最新费率转负信号\n"
            "/heat — 查看热度做多雷达\n"
            "/btc — BTC 实时行情\n"
            "/help — 显示此帮助\n\n"
            "或者直接跟我聊天，我会 AI 回复你！"
        )
        tg_send(chat_id, help_text)
        return True

    elif cmd == '/signals':
        tg_send(chat_id, "📡 正在扫描费率转负信号，请稍候...")
        try:
            signals = scan_funding_reversal()
            if signals:
                strong = [s for s in signals if s['current_fr'] < 0 and s.get('oi_rising')]
                if strong:
                    msg = format_funding_alert(strong)
                    if msg:
                        tg_send(chat_id, msg)
                        return True
            tg_send(chat_id, "✅ 当前未发现新的费率转负信号")
        except Exception as e:
            tg_send(chat_id, f"❌ 扫描出错: {str(e)}")
        return True

    elif cmd == '/heat':
        tg_send(chat_id, "📊 正在扫描热度雷达，请稍候...")
        try:
            hot_list = scan_heat_radar()
            if hot_list:
                msg = format_heat_alert(hot_list[:8])
                if msg:
                    tg_send(chat_id, msg)
                    return True
            tg_send(chat_id, "📊 暂无热度数据")
        except Exception as e:
            tg_send(chat_id, f"❌ 扫描出错: {str(e)}")
        return True

    elif cmd == '/btc':
        tg_send(chat_id, "📈 正在获取 BTC 行情...")
        try:
            resp = requests.get(
                'https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=BTCUSDT',
                timeout=10
            )
            api_data = resp.json()
            # Binance 可能返回 dict 或 list
            if isinstance(api_data, list):
                api_data = api_data[0] if api_data else {}
            price = float(api_data.get('lastPrice', 0))
            chg = float(api_data.get('priceChangePercent', 0))
            high = float(api_data.get('highPrice', 0))
            low = float(api_data.get('lowPrice', 0))
            vol = float(api_data.get('quoteVolume', 0))
            emoji = "🟢" if chg >= 0 else "🔴"
            msg = (
                f"📈 *BTC/USDT 永续合约*\n\n"
                f"价格: ${price:,.2f}\n"
                f"24h涨跌: {emoji} {chg:+.2f}%\n"
                f"24h最高: ${high:,.2f}\n"
                f"24h最低: ${low:,.2f}\n"
                f"成交额: ${vol/1e9:.2f}B"
            )
            tg_send(chat_id, msg)
        except Exception as e:
            tg_send(chat_id, f"❌ 获取行情失败: {str(e)}")
        return True

    return False


# ============ AI 对话 ============
def ai_chat(user_message: str) -> str:
    """调用 DeepSeek API 生成回复"""
    if not AI_API_KEY:
        return "🤖 AI 未配置，请联系管理员设置 AI_API_KEY"

    system_prompt = """你是 Crypto Coke Bot，一个专业的加密货币交易助手。
你可以回答关于加密货币、区块链、交易策略的问题。
当前你有以下能力：
1. 币安合约 OI + 资金费率扫描
2. 热度做多雷达
3. 费率极端值监控（⚠️ 自动推送）
4. 多空配对套利信号（🔄 自动推送）
5. 自动交易执行（🤖 仅操作主人账户）
6. 加密货币知识问答

请用中文回复，语言风格亲切专业。
当用户问及市场行情或信号时，引导他们使用 /signals 或 /heat 命令。"""

    try:
        resp = requests.post(
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


# ============ Webhook 端点 ============
@app.route('/webhook', methods=['POST'])
def webhook():
    """Telegram bot webhook — 纯同步处理"""
    try:
        update = request.get_json(force=True)
        if not update:
            return jsonify({"ok": False, "error": "empty body"}), 400

        # 提取消息
        message = update.get('message', {})
        chat_id = message.get('chat', {}).get('id')
        text = message.get('text', '')

        if not chat_id or not text:
            return jsonify({"ok": True})  # 忽略非文本消息

        print(f"[Webhook] 收到消息: chat_id={chat_id}, text={text[:50]}")

        # 1. 先尝试命令处理
        if handle_command(chat_id, text):
            return jsonify({"ok": True})

        # 2. 非命令消息 → AI 对话
        tg_send_action(chat_id)
        reply = ai_chat(text)
        tg_send(chat_id, reply)

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


@app.route('/api/debug')
def api_debug():
    """调试信息"""
    return jsonify({
        'scanner_alive': latest_data.get('scanner_alive', False),
        'scanner_errors': latest_data.get('scanner_errors', []),
        'last_scan': latest_data.get('last_scan'),
        'total_coins': latest_data.get('total_coins'),
        'tg_configured': bool(TG_BOT_TOKEN and TG_CHAT_ID),
        'tg_token_prefix': TG_BOT_TOKEN[:15] + '...' if TG_BOT_TOKEN else 'NOT SET',
        'tg_chat_id': TG_CHAT_ID or 'NOT SET',
        'auto_trade': AUTO_TRADE_ENABLED,
        'sim_mode': SIMULATION_MODE,
        'binance_api': bool(BINANCE_API_KEY),
        'startup_time': latest_data.get('startup_time'),
        'server_time': time.strftime('%H:%M:%S'),
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

    # 启动标记 - 表示线程已启动
    latest_data['scanner_alive'] = True
    
    time.sleep(5)

    # 初始化去重文件
    fr_alert_file = SCRIPT_DIR / "fr_alert_history.json"
    
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

            current_min = datetime.now().minute

            # 2. 热度雷达 (每15分钟)
            if current_min % 15 < 5:
                hot_list = scan_heat_radar()
                if hot_list:
                    msg = format_heat_alert(hot_list[:8])
                    if msg and TG_BOT_TOKEN and TG_CHAT_ID:
                        send_tg(msg)
                        print(f"[Scanner] 推送热度 TOP8")
                    latest_data['heat_list'] = [{
                        'symbol': s['symbol'], 'coin': s['coin'],
                        'price': s['price'], 'price_chg_24h': s['price_chg_24h'],
                        'volume': s['volume'], 'oi_change_1h': s['oi_change_1h'],
                        'funding_rate': s['funding_rate'], 'heat_score': s['heat_score'],
                    } for s in hot_list[:15]]

            # 3. 费率极端值推送（每5分钟）
            try:
                fr_hist = {}
                if fr_alert_file.exists():
                    fr_hist = json.loads(fr_alert_file.read_text())
                extreme = scan_extreme_funding()
                if extreme:
                    new_x = [s for s in extreme
                             if not is_duplicate(s['symbol'], fr_hist, FR_DEDUP_HOURS)]
                    if new_x and TG_BOT_TOKEN and TG_CHAT_ID:
                        msg = format_extreme_funding_alert(new_x)
                        if msg:
                            send_tg(msg)
                            print(f"[Scanner] 推送 {len(new_x)} 个费率极端信号")
                            for s in new_x:
                                fr_hist = mark_alerted(s['symbol'], fr_hist, FR_DEDUP_HOURS)
                            fr_alert_file.write_text(json.dumps(fr_hist, indent=2))
            except Exception as e:
                print(f"[Scanner] 费率极端值错误: {e}")

            # 4. 套利信号（每15分钟）
            if current_min % 15 < 5:
                try:
                    arb_data = scan_arbitrage_signals()
                    if arb_data:
                        msg = format_arbitrage_alert(arb_data)
                        if msg and TG_BOT_TOKEN and TG_CHAT_ID:
                            send_tg(msg)
                            print(f"[Scanner] 推送套利信号")
                except Exception as e:
                    print(f"[Scanner] 套利信号错误: {e}")

            # 5. 模拟交易检查
            if not AUTO_TRADE_ENABLED:
                try:
                    simulate_auto_trade()
                except Exception as e:
                    print(f"[Scanner] 模拟交易错误: {e}")

            # 6. 自动交易（仅当开启且有API Key时）
            if AUTO_TRADE_ENABLED and BINANCE_API_KEY:
                try:
                    auto_trade()
                except Exception as e:
                    print(f"[Scanner] 自动交易错误: {e}")

            # 更新币种数
            try:
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
            err_msg = f"{time.strftime('%H:%M:%S')} {e}"
            print(f"[Scanner] 错误: {e}")
            latest_data['scanner_errors'] = (latest_data.get('scanner_errors', []) + [err_msg])[-5:]

        for _ in range(300):
            time.sleep(1)


# ============ 设置 Webhook URL ============
def set_webhook():
    """设置 Telegram webhook"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
    if not render_url:
        print("[Webhook] RENDER_EXTERNAL_URL 未设置")
        return False
    webhook_url = f"{render_url}/webhook"
    try:
        resp = requests.post(
            f"{TG_API}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]},
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
    print(f"AI: {'已配置' if AI_API_KEY else '未配置'}")
    print(f"{'='*50}")

    # 启动后台扫描线程
    scanner = threading.Thread(target=run_scanner, daemon=True)
    scanner.start()

    # 恢复自动交易状态
    if AUTO_TRADE_ENABLED and BINANCE_API_KEY:
        state = recover_trade_state()
        if state.get('position'):
            print(f"↩️ 恢复持仓: {state['symbol']} {state['side']}")

    # 尝试设置 webhook
    set_webhook()

    # 启动 Flask
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Web 服务: http://0.0.0.0:{port}")
    print(f"🤖 Webhook: /webhook")
    print(f"📊 Dashboard: /")
    app.run(host='0.0.0.0', port=port)
