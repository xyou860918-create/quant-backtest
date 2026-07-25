import argparse
import os
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

def main():
    parser = argparse.ArgumentParser(description="Paper Trader for Quant Backtest")
    parser.add_argument("--symbols", nargs="+", default=["SOXX", "QQQ", "SPY"], help="Symbols to trade")
    parser.add_argument("--donchian-strategy", action="store_true", help="Enable Donchian strategy")
    parser.add_argument("--entry-period", type=int, default=20, help="Donchian entry period")
    parser.add_argument("--exit-period", type=int, default=10, help="Donchian exit period")
    parser.add_argument("--initial-cash", type=float, default=10000.0, help="Initial cash")
    parser.add_argument("--fee-bps", type=float, default=5.0, help="Fee in basis points")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Slippage in basis points")
    
    args = parser.parse_args()
    
    print("🚀 啟動模擬交易引擎 Paper Trader...")
    print(f"📊 回測標的: {args.symbols}")
    print(f"📈 策略: 唐奇安通道 (突破週期 {args.entry_period} / 跌破週期 {args.exit_period})")
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    
    # 抓取歷史與即時價格
    data = yf.download(args.symbols, start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"))
    
    if data.empty:
        print("❌ 錯誤: 無法抓取歷史行情資料！")
        return

    log_dir = os.path.join("paper_trading", "logs", "paper_trading")
    os.makedirs(log_dir, exist_ok=True)
    
    close = data['Close']
    if isinstance(close, pd.Series):
        close = close.to_frame()
        
    fee_rate = args.fee_bps / 10000.0
    slippage_rate = args.slippage_bps / 10000.0
    
    for symbol in args.symbols:
        if symbol not in close.columns:
            continue
            
        prices = close[symbol].dropna()
        highs = data['High'][symbol].dropna() if 'High' in data else prices
        lows = data['Low'][symbol].dropna() if 'Low' in data else prices
        
        upper = highs.shift(1).rolling(window=args.entry_period).max()
        lower = lows.shift(1).rolling(window=args.exit_period).min()
        
        position = 0
        cash = args.initial_cash
        shares = 0
        equity_curve = []
        
        for date in prices.index:
            price = prices.loc[date]
            u_bound = upper.loc[date] if date in upper.index else np.nan
            l_bound = lower.loc[date] if date in lower.index else np.nan
            
            if not np.isnan(u_bound) and price > u_bound and position == 0:
                buy_price = price * (1 + slippage_rate)
                shares = (cash * (1 - fee_rate)) / buy_price
                cash = 0.0
                position = 1
            elif not np.isnan(l_bound) and price < l_bound and position == 1:
                sell_price = price * (1 - slippage_rate)
                cash = shares * sell_price * (1 - fee_rate)
                shares = 0
                position = 0
                
            curr_equity = cash if position == 0 else shares * price
            equity_curve.append({
                "Date": date.strftime("%Y-%m-%d"),
                "Symbol": symbol,
                "Equity": round(curr_equity, 2),
                "Position": position,
                "Price": round(price, 2)
            })
            
        df_eq = pd.DataFrame(equity_curve)
        if not df_eq.empty:
            eq_filename = os.path.join(log_dir, f"equity_{symbol}.csv")
            df_eq.to_csv(eq_filename, index=False)
            last_rec = df_eq.iloc[-1]
            pos_str = "持倉中 (LONG)" if last_rec['Position'] == 1 else "空倉 (CASH)"
            print(f"✅ {symbol} 對帳單已更新: 最新淨值 ${last_rec['Equity']:,.2f} | 狀態: {pos_str}")
            
    print("🎉 模擬交易計算完成！")

if __name__ == "__main__":
    main()
