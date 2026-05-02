#!/usr/bin/env python3
"""
🚀 Crypto Radar — 币安合约雷达
- OI持续放大 + 费率由正转负 扫描
- 热度排行 + OI异动分析
- 费率极端值推送（做多/做空信号）
- 套利信号推荐（多空组合配对）
- 自动交易（仅操作主人账户）
- Telegram Bot 推送
- 每5分钟运行一次
"""

import requests
import json
import os
import time
import sys
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

# ============ 配置 ============
SCRIPT_DIR = Path(__file__).parent
ALERT_HISTORY_FILE = SCRIPT_DIR / "alert_history.json"
FR_SNAPSHOT_FILE = SCRIPT_DIR / "fr_snapshot.json"
TRADE_STATE_FILE = SCRIPT_DIR / "trade_state.json"

# 信号参数
MIN_OI_CHANGE_PCT = 8
MIN_VOLUME_USDT = 0
DEDUP_HOURS = 24
FR_DEDUP_HOURS = 4        # 费率极端值去重窗口

# 自动交易参数（硬编码安全底线）
MAX_POSITION_USDT = 10     # 每次最多 $10
STOP_LOSS_PCT = -5.0       # 硬止损 -5%
TAKE_PROFIT_PCT = 3.0      # 止盈 +3%
AUTO_TRADE_INTERVAL = 60   # 交易检查频率（秒）

# ============ 加载环境变量 ============
def load_env():
    env = {}
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().strip().split('\n'):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    for k in ['TG_BOT_TOKEN', 'TG_CHAT_ID', 'SCAN_INTERVAL',
              'BINANCE_API_KEY', 'BINANCE_API_SECRET',
              'AUTO_TRADE_ENABLED', 'TRADE_POSITION_USDT',
              'FR_ALERT_THRESHOLD']:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

env = load_env()
TG_BOT_TOKEN = env.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = env.get('TG_CHAT_ID', '')
SCAN_INTERVAL = int(env.get('SCAN_INTERVAL', '300'))

# 币安API（哥哥自己的）
BINANCE_API_KEY = env.get('BINANCE_API_KEY', '')
BINANCE_API_SECRET = env.get('BINANCE_API_SECRET', '')
AUTO_TRADE_ENABLED = env.get('AUTO_TRADE_ENABLED', 'false').lower() == 'true'
TRADE_POSITION_USDT = float(env.get('TRADE_POSITION_USDT', str(MAX_POSITION_USDT)))
TRADE_POSITION_USDT = min(TRADE_POSITION_USDT, MAX_POSITION_USDT)  # 不能超过硬编码上限

# 费率极端值阈值（万分之五 = 0.05%）
FR_ALERT_THRESHOLD = float(env.get('FR_ALERT_THRESHOLD', '0.0005'))

