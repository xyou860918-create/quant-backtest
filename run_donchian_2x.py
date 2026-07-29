import yfinance as yf
import pandas as pd
import numpy as np

def run_donchian_backtest(symbols=["USD", "QLD"]):
    print("="*60)
    print("🚀 啟動【唐奇安通道(20/10) + 15%移動停利】2倍槓桿 ETF 回測")
    print("="*60)
    
    data = yf.download(symbols, start="2023-01-01", progress=False)
    close = data['Close']
    high = data['High'] if 'High' in data else close
    low = data['Low'] if 'Low' in data else close
    
    results = []
    
    for symbol in symbols:
        df = pd.DataFrame({
            'price': close[symbol],
            'high': high[symbol],
            'low': low[symbol]
        }).dropna()
        
        # Donchian Channels (20日高, 10日低)
        df['upper'] = df['high'].shift(1).rolling(20).max()
        df['lower'] = df['low'].shift(1).rolling(10).min()
        df = df.dropna().copy()
        
        position = 0
        peak_price = 0.0
        positions = []
        trailing_stops = 0
        
        for i in range(len(df)):
            p = df['price'].iloc[i]
            u = df['upper'].iloc[i]
            l = df['lower'].iloc[i]
            
            if position == 0:
                if p > u:
                    position = 1
                    peak_price = p
            elif position == 1:
                peak_price = max(peak_price, p)
                stop_loss = peak_price * 0.85 # 15% 高點拉回停利
                if p < stop_loss:
                    position = 0
                    trailing_stops += 1
                elif p < l:
                    position = 0
                    
            positions.append(position)
            
        df['position'] = pd.Series(positions, index=df.index).shift(1).fillna(0)
        df['ret'] = df['price'].pct_change().fillna(0)
        df['trades'] = df['position'].diff().abs().fillna(0)
        
        # 策略報酬 (扣除 0.1% 單邊手續費)
        df['strat_ret'] = (df['position'] * df['ret']) - (df['trades'] * 0.001)
        
        # 績效計算
        eq = 10000 * (1 + df['strat_ret']).cumprod()
        bh_eq = 10000 * (1 + df['ret']).cumprod()
        
        total_ret = (eq.iloc[-1] / 10000 - 1) * 100
        bh_ret = (bh_eq.iloc[-1] / 10000 - 1) * 100
        
        cagr = ((eq.iloc[-1] / 10000) ** (252/len(df)) - 1) * 100
        sharpe = (df['strat_ret'].mean() / df['strat_ret'].std()) * np.sqrt(252) if df['strat_ret'].std() != 0 else 0
        
        cummax = eq.cummax()
        max_dd = abs(((eq - cummax) / cummax).min()) * 100
        
        results.append({
            'Symbol': symbol,
            'Strat_Ret': f"{total_ret:.1f}%",
            'BH_Ret': f"{bh_ret:.1f}%",
            'CAGR': f"{cagr:.1f}%",
            'Sharpe': f"{sharpe:.2f}",
            'Max_DD': f"{max_dd:.1f}%",
            'Trades': int(df['trades'].sum()),
            'Trailing_Hits': trailing_stops
        })
        
    return pd.DataFrame(results)

df_res = run_donchian_backtest()
print(df_res.to_string(index=False))
