import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

def run_fixed_backtest():
    print("=" * 60)
    print("🚀 啟動【Hormuz 修正版 2 因子】(硬停損 -8% + 趨勢過濾 + 平滑 Beta)")
    
    tickers = ["XLE", "JETS", "BZ=F", "HO=F", "SGOV"]
    df_raw = yf.download(tickers, start="2018-01-01", end=datetime.now().strftime("%Y-%m-%d"), progress=False)['Close']
    df = df_raw.ffill().dropna(subset=["XLE", "JETS", "BZ=F", "HO=F"])
    
    # 1. 計算因子
    df['Crack'] = (df['HO=F'] * 42) - df['BZ=F']
    df['Crack_MA60'] = df['Crack'].rolling(60).mean()
    df['Factor4'] = np.where(df['Crack'] > 1.2 * df['Crack_MA60'], 1, 0)
    
    df['Brent_MA5'] = df['BZ=F'].rolling(5).mean()
    df['Factor5'] = np.where((df['Brent_MA5'] > 95) & (df['Brent_MA5'].diff() > 0), 1, 0)
    
    # 趨勢過濾: XLE MA5 > MA20
    df['XLE_MA5'] = df['XLE'].rolling(5).mean()
    df['XLE_MA20'] = df['XLE'].rolling(20).mean()
    df['Trend_OK'] = np.where(df['XLE_MA5'] > df['XLE_MA20'], 1, 0)
    
    # 平滑版 Beta (限制在 0.5 ~ 1.2 之間，再取 10 日均線避免跳動)
    ret_xle = df['XLE'].pct_change()
    ret_jets = df['JETS'].pct_change()
    raw_beta = ret_xle.rolling(60).cov(ret_jets) / ret_jets.rolling(60).var()
    df['Beta'] = raw_beta.clip(0.5, 1.2).rolling(10).mean().fillna(0.75)
    
    # 2. 逐日迴圈跑回測 (解決路徑依賴與停損 Bug)
    init_cash = 1000000.0  # 總資金 1M
    cash = init_cash
    position = 0
    
    entry_xle = 0
    entry_jets = 0
    current_beta = 0
    
    equities = []
    trades = 0
    stop_losses = 0
    
    for i in range(len(df)):
        date = df.index[i]
        
        xle_price = df['XLE'].iloc[i]
        jets_price = df['JETS'].iloc[i]
        f4 = df['Factor4'].iloc[i]
        f5 = df['Factor5'].iloc[i]
        trend = df['Trend_OK'].iloc[i]
        beta = df['Beta'].iloc[i]
        weekday = date.dayofweek
        
        if position == 1:
            # 計算目前持倉未實現報酬 (每邊各分配 50% 資金)
            xle_ret = (xle_price / entry_xle) - 1
            jets_ret = (jets_price / entry_jets) - 1
            trade_ret = 0.5 * xle_ret - (0.5 * current_beta * jets_ret)
            
            # 檢查 -8% 硬停損
            if trade_ret <= -0.08:
                position = 0
                cash = cash * (1 + trade_ret - 0.002) # 扣 0.2% 來回手續費與滑點
                trades += 1
                stop_losses += 1
            # 週五進行例行訊號檢查
            elif weekday == 4:
                if (f4 + f5 == 0) or (trend == 0):
                    position = 0
                    cash = cash * (1 + trade_ret - 0.002)
                    trades += 1
        else:
            # 空倉狀態：資金停泊 SGOV
            if i > 0 and 'SGOV' in df.columns and not pd.isna(df['SGOV'].iloc[i]):
                sgov_ret = (df['SGOV'].iloc[i] / df['SGOV'].iloc[i-1]) - 1
                cash = cash * (1 + sgov_ret)
                
            # 週五檢查進場訊號
            if weekday == 4:
                if (f4 + f5 >= 1) and (trend == 1):
                    position = 1
                    entry_xle = xle_price
                    entry_jets = jets_price
                    current_beta = beta
        
        # 記錄每日權益 (持倉時計算浮動淨值，空倉時為 cash)
        if position == 1:
            xle_ret = (xle_price / entry_xle) - 1
            jets_ret = (jets_price / entry_jets) - 1
            trade_ret = 0.5 * xle_ret - (0.5 * current_beta * jets_ret)
            curr_eq = cash * (1 + trade_ret)
        else:
            curr_eq = cash
            
        equities.append(curr_eq)
        
    df['Equity'] = equities
    
    # 3. 計算最終績效
    final_eq = df['Equity'].iloc[-1]
    total_ret = (final_eq / init_cash - 1) * 100
    total_days = len(df)
    cagr = ((final_eq / init_cash) ** (252 / total_days) - 1) * 100
    
    daily_ret = df['Equity'].pct_change().fillna(0)
    sharpe = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252) if daily_ret.std() > 0 else 0
    
    cummax = df['Equity'].cummax()
    max_dd = abs(((df['Equity'] - cummax) / cummax).min()) * 100
    
    print("\n" + "=" * 50)
    print("📊 修正版 2 因子對沖策略 (固定部位 + -8%硬停損)")
    print("=" * 50)
    print(f"💰 初始資金    : $1,000,000.00")
    print(f"💵 最終資金    : ${final_eq:,.2f}")
    print(f"📈 總報酬率    : {total_ret:.2f}%")
    print(f"🚀 年化(CAGR)  : {cagr:.2f}%")
    print(f"🏆 夏普比率    : {sharpe:.2f}")
    print(f"🛡️ 最大回撤    : {max_dd:.2f}%")
    print(f"🔄 總交易次數  : {trades} 次 (觸發硬停損: {stop_losses} 次)")
    print("=" * 50)

if __name__ == "__main__":
    run_fixed_backtest()