# ============ TG推送 ============
def send_tg(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[TG] 未配置, 仅打印:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        try:
            resp = requests.post(url, json={
                'chat_id': TG_CHAT_ID,
                'text': chunk,
                'parse_mode': 'Markdown'
            }, timeout=10)
            if resp.status_code != 200:
                requests.post(url, json={
                    'chat_id': TG_CHAT_ID,
                    'text': chunk
                }, timeout=10)
        except Exception as e:
            print(f"[TG] 发送失败: {e}")

# ============ 去重 ============
def load_history(path=None):
    if path is None:
        path = ALERT_HISTORY_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except:
            return {}
    return {}

def save_history(history, path=None):
    if path is None:
        path = ALERT_HISTORY_FILE
    path.write_text(json.dumps(history, indent=2))

def is_duplicate(symbol, history, hours=None):
    if hours is None:
        hours = DEDUP_HOURS
    if symbol not in history:
        return False
    last = datetime.fromisoformat(history[symbol])
    return (datetime.now() - last).total_seconds() < hours * 3600

def mark_alerted(symbol, history, hours=None):
    if hours is None:
        hours = DEDUP_HOURS
    history[symbol] = datetime.now().isoformat()
    cutoff = datetime.now() - timedelta(hours=hours * 2)
    history = {k: v for k, v in history.items()
               if datetime.fromisoformat(v) > cutoff}
    return history

# ============ 费率快照 ============
def load_fr_snapshot():
    if FR_SNAPSHOT_FILE.exists():
        try:
            return json.loads(FR_SNAPSHOT_FILE.read_text())
        except:
            pass
    return {}

def save_fr_snapshot(snapshot):
    FR_SNAPSHOT_FILE.write_text(json.dumps(snapshot))

# ============ 币安API签名工具 ============
def binance_sign(params):
    """生成币安HMAC-SHA256签名"""
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        BINANCE_API_SECRET.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature

def binance_request(method, endpoint, params=None, signed=False):
    """通用的币安API请求"""
    if params is None:
        params = {}
    headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
    if signed:
        params['timestamp'] = int(time.time() * 1000)
        params['signature'] = binance_sign(params)
    
    url = f"https://fapi.binance.com{endpoint}"
    try:
        if method == 'GET':
            r = requests.get(url, params=params, headers=headers, timeout=10)
        else:
            r = requests.post(url, params=params, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[BINANCE] API错误: {e}")
        return None

def binance_get_position(symbol=None):
    """获取当前持仓信息"""
    params = {}
    if symbol:
        params['symbol'] = symbol
    result = binance_request('GET', '/fapi/v2/positionRisk', params, signed=True)
    if not result:
        return []
    if symbol:
        # 单个币种查询返回list
        return [p for p in (result if isinstance(result, list) else [result]) 
                if float(p.get('positionAmt', 0)) != 0]
    return [p for p in (result if isinstance(result, list) else []) 
            if float(p.get('positionAmt', 0)) != 0]

def binance_place_order(symbol, side, quantity, leverage=1):
    """开仓（市价单）"""
    # 先设置杠杆
    binance_request('POST', '/fapi/v1/leverage', {
        'symbol': symbol,
        'leverage': leverage
    }, signed=True)
    
    # 开仓
    return binance_request('POST', '/fapi/v1/order', {
        'symbol': symbol,
        'side': side,
        'type': 'MARKET',
        'quantity': quantity,
    }, signed=True)

def binance_close_position(symbol):
    """全平持仓"""
    # 获取当前持仓方向
    positions = binance_get_position(symbol)
    if not positions:
        return None
    
    pos = positions[0]
    amt = abs(float(pos['positionAmt']))
    side = 'SELL' if float(pos['positionAmt']) > 0 else 'BUY'
    
    return binance_request('POST', '/fapi/v1/order', {
        'symbol': symbol,
        'side': side,
        'type': 'MARKET',
        'quantity': amt,
    }, signed=True)

# ============ 交易状态管理 ============
def load_trade_state():
    """加载交易状态"""
    if TRADE_STATE_FILE.exists():
        try:
            return json.loads(TRADE_STATE_FILE.read_text())
        except:
            pass
    return {'position': None, 'entry_price': 0, 'entry_time': None,
            'symbol': None, 'side': None}

def save_trade_state(state):
    """保存交易状态"""
    TRADE_STATE_FILE.write_text(json.dumps(state, indent=2))

def recover_trade_state():
    """从币安恢复交易状态（重启后）"""
    positions = binance_get_position()
    if positions:
        pos = positions[0]
        coin = pos['symbol']
        amt = float(pos['positionAmt'])
        entry = float(pos.get('entryPrice', 0))
        side = 'LONG' if amt > 0 else 'SHORT'
        state = {
            'position': coin,
            'entry_price': entry,
            'entry_time': datetime.now().isoformat(),
            'symbol': coin,
            'side': side
        }
        save_trade_state(state)
        print(f"[TRADE] 从链上恢复持仓: {coin} {side} @ ${entry}")
        return state
    return load_trade_state()

# ============ 核心扫描: OI + 费率转负 ============
def scan_funding_reversal():
    """扫描资金费率由正转负 + OI放大的币种"""
    ts_start = time.time()
    
    try:
        info = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=10).json()
        symbols = [s['symbol'] for s in info['symbols']
                   if s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    except Exception as e:
        print(f"[ERROR] exchangeInfo: {e}")
        return []
    
    try:
        tickers = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10).json()
        ticker_map = {t['symbol']: t for t in tickers}
    except Exception as e:
        print(f"[ERROR] ticker: {e}")
        return []
    
    active = [s for s in symbols if float(ticker_map.get(s, {}).get('quoteVolume', 0)) > MIN_VOLUME_USDT]
    
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
        fr_current = {item['symbol']: float(item['lastFundingRate']) for item in fr_all}
    except:
        fr_current = {}
    
    prev_snapshot = load_fr_snapshot()
    save_fr_snapshot(fr_current)
    
    if not prev_snapshot:
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"[{ts}] 首次运行，保存快照")
        return []
    
    just_turned_negative = []
    for sym in active:
        prev_fr = prev_snapshot.get(sym)
        curr_fr = fr_current.get(sym)
        if prev_fr is None or curr_fr is None:
            continue
        if prev_fr >= 0 and curr_fr < 0:
            just_turned_negative.append(sym)
    
    if not just_turned_negative:
        elapsed = time.time() - ts_start
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"[{ts}] 扫描 {len(active)}币/{elapsed:.1f}s, 无新转负")
        return []
    
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 发现 {len(just_turned_negative)} 个刚转负")
    
    signals = []
    for sym in just_turned_negative:
        try:
            oi_hist = requests.get('https://fapi.binance.com/futures/data/openInterestHist',
                params={'symbol': sym, 'period': '1h', 'limit': 48}, timeout=10).json()
            
            oi_chg = 0
            segs = []
            oi_rising = False
            if oi_hist and len(oi_hist) >= 12:
                oi_values = [float(x['sumOpenInterestValue']) for x in oi_hist]
                seg_len = len(oi_values) // 4
                if seg_len >= 3:
                    segs = [
                        sum(oi_values[:seg_len]) / seg_len,
                        sum(oi_values[seg_len:seg_len*2]) / seg_len,
                        sum(oi_values[seg_len*2:seg_len*3]) / seg_len,
                        sum(oi_values[seg_len*3:]) / max(1, len(oi_values[seg_len*3:]))
                    ]
                    oi_chg = (segs[3] - segs[0]) / segs[0] * 100 if segs[0] > 0 else 0
                    oi_rising = oi_chg > 0
            
            t = ticker_map.get(sym, {})
            signals.append({
                'symbol': sym,
                'price': float(t.get('lastPrice', 0)),
                'price_chg_24h': float(t.get('priceChangePercent', 0)),
                'volume': float(t.get('quoteVolume', 0)),
                'oi_change': oi_chg,
                'oi_segments': segs,
                'oi_rising': oi_rising,
                'current_fr': fr_current.get(sym, 0),
                'prev_fr': prev_snapshot.get(sym, 0),
            })
        except:
            continue
    
    elapsed = time.time() - ts_start
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 扫描完成: {len(active)}币/{elapsed:.1f}s, 信号: {len(signals)}")
    
    return signals


