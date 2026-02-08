import sys
import os
import re
import pandas as pd
import plotext as plt
from datetime import datetime

# --- SETTINGS ---
INITIAL_BALANCE = 10.0 

def parse_logs(file_paths):
    trades = []
    # Universal Regex Pattern
    pattern = re.compile(
        r"(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
        r"(?:PAIR: )?(?P<pair>\w+).*"
        r"RESULT: (?P<result>WIN|LOSS) \| "
        r"PNL: \$(?P<pnl_usd>[+-]?\d+\.\d+)"
    )
    for path in file_paths:
        if not os.path.exists(path): continue
        with open(path, 'r') as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    data = match.groupdict()
                    # UPDATED: Now storing the date string as well
                    trades.append({
                        'date': data['date'], 
                        'pnl': float(data['pnl_usd']), 
                        'res': data['result']
                    })
    return pd.DataFrame(trades) if trades else None

def get_stepped_data(data):
    """ Creates the Square Wave effect for high contrast """
    y_values = list(data)
    xs, ys = [], []
    for i in range(len(y_values)):
        xs.append(i); ys.append(y_values[i])
        if i < len(y_values) - 1:
            xs.append(i + 1); ys.append(y_values[i])
    return xs, ys

def show_terminal_graph(df, fname):
    tw, th = os.get_terminal_size()
    
    # 1. Data Processing
    df['cum_pnl'] = df['pnl'].cumsum()
    df['win_count'] = (df['res'] == 'WIN').astype(int).cumsum()
    df['accuracy'] = (df['win_count'] / (df.index + 1)) * 100
    
    pnl_x, pnl_y = get_stepped_data(df['cum_pnl'])
    acc_x, acc_y = get_stepped_data(df['accuracy'])

    # 2. Grid Setup
    total_trades = len(df)
    x_ticks = list(range(0, total_trades + 1, 5))
    x_labels = [str(i) if i % 100 == 0 else "" for i in x_ticks]
    
    y_ticks_acc = list(range(0, 101, 10))
    y_labels_acc = [str(i) for i in y_ticks_acc]

    # 3. Height Calculation
    ind_h = (th - 20) // 2  # Adjusted for larger report

    # --- TOP GRAPH: EQUITY ---
    plt.clf()
    plt.theme('dark')
    plt.canvas_color("black")
    plt.axes_color("black")
    plt.plotsize(tw, ind_h)
    plt.plot(pnl_x, pnl_y, color="bright-cyan", label="PnL ($)", marker="braille")
    plt.xticks(x_ticks, x_labels)
    plt.title(f"📈 EQUITY CURVE | File: {fname}")
    plt.grid(True)
    plt.show()

    # --- BOTTOM GRAPH: ACCURACY ---
    plt.clf()
    plt.theme('dark')
    plt.canvas_color("black")
    plt.axes_color("black")
    plt.plotsize(tw, ind_h)
    plt.plot(acc_x, acc_y, color="bright-yellow", label="Acc (%)", marker="braille")
    plt.hline(70, color="red") 
    plt.xticks(x_ticks, x_labels)
    plt.yticks(y_ticks_acc, y_labels_acc)
    plt.title(f"🎯 ACCURACY STABILITY | File: {fname}")
    plt.ylim(0, 100)
    plt.grid(True)
    plt.show()

