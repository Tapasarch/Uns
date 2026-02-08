import asyncio
import json
import os
import sys
import pandas as pd
import numpy as np
import aiohttp
import websockets
from datetime import datetime

# --- SETTINGS ---
PAIRS = ["btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "adausdt", 
         "dogeusdt", "pepeusdt", "dotusdt", "linkusdt", "trxusdt", "ltcusdt"]

# Dynamic Filenames based on script name
SCRIPT_NAME = os.path.basename(__file__).replace('.py', '')
LOG_FILE = os.path.join(os.getcwd(), f"{SCRIPT_NAME}.txt")
STATE_FILE = os.path.join(os.getcwd(), f"{SCRIPT_NAME}.json")

INITIAL_BALANCE = 10.0    
TRADE_AMOUNT_USD = 1.0    
MAX_CONCURRENT_TRADES = 12 
RR_RATIO = 2.0 

# Shared State
active_trades = {}
market_data = {pair: pd.DataFrame() for pair in PAIRS}

# Speed Cache for Stats
stats_cache = {"total": 0, "wins": 0, "losses": 0, "acc": 0.0, "pnl_u": 0.0, "pnl_p": 0.0, "bal": INITIAL_BALANCE}

# --- PERSISTENCE (SAVE/LOAD STATE) ---
def save_state():
    """Saves active trades to JSON so they survive restarts"""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(active_trades, f)
    except Exception as e:
        pass

def load_state():
    """Loads active trades on startup"""
    global active_trades
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                active_trades = json.load(f)
        except Exception:
            active_trades = {}

# --- STATS ENGINE ---
async def get_stats(force_refresh=False):
    global stats_cache
    
    # If using cache
    if not force_refresh and stats_cache["total"] > 0:
        # Recalculate floating balance only
        stats_cache["bal"] = INITIAL_BALANCE + stats_cache["pnl_u"]
        return stats_cache["total"], stats_cache["wins"], stats_cache["losses"], stats_cache["acc"], stats_cache["pnl_u"], stats_cache["pnl_p"], stats_cache["bal"]

    if not os.path.exists(LOG_FILE): 
        return 0, 0, 0, 0.0, 0.0, 0.0, INITIAL_BALANCE
    
    try:
        with open(LOG_FILE, "r") as f: 
            lines = [l for l in f.readlines() if "RESULT:" in l]
        
        total = len(lines)
        if total == 0: return 0, 0, 0, 0.0, 0.0, 0.0, INITIAL_BALANCE
        
        wins = sum(1 for l in lines if "WIN" in l)
        losses = sum(1 for l in lines if "LOSS" in l)
        acc = (wins/total)*100 if total > 0 else 0.0
        
        # Calculate Real PnL by parsing the file
        current_pnl_usd = 0.0
        for l in lines:
            try:
                # Parse "PNL: $±0.0000"
                part = l.split("PNL: $")[1].split(" ")[0]
                current_pnl_usd += float(part)
            except: pass
            
        pnl_p = (current_pnl_usd / INITIAL_BALANCE) * 100
        # Balance = Initial + Realized PnL (Open positions are part of equity, not balance usually, but here simple math)
        bal = INITIAL_BALANCE + current_pnl_usd 
        
        stats_cache = {"total": total, "wins": wins, "losses": losses, "acc": acc, "pnl_u": current_pnl_usd, "pnl_p": pnl_p, "bal": bal}
        return total, wins, losses, acc, current_pnl_usd, pnl_p, bal
    except: return 0, 0, 0, 0.0, 0.0, 0.0, INITIAL_BALANCE