# ============ 核心扫描: 费率极端值推送 ============
def scan_extreme_funding():
    """
    扫描费率极端值（绝对值超过阈值）
    正费率=多头付钱→做空有利
    负费率=空头付钱→做多有利
    """
    ts_start = time.time()
    
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
    except:
        return []
    
    # 获取成交量过滤
    try:
        tickers = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10).json()
        ticker_map = {t['symbol']: t for t in tickers}
    except:
        ticker_map = {}
    
    # 获取OI数据（批量）
    results = []
    for item in fr_all:
        sym = item['symbol']
        fr = float(item['lastFundingRate'])
        
        # 只关注有成交量的币
        vol = float(ticker_map.get(sym, {}).get('quoteVolume', 0))
        if vol < 500_000:  # 忽略小币
            continue
        
        if abs(fr) >= FR_ALERT_THRESHOLD:
            t = ticker_map.get(sym, {})
            # 查OI
            oi_chg_1h = 0
            try:
                oi_hist = requests.get('https://fapi.binance.com/futures/data/openInterestHist',
                    params={'symbol': sym, 'period': '1h', 'limit': 4}, timeout=5).json()
                if oi_hist and len(oi_hist) >= 2:
                    oi_now = float(oi_hist[-1]['sumOpenInterestValue'])
                    oi_before = float(oi_hist[0]['sumOpenInterestValue'])
                    oi_chg_1h = (oi_now - oi_before) / oi_before * 100 if oi_before > 0 else 0
            except:
                pass
            
            results.append({
                'symbol': sym,
                'coin': sym.replace('USDT', ''),
                'funding_rate': fr,
                'price': float(t.get('lastPrice', 0)),
                'price_chg_24h': float(t.get('priceChangePercent', 0)),
                'volume': vol,
                'oi_change_1h': oi_chg_1h,
            })
    
    # 按费率绝对值排序
    results.sort(key=lambda x: abs(x['funding_rate']), reverse=True)
    
    elapsed = time.time() - ts_start
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 费率极端值扫描: 发现 {len(results)} 个 (阈值>{FR_ALERT_THRESHOLD:.4%})/{elapsed:.1f}s")
    
    return results[:10]