def print_daily_breakdown(df):
    """Generates a Zerodha-style Day-by-Day report"""
    # Convert date column to datetime objects
    df['dt'] = pd.to_datetime(df['date'])
    df['day_str'] = df['dt'].dt.strftime('%Y-%m-%d')
    
    # Group by Day
    daily_groups = df.groupby('day_str')
    
    tw, _ = os.get_terminal_size()
    print("━" * tw)
    print(f"  \033[1m📅 DAILY BREAKDOWN (Zerodha Style)\033[0m")
    print(f"  {'DATE':<12} | {'TRADES':<6} | {'WINS':<4} | {'DAILY PNL':<12} | {'ROI %':<8} | {'VISUAL'}")
    print("─" * tw)

    current_balance = INITIAL_BALANCE
    
    for day, day_df in daily_groups:
        day_pnl = day_df['pnl'].sum()
        trades_count = len(day_df)
        wins = len(day_df[day_df['res'] == 'WIN'])
        
        # Calculate ROI based on Opening Balance of that day
        roi_pct = (day_pnl / current_balance) * 100 if current_balance > 0 else 0.0
        
        # Colors
        c_pnl = "\033[92m" if day_pnl >= 0 else "\033[91m"
        c_reset = "\033[0m"
        
        # Visual Bar
        bar_len = min(int(abs(roi_pct)), 10) # Cap visual bar at 10 blocks
        bar_char = "█" * bar_len
        bar_visual = f"{c_pnl}{bar_char}{c_reset}"

        print(f"  {day:<12} | {trades_count:<6} | {wins:<4} | {c_pnl}${day_pnl:+.2f}{c_reset:<12} | {c_pnl}{roi_pct:+.2f}%{c_reset:<8} | {bar_visual}")
        
        # Update balance for next day compounding calculation
        current_balance += day_pnl

    print("━" * tw)

def print_deep_report(df, fname):
    results = df['res'].tolist()
    max_w, max_l, cur_w, cur_l = 0, 0, 0, 0
    for r in results:
        if r == 'WIN':
            cur_w += 1; cur_l = 0
            max_w = max(max_w, cur_w)
        else:
            cur_l += 1; cur_w = 0
            max_l = max(max_l, cur_l)
            
    df['cum_pnl'] = df['pnl'].cumsum()
    df['accuracy'] = ((df['res'] == 'WIN').astype(int).cumsum() / (df.index + 1)) * 100
    df['peak_pnl'] = df['cum_pnl'].cummax()
    max_dd_usd = (df['peak_pnl'] - df['cum_pnl']).max()
    min_acc = df['accuracy'].min()

    final_pnl = df['cum_pnl'].iloc[-1]
    pnl_pct = (final_pnl / INITIAL_BALANCE) * 100
    final_acc = df['accuracy'].iloc[-1]
    wins, losses = len(df[df['res']=='WIN']), len(df[df['res']=='LOSS'])
    tw, _ = os.get_terminal_size()

    c_pnl = "\033[1;92m" if final_pnl >= 0 else "\033[1;91m"
    c_acc = "\033[1;93m" if final_acc >= 70 else "\033[1;91m"

    print("━" * tw)
    print(f"  \033[1m📊 PERFORMANCE REPORT [{fname}]: {len(df)} TRADES\033[0m")
    print(f"  💰 TOTAL PNL : {c_pnl}${final_pnl:.4f} ({pnl_pct:+.2f}%)\033[0m")
    print(f"  🎯 ACCURACY  : {c_acc}{final_acc:.1f}%\033[0m (Goal: 70%)")
    print(f"  ✅ WINS: {wins} | ❌ LOSSES: {losses}")
    print("─" * tw)
    print(f"  🔥 MAX WIN STREAK : \033[92m{max_w}\033[0m | MAX LOSS STREAK: \033[91m{max_l}\033[0m")
    print(f"  📉 MAX DRAWDOWN   : \033[91m${max_dd_usd:.4f}\033[0m | LOWEST ACCURACY: \033[91m{min_acc:.1f}%\033[0m")
    print("━" * tw)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python stat.py log.txt")
    else:
        fn = os.path.basename(sys.argv[1])
        data = parse_logs(sys.argv[1:])
        if data is not None:
            show_terminal_graph(data, fn)
            print_daily_breakdown(data) # <--- Added New Function Call Here
            print_deep_report(data, fn)
        else:
            print("\033[91mError: No valid trade data found.\033[0m")
