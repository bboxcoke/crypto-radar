#!/usr/bin/env python3
"""
🚀 Crypto Radar — 币安合约雷达
- OI持续放大 + 费率由正转负 扫描
- 热度排行 + OI异动分析
- Telegram Bot 推送
- 每5分钟运行一次

参考: connectfarm1.com 潜水观察员
改进: 单文件可部署, 纯API零成本
"""

import requests
import json
import os
import time
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ============ 配置 ============
SCRIPT_DIR = Path(__file__).parent
ALERT_HISTORY_FILE = SCRIPT_DIR / "alert_history.json"
FR_SNAPSHOT_FILE = SCRIPT_DIR / "fr_snapshot.json"

# 信号参数
MIN_OI_CHANGE_PCT = 8       # OI总涨幅最低8%
MIN_VOLUME_USDT = 0         # 无门槛
DEDUP_HOURS = 24            # 去重窗口

# ============ 加载环境变量 ============
def load_env():
    """从环境变量或 .env 文件加载配置"""
    env = {}
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().strip().split('\n'):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    # 环境变量优先
    for k in ['TG_BOT_TOKEN', 'TG_CHAT_ID', 'SCAN_INTERVAL']:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env

env = load_env()
TG_BOT_TOKEN = env.get('TG_BOT_TOKEN', '')
TG_CHAT_ID = env.get('TG_CHAT_ID', '')
SCAN_INTERVAL = int(env.get('SCAN_INTERVAL', '300'))  # 默认5分钟

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
def load_history():
    if ALERT_HISTORY_FILE.exists():
        try:
            return json.loads(ALERT_HISTORY_FILE.read_text())
        except:
            return {}
    return {}

def save_history(history):
    ALERT_HISTORY_FILE.write_text(json.dumps(history, indent=2))

def is_duplicate(symbol, history):
    if symbol not in history:
        return False
    last = datetime.fromisoformat(history[symbol])
    return (datetime.now() - last).total_seconds() < DEDUP_HOURS * 3600

def mark_alerted(symbol, history):
    history[symbol] = datetime.now().isoformat()
    cutoff = datetime.now() - timedelta(hours=DEDUP_HOURS * 2)
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

# ============ 核心扫描: OI + 费率转负 ============
def scan_funding_reversal():
    """
    扫描资金费率由正转负 + OI放大的币种
    返回信号列表
    """
    ts_start = time.time()
    
    # 1. 获取所有永续合约
    try:
        info = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=10).json()
        symbols = [s['symbol'] for s in info['symbols'] 
                   if s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    except Exception as e:
        print(f"[ERROR] exchangeInfo: {e}")
        return []
    
    # 2. 批量获取24h行情
    try:
        tickers = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10).json()
        ticker_map = {t['symbol']: t for t in tickers}
    except Exception as e:
        print(f"[ERROR] ticker: {e}")
        return []
    
    active = [s for s in symbols if float(ticker_map.get(s, {}).get('quoteVolume', 0)) > MIN_VOLUME_USDT]
    
    # 3. 批量获取当前费率
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
        fr_current = {item['symbol']: float(item['lastFundingRate']) for item in fr_all}
    except:
        fr_current = {}
    
    # 4. 加载上次快照，对比找"刚转负"的
    prev_snapshot = load_fr_snapshot()
    save_fr_snapshot(fr_current)
    
    if not prev_snapshot:
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"[{ts}] 首次运行，保存快照")
        return []
    
    # 找出: 上次>=0, 这次<0 的币
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
    
    # 5. 对刚转负的币查OI历史
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
        except Exception as e:
            # 静默跳过
            continue
    
    elapsed = time.time() - ts_start
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 扫描完成: {len(active)}币/{elapsed:.1f}s, 信号: {len(signals)}")
    
    return signals