# ============ 核心扫描: 套利信号 ============
def scan_arbitrage_signals():
    """
    多空配对套利信号
    找：费率最负（做多有利） + 费率最正（做空有利）
    配对给哥哥推荐
    """
    ts_start = time.time()
    
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
    except:
        return []
    
    try:
        tickers = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10).json()
        ticker_map = {t['symbol']: t for t in tickers}
    except:
        ticker_map = {}
    
    candidates = []
    for item in fr_all:
        sym = item['symbol']
        fr = float(item['lastFundingRate'])
        vol = float(ticker_map.get(sym, {}).get('quoteVolume', 0))
        if vol < 500_000:
            continue
        t = ticker_map.get(sym, {})
        candidates.append({
            'symbol': sym,
            'coin': sym.replace('USDT', ''),
            'funding_rate': fr,
            'price': float(t.get('lastPrice', 0)),
            'price_chg_24h': float(t.get('priceChangePercent', 0)),
            'volume': vol,
        })
    
    # 按费率排序
    candidates.sort(key=lambda x: x['funding_rate'])
    
    # 最负的前5个（做多有利）
    long_candidates = candidates[:5]
    # 最正的前5个（做空有利）
    short_candidates = [c for c in candidates if c['funding_rate'] > 0]
    short_candidates.sort(key=lambda x: x['funding_rate'], reverse=True)
    short_candidates = short_candidates[:5]
    
    elapsed = time.time() - ts_start
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 套利信号扫描: {elapsed:.1f}s")
    
    return {
        'long': long_candidates,
        'short': short_candidates
    }


# ============ 自动交易逻辑 ============
def auto_trade():
    """自动交易执行（仅操作哥哥自己的账户）"""
    if not AUTO_TRADE_ENABLED:
        return
    
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("[TRADE] 未配置API Key，跳过自动交易")
        return
    
    ts = datetime.now().strftime('%H:%M:%S')
    trade_state = load_trade_state()
    current_pos = trade_state.get('position')
    
    print(f"[{ts}] [TRADE] 自动交易检查...")
    
    # 如果有持仓 → 检查平仓条件
    if current_pos:
        check_close_conditions(trade_state)
        return
    
    # 无持仓 → 检查开仓信号
    check_open_signals()


def check_open_signals():
    """检查开仓信号"""
    ts = datetime.now().strftime('%H:%M:%S')
    
    # 获取当前所有费率
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
    except:
        return
    
    try:
        tickers = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10).json()
        ticker_map = {t['symbol']: t for t in tickers}
    except:
        ticker_map = {}
    
    # 找信号：费率极端 + OI配合
    best_long = None   # 做多信号（费率最负）
    best_short = None  # 做空信号（费率最正）
    
    for item in fr_all:
        sym = item['symbol']
        fr = float(item['lastFundingRate'])
        vol = float(ticker_map.get(sym, {}).get('quoteVolume', 0))
        if vol < 1_000_000:
            continue
        
        t = ticker_map.get(sym, {})
        price = float(t.get('lastPrice', 0))
        
        # 查OI
        oi_chg = 0
        try:
            oi = requests.get('https://fapi.binance.com/futures/data/openInterestHist',
                params={'symbol': sym, 'period': '1h', 'limit': 4}, timeout=5).json()
            if oi and len(oi) >= 2:
                now_val = float(oi[-1]['sumOpenInterestValue'])
                before_val = float(oi[0]['sumOpenInterestValue'])
                oi_chg = (now_val - before_val) / before_val * 100 if before_val > 0 else 0
        except:
            pass
        
        # 做多条件：费率 < -0.05%（空头付钱多）且 OI涨
        if fr < -FR_ALERT_THRESHOLD and oi_chg > 3:
            if best_long is None or fr < best_long['funding_rate']:
                best_long = {'symbol': sym, 'funding_rate': fr, 'price': price, 'oi_chg': oi_chg}
        
        # 做空条件：费率 > 0.05%（多头付钱多）且 OI跌
        if fr > FR_ALERT_THRESHOLD and oi_chg < -3:
            if best_short is None or fr > best_short['funding_rate']:
                best_short = {'symbol': sym, 'funding_rate': fr, 'price': price, 'oi_chg': oi_chg}
    
    # 如果有做空信号且没有做多信号 → 只做空
    if best_short and not best_long:
        execute_trade(best_short['symbol'], 'SHORT', best_short['price'],
                      best_short['funding_rate'], '费率极端正 + OI下跌')
        return
    
    # 如果有做多信号且没有做空信号 → 只做多
    if best_long and not best_short:
        execute_trade(best_long['symbol'], 'LONG', best_long['price'],
                      best_long['funding_rate'], '费率极端负 + OI上涨')
        return
    
    # 两个信号都有 → 优先选更强的
    if best_long and best_short:
        # 比较信号强度
        long_strength = abs(best_long['funding_rate']) + best_long['oi_chg'] / 10
        short_strength = best_short['funding_rate'] + abs(best_short['oi_chg']) / 10
        if long_strength >= short_strength:
            execute_trade(best_long['symbol'], 'LONG', best_long['price'],
                          best_long['funding_rate'], '费率极端负 + OI上涨')
        else:
            execute_trade(best_short['symbol'], 'SHORT', best_short['price'],
                          best_short['funding_rate'], '费率极端正 + OI下跌')
    
    if not best_long and not best_short:
        print(f"[{ts}] [TRADE] 无合适开仓信号")


