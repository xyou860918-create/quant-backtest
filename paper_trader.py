import argparse
import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta

def main():
    parser = argparse.ArgumentParser(description="Donchian + Trailing Stop Paper Trader")
    parser.add_argument("--symbols", nargs="+", default=["TSLL", "SOXL", "TQQQ"], help="Symbols to trade")
    parser.add_argument("--entry-period", type=int, default=20, help="Donchian entry period")
    parser.add_argument("--exit-period", type=int, default=10, help="Donchian exit period")
    parser.add_argument("--trailing-stop-pct", type=float, default=0.15, help="Trailing stop percentage")
    parser.add_argument("--initial-cash", type=float, default=10000.0, help="Initial cash")
    parser.add_argument("--fee-bps", type=float, default=10.0, help="Fee in basis points (0.1%)")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage in basis points")
    
    args = parser.parse_args()
    
    print("🚀 啟動【海龜通道(20/10) + 15% 移動停利】雲端模擬交易引擎...")
    print(f"📊 追蹤槓桿標的: {args.symbols}")
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    
    data = yf.download(args.symbols, start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"), progress=False)
    
    if data.empty:
        print("❌ 錯誤: 無法抓取歷史行情資料！")
        return

    log_dir = os.path.join("paper_trading", "logs", "paper_trading")
    os.makedirs(log_dir, exist_ok=True)
    
    close = data['Close']
    high = data['High'] if 'High' in data else close
    low = data['Low'] if 'Low' in data else close

    if isinstance(close, pd.Series):
        close = close.to_frame()
        high = high.to_frame()
        low = low.to_frame()

    total_cost_rate = (args.fee_bps + args.slippage_bps) / 10000.0
    telegram_msgs = []

    for symbol in args.symbols:
        if symbol not in close.columns:
            continue
        
        prices = close[symbol].dropna()
        highs = high[symbol].dropna() if symbol in high.columns else prices
        lows = low[symbol].dropna() if symbol in low.columns else prices
        
        df_sym = pd.DataFrame({'price': prices, 'high': highs, 'low': lows}).dropna()
        
        # 海龜通道 (前 20 日高點 / 前 10 日低點)
        df_sym['upper_bound'] = df_sym['high'].shift(1).rolling(window=args.entry_period).max()
        df_sym['lower_bound'] = df_sym['low'].shift(1).rolling(window=args.exit_period).min()
        
        df_sym = df_sym.dropna().copy()
        
        position = 0
        highest_price = 0.0
        positions = []
        
        for idx in range(len(df_sym)):
            price = df_sym.iloc[idx]['price']
            upper = df_sym.iloc[idx]['upper_bound']
            lower = df_sym.iloc[idx]['lower_bound']
            
            if position == 0:
                if price > upper:
                    position = 1
                    highest_price = price
            elif position == 1:
                highest_price = max(highest_price, price)
                stop_price = highest_price * (1 - args.trailing_stop_pct)
                if (price < stop_price) or (price < lower):
                    position = 0
            
            positions.append(position)
        
        # 訊號次日執行 (防止 Lookahead Bias)
        df_sym['position'] = pd.Series(positions, index=df_sym.index).shift(1).fillna(0)
        df_sym['market_return'] = df_sym['price'].pct_change().fillna(0)
        df_sym['trades'] = df_sym['position'].diff().abs().fillna(0)
        df_sym['strategy_return'] = (df_sym['position'] * df_sym['market_return']) - (df_sym['trades'] * total_cost_rate)
        
        df_sym['equity'] = args.initial_cash * (1 + df_sym['strategy_return']).cumprod()
        
        # 儲存對帳單 Log
        eq_filename = os.path.join(log_dir, f"equity_{symbol}.csv")
        df_sym.to_csv(eq_filename)
        
        last_rec = df_sym.iloc[-1]
        pos_status = "🟢 多頭持倉" if last_rec['position'] == 1 else "⚪ 空倉觀望"
        
        # 偵測本日訊號轉折點
        signal_alert = ""
        if len(df_sym) >= 2:
            prev_pos = df_sym.iloc[-2]['position']
            curr_pos = last_rec['position']
            if prev_pos == 0 and curr_pos == 1:
                signal_alert = "\n🔥 【訊號變更：觸發買進進場！】"
            elif prev_pos == 1 and curr_pos == 0:
                signal_alert = "\n🛑 【訊號變更：觸發 15% 停利/出場！】"
                
        msg = f"📌 *{symbol}*\n• 狀態：{pos_status}{signal_alert}\n• 最新收盤價：${last_rec['price']:.2f}\n• 當前策略權益：${last_rec['equity']:,.2f}"
        telegram_msgs.append(msg)
        print(f"✅ {symbol} 模擬交易計算完成：最新權益 ${last_rec['equity']:,.2f} ({pos_status})")

    # 發送 Telegram 通報
    tg_token = os.environ.get("TELEGRAM_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if tg_token and tg_chat_id:
        today_str = datetime.now().strftime("%Y-%m-%d")
        header = f"🤖 *Quant Paper Trading 每日戰報 ({today_str})*\n策略：海龜通道 (20/10) + 15% 移動停利\n" + "—"*22 + "\n\n"
        full_msg = header + "\n\n".join(telegram_msgs)
        
        url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
        requests.post(url, json={"chat_id": tg_chat_id, "text": full_msg, "parse_mode": "Markdown"})
        print("📲 Telegram 每日戰報已發送！")
    else:
        print("⚠️ 未偵測到 Telegram 金鑰，跳過訊息推播。")

if __name__ == "__main__":
    main()