# --- STRATEGY 2: RSI + BOLLINGER MEAN REVERSION ---
def calculate_indicators(df):
    if len(df) < 50: return None
    df = df.copy()
    close = df['close']
    
    # 1. RSI (14)
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.ewm(com=13, adjust=False).mean()
    ma_down = down.ewm(com=13, adjust=False).mean()
    rs = ma_up / ma_down
    df['rsi'] = 100 - (100 / (1 + rs))

    # 2. Bollinger Bands (20, 2)
    df['sma_20'] = close.rolling(20).mean()
    df['std_20'] = close.rolling(20).std()
    df['upper'] = df['sma_20'] + (df['std_20'] * 2)
    df['lower'] = df['sma_20'] - (df['std_20'] * 2)
    
    # ATR for Stop Loss sizing (still good to have dynamic SL based on volatility)
    tr = np.maximum(df['high'] - df['low'], 
                    np.maximum(abs(df['high'] - close.shift(1)), 
                               abs(df['low'] - close.shift(1))))
    df['atr'] = tr.rolling(14).mean()
    
    return df

async def handle_socket():
    streams = "/".join([f"{p}@kline_1m" for p in PAIRS])
    uri = f"wss://stream.binance.com:9443/stream?streams={streams}"
    
    # Auto-Reconnect Loop
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                print("🟢 WebSocket Connected.")
                while True:
                    res = await websocket.recv()
                    data = json.loads(res)
                    pair = data['stream'].split('@')[0]
                    k = data['data']['k']
                    # Structure data
                    new_row = pd.DataFrame([{'close': float(k['c']), 'high': float(k['h']), 'low': float(k['l']), 'v': float(k['v'])}])
                    
                    # Append and keep size manageable
                    market_data[pair] = pd.concat([market_data[pair], new_row], ignore_index=True).tail(250)
                    
                    await process_logic(pair)
        except Exception as e:
            print(f"🔴 Connection Lost: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

async def process_logic(pair):
    df = market_data[pair]
    if len(df) < 50: return
    
    ind = calculate_indicators(df)
    if ind is None: return
    last = ind.iloc[-1]
    cp = last['close']
    
    # 1. Manage Active Trades (State is persistent)
    if pair in active_trades:
        trade = active_trades[pair]
        entry_price = trade['entry']
        
        # Real PnL Calc
        pnl_pct = ((cp - entry_price) / entry_price) * 100
        
        # LOGIC:
        # A) Hard SL
        # B) Hard TP (1:2)
        # C) Dynamic TP: Strategy 2 says exit if price touches Upper Band
        
        upper_band_hit = (cp >= last['upper'])
        
        if cp >= trade['tp']: 
            await exit_trade(pair, "WIN", pnl_pct, reason="Target Hit")
        elif cp <= trade['sl']: 
            await exit_trade(pair, "LOSS", pnl_pct, reason="Stop Hit")
        elif upper_band_hit and pnl_pct > 0.1: # Only use BB exit if slightly profitable to cover fees
            await exit_trade(pair, "WIN", pnl_pct, reason="Upper Band Hit")
            
        return

    # 2. Check for New Entries
    if len(active_trades) >= MAX_CONCURRENT_TRADES: return
    
    # Balance Check
    _, _, _, _, _, _, current_bal = await get_stats()
    # Logic: Can we afford a trade? (Using roughly $1 margin)
    if current_bal < 1.0: return 

    # --- STRATEGY 2 ENTRY LOGIC ---
    # Buy Only
    # Condition: Close < Lower BB AND RSI < 30
    if last['close'] < last['lower'] and last['rsi'] < 30:
        enter_trade(pair, 'BUY', last)

def enter_trade(pair, side, last):
    entry = last['close']
    
    # Strategy 2 Risk Management
    # Stop Loss = 1.5 ATR (Volatility based) or Fixed % if preferred. 
    # Using ATR ensures we don't get stopped out by noise in high vol pairs.
    atr_sl = last['atr'] * 1.5 
    
    sl = entry - atr_sl
    risk = entry - sl
    tp = entry + (risk * RR_RATIO) # 1:2 RR
    
    active_trades[pair] = {
        'side': side, 
        'entry': entry, 
        'tp': tp, 
        'sl': sl, 
        'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    save_state() # Save immediately

async def exit_trade(pair, result, pnl_pct, reason=""):
    if pair not in active_trades: return
    t = active_trades.pop(pair)
    save_state() # Update state immediately
    
    close_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # REAL PnL CALCULATION
    # (Exit - Entry) * (Amount / Entry)
    entry_p = t['entry']
    amount_bought = TRADE_AMOUNT_USD / entry_p
    
    # Reconstruct exit price based on pct (approx) or use current market price if available
    # For accuracy in logs, we calculate $ based on the % passed in
    pnl_usd = TRADE_AMOUNT_USD * (pnl_pct / 100)
    
    log_entry = (
        f"{close_time} | PAIR: {pair.upper()} | SIDE: {t['side']} | "
        f"ENTRY: {t['entry']:.4f} | TP: {t['tp']:.4f} | SL: {t['sl']:.4f} | "
        f"RESULT: {result} | PNL: ${pnl_usd:+.4f} ({pnl_pct:+.2f}%)\n"
    )
    
    with open(LOG_FILE, "a") as f: 
        f.write(log_entry)
        total, wins, losses, acc, pnl_u, pnl_p, bal = await get_stats(force_refresh=True)
        f.write(f"--- Updated Stats -> Accuracy: {acc:.1f}% | Total PnL: ${pnl_u:.2f} | Balance: ${bal:.2f} ---\n\n")

# --- DASHBOARD ---
async def dashboard():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        total, wins, losses, acc, pnl_usd, pnl_pct, balance = await get_stats()
        
        print("═"*95)
        print(f" 🚀 RSI+BB REVERSION BOT | Accuracy:{acc:.1f}% | PnL:${pnl_usd:.2f}({pnl_pct:+.1f}%) | Active:{len(active_trades)} | Trades:{total} | Win:{wins} | Loss:{losses} | Balance:${balance:.2f}")
        print("═"*95)
        print(f"{'PAIR':<10} | {'PRICE':<10} | {'STATUS':<12} | {'POSITION'}")
        print("-" * 95)
        
        for p in PAIRS:
            price = market_data[p]['close'].iloc[-1] if not market_data[p].empty else 0
            
            if p in active_trades:
                status = "IN TRADE"
                trade = active_trades[p]
                # Live PnL Calculation for Dashboard
                if price > 0:
                    pnl_l_pct = ((price - trade['entry']) / trade['entry']) * 100
                    pnl_l_usd = TRADE_AMOUNT_USD * (pnl_l_pct / 100)
                    pos_pnl = f"$ {pnl_l_usd:+.2f} ({pnl_l_pct:+.2f}%)"
                else:
                    pos_pnl = "Wait Data..."
            else:
                pos_pnl = "-"
                # Check indicator condition for status
                if not market_data[p].empty and len(market_data[p]) > 20:
                    last = calculate_indicators(market_data[p]).iloc[-1]
                    rsi_val = last['rsi']
                    is_oversold = rsi_val < 30
                    status = f"RSI: {rsi_val:.1f}" if not is_oversold else "OVERSOLD"
                else:
                    status = "SCANNING"
                    
            print(f"{p.upper():<10} | {price:<10.2f} | {status:<12} | {pos_pnl}")
        
        if active_trades:
            print("\n🔥 ACTIVE POSITIONS DETAIL")
            for p, t in active_trades.items():
                print(f" {p.upper()} {t['side']} | Entry: {t['entry']:.4f} | TP: {t['tp']:.4f} | SL: {t['sl']:.4f}")
        
        await asyncio.sleep(1)

async def main():
    load_state() # Restore trades on boot
    
    # Pre-fetch history for indicators
    async with aiohttp.ClientSession() as session:
        for p in PAIRS:
            try:
                # Fetch 200 candles to ensure indicators work immediately
                async with session.get(f"https://api.binance.com/api/v3/klines?symbol={p.upper()}&interval=1m&limit=200") as resp:
                    d = await resp.json()
                    df = pd.DataFrame(d, columns=['t','o','h','l','c','v','ct','qv','tr','tb','tq','i'])
                    market_data[p] = df[['c','h','l','v']].rename(columns={'c':'close','h':'high','l':'low','v':'v'}).astype(float)
            except: pass
            
    await get_stats(force_refresh=True)
    await asyncio.gather(handle_socket(), dashboard())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit()