def execute_trade(symbol, side, price, fr, reason):
    """执行开仓"""
    ts = datetime.now().strftime('%H:%M:%S')
    coin = symbol.replace('USDT', '')
    
    print(f"[{ts}] [TRADE] 🔥 开仓信号: {coin} {side}")
    print(f"         费率: {fr:+.4%}  当前价: ${price}")
    print(f"         理由: {reason}")
    
    # 检查链上是否已有仓位
    existing = binance_get_position(symbol)
    if existing:
        print(f"[{ts}] [TRADE] ⚠️ {coin}已有持仓，跳过")
        return
    
    # 计算仓位大小
    usdt_amount = min(TRADE_POSITION_USDT, MAX_POSITION_USDT)
    quantity = round(usdt_amount / price, 3)
    if quantity <= 0:
        print(f"[TRADE] 数量计算异常: ${usdt_amount} / ${price} = {quantity}")
        return
    
    # 开仓
    binance_side = 'BUY' if side == 'LONG' else 'SELL'
    result = binance_place_order(symbol, binance_side, quantity)
    
    if result and 'orderId' in result:
        # 保存状态
        state = {
            'position': symbol,
            'entry_price': price,
            'entry_time': datetime.now().isoformat(),
            'symbol': symbol,
            'side': side,
            'quantity': quantity,
            'reason': reason,
            'entry_fr': fr,
        }
        save_trade_state(state)
        
        print(f"[{ts}] [TRADE] ✅ 开仓成功!")
        print(f"         {coin} {side} 数量:{quantity} 均价:${price:.4f}")
        
        msg = (
            f"🤖 *自动交易开仓*\n"
            f"┌─────────────────────\n"
            f"│ {coin} **{side}**\n"
            f"│ 数量: {quantity}\n"
            f"│ 均价: ${price:.4f}\n"
            f"│ 费率: {fr:+.4%}\n"
            f"│ 理由: {reason}\n"
            f"│ 止损: -{abs(STOP_LOSS_PCT):.0f}%  止盈: +{TAKE_PROFIT_PCT:.0f}%\n"
            f"└─────────────────────"
        )
        send_tg(msg)
    else:
        print(f"[{ts}] [TRADE] ❌ 开仓失败: {result}")
        send_tg(f"❌ 自动交易开仓失败 {coin} {side}: {result}")


def check_close_conditions(trade_state):
    """检查平仓条件"""
    ts = datetime.now().strftime('%H:%M:%S')
    symbol = trade_state['symbol']
    side = trade_state['side']
    entry_price = trade_state['entry_price']
    coin = symbol.replace('USDT', '')
    
    # 获取当前价格
    try:
        ticker = requests.get(f'https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}', timeout=5).json()
        current_price = float(ticker.get('lastPrice', 0))
    except:
        print(f"[{ts}] [TRADE] 获取价格失败")
        return
    
    # 计算盈亏
    if side == 'LONG':
        pnl_pct = (current_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - current_price) / entry_price * 100
    
    print(f"[{ts}] [TRADE] {coin} {side}  入场:${entry_price:.4f} 当前:${current_price:.4f} 盈亏:{pnl_pct:+.2f}%")
    
    # 检查止损
    if pnl_pct <= STOP_LOSS_PCT:
        print(f"[{ts}] [TRADE] 🛑 触发止损! {pnl_pct:.2f}% <= {STOP_LOSS_PCT:.0f}%")
        close_and_notify(symbol, entry_price, current_price, side, pnl_pct, '止损')
        return
    
    # 检查止盈
    if pnl_pct >= TAKE_PROFIT_PCT:
        print(f"[{ts}] [TRADE] 🎯 触发止盈! {pnl_pct:.2f}% >= {TAKE_PROFIT_PCT:.0f}%")
        close_and_notify(symbol, entry_price, current_price, side, pnl_pct, '止盈')
        return
    
    # 检查费率回归（没有极端费率了就平）
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=5).json()
        fr_map = {item['symbol']: float(item['lastFundingRate']) for item in fr_all}
        current_fr = fr_map.get(symbol, 0)
        
        # 如果费率回归到正常范围（绝对值 < 阈值的一半）
        if abs(current_fr) < FR_ALERT_THRESHOLD / 2 and pnl_pct > 0:
            print(f"[{ts}] [TRADE] 📊 费率回归正常，平仓获利")
            close_and_notify(symbol, entry_price, current_price, side, pnl_pct, '费率回归正常')
            return
    except:
        pass