# ============ 核心扫描: 热度雷达 ============
def scan_heat_radar():
    """
    热度做多雷达: OI异动 + 24h涨幅 + 成交量排行
    返回热度排行列表
    """
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
    
    # 过滤成交量 > 100万USDT
    active = [(s, float(ticker_map.get(s, {}).get('quoteVolume', 0))) 
              for s in symbols if float(ticker_map.get(s, {}).get('quoteVolume', 0)) > 500_000]
    
    # 获取当前费率
    try:
        fr_all = requests.get('https://fapi.binance.com/fapi/v1/premiumIndex', timeout=10).json()
        fr_map = {item['symbol']: float(item['lastFundingRate']) for item in fr_all}
    except:
        fr_map = {}
    
    # 批量获取OI数据
    hot_list = []
    batch_size = 30
    for i in range(0, len(active), batch_size):
        batch = active[i:i+batch_size]
        for sym, vol in batch:
            try:
                t = ticker_map.get(sym, {})
                price_chg = float(t.get('priceChangePercent', 0))
                price = float(t.get('lastPrice', 0))
                
                # OI变化
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
                
                # 热度分数: 综合OI异动 + 涨幅 + 成交量 + 费率
                heat_score = 0
                if oi_chg_1h > 5: heat_score += 3
                elif oi_chg_1h > 2: heat_score += 1
                if price_chg > 3: heat_score += 2
                elif price_chg > 1: heat_score += 1
                if price_chg < -3: heat_score += 1  # 抄底热度
                if fr < -0.01: heat_score += 2  # 高做空费率=做多有利
                heat_score += min(vol / 10_000_000, 3)  # 成交量加分
                
                hot_list.append({
                    'symbol': sym,
                    'coin': coin,
                    'price': price,
                    'price_chg_24h': price_chg,
                    'volume': vol,
                    'oi_change_1h': oi_chg_1h,
                    'oi_value': oi_value,
                    'funding_rate': fr,
                    'heat_score': heat_score,
                })
            except:
                continue
    
    # 按热度分数排序
    hot_list.sort(key=lambda x: x['heat_score'], reverse=True)
    
    elapsed = time.time() - ts_start
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] 热度扫描完成: {len(active)}币/{elapsed:.1f}s")
    
    return hot_list[:15]  # 返回前15名


# ============ 附加信息 ============
def get_market_caps():
    """获取币安流通市值"""
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
    """获取有现货的币种"""
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


# ============ 格式化推送 ============
def format_funding_alert(signals):
    """格式化费率转负信号"""
    if not signals:
        return None
    
    signals.sort(key=lambda x: (-int(x.get('oi_rising', False)), x['current_fr']))
    mcap_map = get_market_caps()
    spot_set = get_spot_symbols()
    
    now = datetime.now().strftime('%m-%d %H:%M')
    lines = [f"🔥 *费率转负 + OI 扫描*  🔥\n{now}\n"]
    
    for s in signals:
        coin = s['symbol'].replace('USDT', '')
        fr_change = f"{s['prev_fr']:+.4%} → {s['current_fr']:+.4%}"
        mcap = mcap_map.get(coin, 0)
        has_spot = coin in spot_set
        
        lines.append("```")
        lines.append(f"#{coin}")
        lines.append(f"  价格: ${s['price']:.4f}  24h: {s['price_chg_24h']:+.1f}%")
        lines.append(f"  费率: {fr_change}")
        if s['oi_segments']:
            oi_segs = ' > '.join([f"{v/1e6:.1f}M" for v in s['oi_segments']])
            lines.append(f"  OI: +{s['oi_change']:.1f}%  ({oi_segs})")
        lines.append(f"  成交额: ${s['volume']/1e6:.1f}M")
        lines.append(f"  市值: {format_mcap(mcap) if mcap else '未知'}  现货: {'有' if has_spot else '仅合约'}")
        lines.append("```")
    
    return '\n'.join(lines)

def format_heat_alert(hot_list):
    """格式化热度排行"""
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


# ============ 单次运行(供定时任务调用) ============
def run_once():
    """执行一次完整扫描"""
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
    
    # 2. 热度雷达扫描 (每小时跑一次)
    current_min = datetime.now().minute
    if current_min % 15 < 5:  # 每小时的 00-04, 15-19, 30-34, 45-49 分钟运行
        print("\n📊 扫描热度雷达...")
        hot_list = scan_heat_radar()
        if hot_list:
            msg = format_heat_alert(hot_list)
            if msg:
                send_tg(msg)
                print(f"  ✅ 推送热度排行 TOP8")
    else:
        print("\n📊 热度雷达本次跳过")
    
    print(f"\n✅ 完成\n")


# ============ 主入口 ============
if __name__ == '__main__':
    import schedule
    
    print("🚀 Crypto Radar 启动中...")
    print(f"TG Bot: {'已配置' if TG_BOT_TOKEN else '未配置'}")
    print(f"扫描间隔: {SCAN_INTERVAL}秒")
    print(f"{'='*50}")
    
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
