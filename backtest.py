import sys
import os
import asyncio
import importlib.util
import pandas as pd
import numpy as np
import aiohttp
import re
import time
from datetime import datetime, timedelta

# --- GLOBAL SIMULATION STATE ---
# This variable holds the "Chart Time" current candle
CURRENT_SIMULATION_TIME = datetime.now()

# --- MOCKING DATETIME ---
# We create a fake datetime class that looks and acts like the real one
# but returns our historical simulation time when .now() is called.
class MockDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return CURRENT_SIMULATION_TIME
    
    @classmethod
    def utcnow(cls):
        return CURRENT_SIMULATION_TIME

# --- HELPER FUNCTIONS ---

def extract_timeframe(script_path):
    """Reads the bot script to find the timeframe (e.g., '1m', '5m', '1h')."""
    try:
        with open(script_path, 'r') as f:
            content = f.read()
            match = re.search(r"(?:interval=|kline_)(\d+[mhdw])", content)
            if match:
                return match.group(1)
    except:
        pass
    print("⚠️ Could not detect timeframe in script. Defaulting to '1m'.")
    return '1m'

def date_to_ms(date_str):
    """Converts YYYY-MM-DD string to milliseconds timestamp."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return int(dt.timestamp() * 1000)
    except ValueError:
        print("❌ Invalid Date Format. Please use YYYY-MM-DD (e.g., 2023-01-01)")
        sys.exit(1)

def import_bot_script(script_path):
    """Dynamically imports the trading bot script and forces it to use MockDatetime."""
    if not os.path.exists(script_path):
        print(f"❌ Error: File '{script_path}' not found.")
        sys.exit(1)
        
    module_name = os.path.basename(script_path).replace('.py', '')
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    
    sys.modules[module_name] = module
    
    # 1. Execute the module (This runs the bot's imports, like 'from datetime import datetime')
    spec.loader.exec_module(module)
    
    # 2. OVERWRITE datetime AFTER execution
    # This ensures that when the bot calls datetime.now(), it calls OUR function, not the system's.
    module.datetime = MockDatetime
    
    return module

async def fetch_historical_range(session, pair, interval, start_ms, end_ms):
    """Fetches ALL candles between start_ms and end_ms using pagination."""
    base_url = "https://api.binance.com/api/v3/klines"
    all_data = []
    current_start = start_ms
    
    print(f"   Downloading {pair.upper()}...", end="", flush=True)
    
    while True:
        # Don't fetch beyond end_ms
        if current_start >= end_ms:
            break

        params = {
            'symbol': pair.upper(),
            'interval': interval,
            'startTime': current_start,
            'endTime': end_ms,
            'limit': 1000 
        }
        
        try:
            async with session.get(base_url, params=params) as resp:
                if resp.status != 200:
                    print(f" HTTP Error {resp.status}")
                    break
                    
                data = await resp.json()
                
                if not data or len(data) == 0:
                    break
                
                all_data.extend(data)
                
                # Update start time to the close time of the last candle + 1ms
                last_close_time = data[-1][6]
                current_start = last_close_time + 1
                
                # Visual progress
                if len(all_data) % 5000 == 0:
                    print(".", end="", flush=True)
                
                # Small delay to be nice to API
                await asyncio.sleep(0.05)

        except Exception as e:
            print(f" Connection Error: {e}")
            await asyncio.sleep(1)
            continue

    print(f" Done. ({len(all_data)} candles)")
    
    if not all_data:
        return pd.DataFrame()

    # Convert to DataFrame
    df = pd.DataFrame(all_data, columns=['t','o','h','l','c','v','ct','qv','tr','tb','tq','i'])
    df = df[['t', 'o', 'h', 'l', 'c', 'v']].astype(float)
    df.columns = ['time', 'open', 'high', 'low', 'close', 'v']
    
    # Filter strictly within range (API sometimes returns loose bounds)
    df = df[(df['time'] >= start_ms) & (df['time'] <= end_ms)]
    
    return df

# --- MAIN BACKTEST LOGIC ---

async def run_backtest(script_filename):
    global CURRENT_SIMULATION_TIME
    
    print("="*60)
    print(f"🚀 PROFESSIONAL BACKTESTER FOR: {script_filename}")
    print("="*60)
    
    # 1. User Inputs
    start_date = input("📅 Enter Start Date (YYYY-MM-DD): ").strip()
    end_date = input("📅 Enter End Date   (YYYY-MM-DD): ").strip()
    
    start_ms = date_to_ms(start_date)
    end_ms = date_to_ms(end_date)
    
    # Set end_ms to end of that day
    end_ms = end_ms + (24 * 60 * 60 * 1000) - 1
    
    if end_ms <= start_ms:
        print("❌ End Date must be after Start Date.")
        return

    # 2. Detect Timeframe & Load Bot
    timeframe = extract_timeframe(script_filename)
    print(f"⏱️  Detected Timeframe: {timeframe}")

    try:
        bot = import_bot_script(script_filename)
    except Exception as e:
        print(f"❌ Failed to load bot: {e}")
        return

    # 3. Setup Logging
    # We change the log file name so we don't mess up your real trading logs
    BACKTEST_LOG = f"backtest_{script_filename.replace('.py', '.txt')}"
    if os.path.exists(BACKTEST_LOG): os.remove(BACKTEST_LOG)
    bot.LOG_FILE = BACKTEST_LOG
    
    # Reset bot state
    if hasattr(bot, 'active_trades'): bot.active_trades = {}
    
    # 4. Download Data Loop
    print(f"\n📥 Fetching Data from {start_date} to {end_date}...")
    market_history = {}
    
    async with aiohttp.ClientSession() as session:
        for pair in bot.PAIRS:
            df = await fetch_historical_range(session, pair, timeframe, start_ms, end_ms)
            if not df.empty:
                market_history[pair] = df
            else:
                print(f"⚠️  No data found for {pair}.")

    if not market_history:
        print("❌ No valid market data downloaded. Exiting.")
        return

    # 5. Synchronization & Simulation
    print("\n▶️  Running Strategy Simulation...")
    
    # Find primary pair (the one with most data) to drive the clock
    primary_pair = max(market_history, key=lambda k: len(market_history[k]))
    primary_df = market_history[primary_pair]
    total_candles = len(primary_df)
    
    start_time = time.time()
    
    # Warmup period (e.g., 300 candles for indicators)
    warmup = 300
    if total_candles < warmup:
        print("❌ Not enough data for warmup period.")
        return

    # Iterate through history
    for i in range(warmup, total_candles):
        # Visual Progress Bar
        if i % 500 == 0:
            pct = (i / total_candles) * 100
            sys.stdout.write(f"\r⏳ Progress: {pct:.1f}%")
            sys.stdout.flush()

        # Update Time Travel
        # We use the close time of the current candle for the timestamp
        current_ts = primary_df.iloc[i]['time']
        CURRENT_SIMULATION_TIME = datetime.fromtimestamp(current_ts / 1000.0)

        for pair in bot.PAIRS:
            if pair not in market_history: continue
            df = market_history[pair]
            
            # Simple index sync (assuming contiguous data for speed)
            # In a highly fragmented dataset, you'd use timestamp lookups, 
            # but for Binance historical data, index alignment is usually sufficient and 100x faster.
            if i >= len(df): continue
            
            # Inject Data Window (Past + Current Candle)
            window = df.iloc[i-warmup : i+1].copy()
            bot.market_data[pair] = window
            
            # Execute Bot Logic
            # The bot will now call datetime.now(), which hits our MockDatetime class
            try:
                await bot.process_logic(pair)
            except Exception:
                pass

    elapsed = time.time() - start_time
    print(f"\n\n✅ Simulation finished in {elapsed:.2f} seconds.")
    generate_report(BACKTEST_LOG, bot.INITIAL_BALANCE)

def generate_report(log_file, initial_balance):
    if not os.path.exists(log_file):
        print("❌ No trades generated.")
        return

    print("\n" + "="*60)
    print(f"📊 FINAL BACKTEST REPORT")
    print("="*60)
    
    wins = 0
    losses = 0
    total_pnl = 0.0
    equity_curve = [initial_balance]
    
    with open(log_file, 'r') as f:
        lines = f.readlines()
        
    trade_count = 0
    for line in lines:
        if "RESULT:" in line:
            trade_count += 1
            try:
                parts = line.split("|")
                # Parse PNL line carefully
                pnl_part = [x for x in parts if "PNL" in x][0]
                res_part = [x for x in parts if "RESULT" in x][0]
                
                # Format is usually: PNL: $+0.0400...
                pnl_val_str = pnl_part.split("$")[1].split(" ")[0]
                pnl = float(pnl_val_str)
                
                result = res_part.split(":")[1].strip()
                
                total_pnl += pnl
                equity_curve.append(initial_balance + total_pnl)
                
                if result == "WIN": wins += 1
                elif result == "LOSS": losses += 1
            except: pass

    final_balance = initial_balance + total_pnl
    win_rate = (wins / trade_count * 100) if trade_count > 0 else 0.0
    
    # Calculate Max Drawdown
    peak = -999999999
    max_dd = 0.0
    max_dd_pct = 0.0
    
    for eq in equity_curve:
        if eq > peak: peak = eq
        dd = peak - eq
        if peak > 0:
            dd_pct = (dd / peak) * 100
        else:
            dd_pct = 0
            
        if dd_pct > max_dd_pct: max_dd_pct = dd_pct
        if dd > max_dd: max_dd = dd

    print(f"Total Trades:   {trade_count}")
    print(f"Win Rate:       {win_rate:.2f}%")
    print(f"Wins / Losses:  {wins} / {losses}")
    print("-" * 30)
    print(f"Initial Bal:    ${initial_balance:.2f}")
    print(f"Final Bal:      ${final_balance:.2f}")
    print(f"Net Profit:     ${total_pnl:.2f} ({(total_pnl/initial_balance)*100:.2f}%)")
    print(f"Max Drawdown:   {max_dd_pct:.2f}% (${max_dd:.2f})")
    print("="*60)
    print(f"📂 Full logs: {log_file}")
    print("="*60)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python backtest.py <strategy_file.py>")
    else:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        try:
            asyncio.run(run_backtest(sys.argv[1]))
        except KeyboardInterrupt:
            print("\n⛔ Backtest stopped by user.")