def close_and_notify(symbol, entry_price, current_price, side, pnl_pct, reason):
    """平仓并通知"""
    ts = datetime.now().strftime('%H:%M:%S')
    result = binance_close_position(symbol)
    coin = symbol.replace('USDT', '')
    
    # 清空状态
    save_trade_state({'position': None, 'entry_price': 0, 'entry_time': None,
                      'symbol': None, 'side': None})
    
    emoji = '🟢' if pnl_pct > 0 else '🔴'
    
    if result and 'orderId' in result:
        print(f"[{ts}] [TRADE] ✅ 平仓成功 {coin} {side}")
        msg = (
            f"{emoji} *自动交易平仓*\n"
            f"┌─────────────────────\n"
            f"│ {coin} {side}\n"
            f"│ 入场: ${entry_price:.4f}  →  出场: ${current_price:.4f}\n"
            f"│ 盈亏: **{pnl_pct:+.2f}%**\n"
            f"│ 理由: {reason}\n"
            f"└─────────────────────"
        )
        send_tg(msg)
    else:
        print(f"[{ts}] [TRADE] ❌ 平仓失败: {result}")


# ============ 核心扫描: 热度雷达 ============
def scan_heat_radar():
    """热度做多雷达: OI异动 + 24h涨幅 + 成交量排行"""
    ts_start = time.time()
    
    try:
        info = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=10).json()
        symbols = [s['symbol'] for s in info['symbols']
                   if s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    except Exception as e:
        print(f"[ERROR] exchangeInfo: {e}")
        return []
    
    try:
        tickers = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10).json()
        ticker_map = {t['symbol']: t for t in tickers}
    except Exception as e:
        print(f"[ERROR] ticker: {e}")
        return []
    
    active = [(s, float(ticker_map.get(s, {}).get('quoteVolume', 0)))
              for s in symbols if float(ticker_map.get(s, {}).get('quoteVolume', 0)) > 500_000]
    
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
        fr_map = {item['symbol']: float(item['lastFundingRate']) for item in fr_all}
    except:
        fr_map = {}
    
    hot_list = []
    for sym, vol in active:
        try:
            t = ticker_map.get(sym, {})
            price_chg = float(t.get('priceChangePercent', 0))
            price = float(t.get('lastPrice', 0))
            
            oi_hist = requests.get('https://fapi.binance.com/futures/data/openInterestHist',
                params={'symbol': sym, 'period': '1h', 'limit': 4}, timeout=10).json()
            
            oi_chg_1h = 0
            oi_value = 0
            if oi_hist and len(oi_hist) >= 2:
                oi_now = float(oi_hist[-1]['sumOpenInterestValue'])
                oi_before = float(oi_hist[0]['sumOpenInterestValue'])
                oi_value = oi_now
                oi_chg_1h = (oi_now - oi_before) / oi_before * 100 if oi_before > 0 else 0
            
            fr = fr_map.get(sym, 0)
            coin = sym.replace('USDT', '')
            
            heat_score = 0
            if oi_chg_1h > 5: heat_score += 3
            elif oi_chg_1h > 2: heat_score += 1
            if price_chg > 3: heat_score += 2
            elif price_chg > 1: heat_score += 1
            if price_chg < -3: heat_score += 1
            if fr < -0.01: heat_score += 2
            heat_score += min(vol / 10_000_000, 3)
            
            hot_list.append({
                'symbol': sym, 'coin': coin, 'price': price,
                'price_chg_24h': price_chg, 'volume': vol,
                'oi_change_1h': oi_chg_1h, 'oi_value': oi_value,
                'funding_rate': fr, 'heat_score': heat_score,
            })
        except:
            continue
    
    hot_list.sort(key=lambda x: x['heat_score'], reverse=True)
    
    elapsed = time.time() - ts_start
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 热度扫描完成: {len(active)}币/{elapsed:.1f}s")
    
    return hot_list[:15]


# ============ 格式化推送 ============
def format_funding_alert(signals):
    if not signals:
        return None
    signals.sort(key=lambda x: (-int(x.get('oi_rising', False)), x['current_fr']))
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f"🔥 *费率转负 + OI 扫描*  🔥\n{now}\n"]
    for s in signals:
        coin = s['symbol'].replace('USDT', '')
        fr_change = f"{s['prev_fr']:+.4%} → {s['current_fr']:+.4%}"
        lines.append("```")
        lines.append(f"#{coin}")
        lines.append(f"  价格: ${s['price']:.4f}  24h: {s['price_chg_24h']:+.1f}%")
        lines.append(f"  费率: {fr_change}")
        if s['oi_segments']:
            oi_segs = ' > '.join([f"{v/1e6:.1f}M" for v in s['oi_segments']])
            lines.append(f"  OI: +{s['oi_change']:.1f}%  ({oi_segs})")
        lines.append(f"  成交额: ${s['volume']/1e6:.1f}M")
        lines.append("```")
    return '\n'.join(lines)

def format_heat_alert(hot_list):
    if not hot_list:
        return None
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f"📊 *热度做多雷达*  📊\n{now}\n"]
    for i, s in enumerate(hot_list[:8], 1):
        fr_str = f"{s['funding_rate']:+.4%}"
        lines.append(f"#{i} [{s['coin']}]")
        lines.append(f"  价格: ${s['price']:.4f}  24h: {s['price_chg_24h']:+.1f}%")
        lines.append(f"  OI: {s['oi_change_1h']:+.1f}%  |  费率: {fr_str}")
        lines.append(f"  成交额: ${s['volume']/1e6:.1f}M")
    lines.append(f"\n共扫描 {len(hot_list)} 个热门币")
    return '\n'.join(lines)

def format_extreme_funding_alert(extreme_list):
    """格式化费率极端值推送"""
    if not extreme_list:
        return None
    
    # 按费率绝对值排序
    sorted_list = sorted(extreme_list, key=lambda x: abs(x['funding_rate']), reverse=True)
    
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f"⚠️ *费率极端值监控* ⚠️\n阈值: >{FR_ALERT_THRESHOLD:.4%}\n{now}\n"]
    
    for s in sorted_list:
        fr = s['funding_rate']
        direction = '🔥 做多有利' if fr < 0 else '❄️ 做空有利'
        lines.append(f"{s['coin']} | 费率: {fr:+.4%} | {direction}")
        lines.append(f"  价: ${s['price']:.4f}  24h: {s['price_chg_24h']:+.1f}%")
        lines.append(f"  OI 1h: {s['oi_change_1h']:+.1f}%  成交: ${s['volume']/1e6:.1f}M")
    
    return '\n'.join(lines)

def format_arbitrage_alert(arb_data):
    """格式化套利信号"""
    if not arb_data or not arb_data.get('long') or not arb_data.get('short'):
        return None
    
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f"🔄 *多空配对套利信号* 🔄\n{now}\n"]
    
    # 做多候选
    lines.append("📈 *做多候选（费率最负，空头付钱）*")
    for s in arb_data['long'][:3]:
        lines.append(f"  {s['coin']} | 费率: {s['funding_rate']:+.4%} | ${s['price']:.4f}")
    
    lines.append("")
    # 做空候选
    lines.append("📉 *做空候选（费率最正，多头付钱）*")
    for s in arb_data['short'][:3]:
        lines.append(f"  {s['coin']} | 费率: {s['funding_rate']:+.4%} | ${s['price']:.4f}")
    
    # 推荐配对
    lines.append("")
    lines.append("*推荐配对（费率套利）*")
    for i in range(min(3, len(arb_data['long']), len(arb_data['short']))):
        l = arb_data['long'][i]
        s = arb_data['short'][i]
        spread = abs(l['funding_rate']) + abs(s['funding_rate'])
        lines.append(f"  配对#{i+1}: 多 {l['coin']}  空 {s['coin']}  |  费率差: {spread:.4%}")
    
    lines.append("\n💡 套利逻辑：多空等额对冲，赚取费率差")
    return '\n'.join(lines)


# ============ 附加信息 ============
def get_market_caps():
    mcap = {}
    try:
        r = requests.get(
            "https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list",
            timeout=10
        )
        if r.status_code == 200:
            for item in r.json().get("data", []):
                name = item.get("name", "")
                mc = item.get("marketCap", 0)
                if name and mc:
                    mcap[name] = float(mc)
    except:
        pass
    return mcap

def get_spot_symbols():
    try:
        info = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=10).json()
        return {s["baseAsset"] for s in info["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    except:
        return set()

def format_mcap(v):
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# ============ 单次运行 ============
def run_once():
    ts = datetime.now().strftime('%m-%d %H:%M:%S')
    print(f"\n{'='*50}")
    print(f"🚀 Crypto Radar — {ts}")
    print(f"{'='*50}")
    
    # 1. OI + 费率转负扫描
    print("\n📡 扫描费率转负...")
    signals = scan_funding_reversal()
    if signals:
        strong = [s for s in signals if s['current_fr'] < 0 and s.get('oi_rising')]
        if strong:
            msg = format_funding_alert(strong)
            if msg:
                send_tg(msg)
                print(f"  ✅ 推送 {len(strong)} 个费率转负信号")
        else:
            print(f"  {len(signals)} 个转负但无OI在涨, 跳过")
    else:
        print("  无费率转负信号")
    
    # 2. 费率极端值推送（每5分钟）
    print("\n⚠️ 扫描费率极端值...")
    fr_history = load_history(SCRIPT_DIR / "fr_alert_history.json")
    extreme = scan_extreme_funding()
    if extreme:
        # 去重后推送
        new_extreme = [s for s in extreme 
                       if not is_duplicate(s['symbol'], fr_history, FR_DEDUP_HOURS)]
        if new_extreme:
            # 更新去重记录
            for s in new_extreme:
                fr_history = mark_alerted(s['symbol'], fr_history, FR_DEDUP_HOURS)
            save_history(fr_history, SCRIPT_DIR / "fr_alert_history.json")
            msg = format_extreme_funding_alert(new_extreme)
            if msg:
                send_tg(msg)
                print(f"  ✅ 推送 {len(new_extreme)} 个费率极端信号")
        else:
            print(f"  全部重复, 跳过推送")
    else:
        print("  无费率极端值")
    
    # 3. 套利信号（每15分钟）
    current_min = datetime.now().minute
    if current_min % 15 < 5:
        print("\n🔄 扫描套利信号...")
        arb_data = scan_arbitrage_signals()
        if arb_data:
            msg = format_arbitrage_alert(arb_data)
            if msg:
                send_tg(msg)
                print(f"  ✅ 推送套利信号")
    else:
        print("\n🔄 套利信号本次跳过")
    
    # 4. 热度雷达
    if current_min % 15 < 5:
        print("\n📊 扫描热度雷达...")
        hot_list = scan_heat_radar()
        if hot_list:
            msg = format_heat_alert(hot_list)
            if msg:
                send_tg(msg)
                print(f"  ✅ 推送热度排行 TOP8")
    else:
        print("\n📊 热度雷达本次跳过")
    
    # 5. 自动交易（每次运行都检查）
    if AUTO_TRADE_ENABLED:
        print("\n🤖 自动交易检查...")
        auto_trade()
    
    print(f"\n✅ 完成\n")


# ============ 主入口 ============
if __name__ == '__main__':
    import schedule
    
    print("🚀 Crypto Radar 启动中...")
    print(f"TG Bot: {'已配置' if TG_BOT_TOKEN else '未配置'}")
    print(f"扫描间隔: {SCAN_INTERVAL}秒")
    if AUTO_TRADE_ENABLED:
        print(f"🤖 自动交易: 已开启 (每笔 ${TRADE_POSITION_USDT})")
        print(f"   止损: {STOP_LOSS_PCT:.0f}%  止盈: +{TAKE_PROFIT_PCT:.0f}%")
        print(f"   API Key: {'已配置' if BINANCE_API_KEY else '未配置'}")
    else:
        print(f"🤖 自动交易: 未开启（设置 AUTO_TRADE_ENABLED=true 开启）")
    print(f"{'='*50}")
    
    # 如果自动交易开启，先恢复状态
    if AUTO_TRADE_ENABLED and BINANCE_API_KEY:
        state = recover_trade_state()
        if state.get('position'):
            print(f"↩️ 恢复持仓: {state['symbol']} {state['side']}")
    
    # 先跑一次
    run_once()
    
    # 定时运行
    schedule.every(SCAN_INTERVAL).seconds.do(run_once)
    
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 停止")
