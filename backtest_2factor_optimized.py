import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime

def run_optimized_backtest():
    print("=" * 60)
    print("🚀 啟動【Hormuz 優化版 2 因子策略】(Factor 4 放寬 + 動態 Beta + SGOV 停泊)")
    
    # 1. 抓取所需數據 (XLE, JETS, BZ=F 布蘭特, HO=F 燃油, SGOV 閒置資金停泊)
    tickers = ["XLE", "JETS", "BZ=F", "HO=F", "SGOV"]
    print("📥 正在下載歷史數據...")
    df_raw = yf.download(tickers, start="2018-01-01", end=datetime.now().strftime("%Y-%m-%d"), progress=False)['Close']
    df = df_raw.ffill().dropna(subset=["XLE", "JETS", "BZ=F", "HO=F"])
    
    # 2. 計算 2 大核心因子
    # 燃油期貨(HO=F)報價為「每加侖」，原油(BZ=F)為「每桶」(1桶=42加侖)
    df['Crack_Spread'] = (df['HO=F'] * 42) - df['BZ=F']
    df['Crack_MA60'] = df['Crack_Spread'].rolling(window=60).mean()
    
    # Factor 4: 裂解價差大於 60 日均值的 1.2 倍 (放寬閾值)
    df['Factor4'] = np.where(df['Crack_Spread'] > (1.2 * df['Crack_MA60']), 1, 0)
    
    # Factor 5: 布蘭特原油 MA5 > 95 且斜率向上
    df['Brent_MA5'] = df['BZ=F'].rolling(window=5).mean()
    df['Brent_Slope'] = df['Brent_MA5'].diff()
    df['Factor5'] = np.where((df['Brent_MA5'] > 95) & (df['Brent_Slope'] > 0), 1, 0)
    
    # 3. 計算動態 Beta (XLE vs JETS 滾動 60 日)
    ret_xle = df['XLE'].pct_change()
    ret_jets = df['JETS'].pct_change()
    cov = ret_xle.rolling(60).cov(ret_jets)
    var = ret_jets.rolling(60).var()
    df['Dynamic_Beta'] = (cov / var).fillna(0.75) # 預設給 0.75
    
    # 4. 產生交易訊號 (週頻重採樣邏輯)
    df['Total_Score'] = df['Factor4'] + df['Factor5']
    # 只要有 1 個因子觸發即視為地緣風險升溫
    df['Raw_Signal'] = np.where(df['Total_Score'] >= 1, 1, 0) 
    
    # 將每日訊號轉為每週五確認 (防 Lookahead Bias)
    df['Weekday'] = df.index.dayofweek
    # 記錄每週五的訊號，其餘日子為 NaN，然後往下填補
    df['Weekly_Signal'] = np.where(df['Weekday'] == 4, df['Raw_Signal'], np.nan)
    df['Weekly_Signal'] = df['Weekly_Signal'].ffill().fillna(0)
    
    # 延遲一天執行 (下週一開盤)
    df['Position'] = df['Weekly_Signal'].shift(1).fillna(0)
    df['Hedge_Ratio'] = df['Dynamic_Beta'].shift(1).fillna(0.75)
    
    # 5. 計算 SGOV 停泊報酬率 (若 SGOV 尚未上市則為 0)
    df['SGOV_Ret'] = df['SGOV'].pct_change().fillna(0) if 'SGOV' in df.columns else 0
    
    # 6. 計算策略報酬
    # 處於 Position == 1 時: 做多 XLE，做空 (Hedge_Ratio * JETS)
    # 處於 Position == 0 時: 資金停泊在 SGOV 賺取利息
    df['Strat_Ret'] = np.where(
        df['Position'] == 1,
        ret_xle - (df['Hedge_Ratio'] * ret_jets),
        df['SGOV_Ret']
    )
    
    # 扣除換倉手續費 (假設 0.1%)
    fee = 0.001
    df['Trades'] = df['Position'].diff().abs().fillna(0)
    df['Strat_Ret'] -= df['Trades'] * fee
    
    # 7. 計算績效
    df['Equity'] = 10000 * (1 + df['Strat_Ret']).cumprod()
    df['XLE_Hold'] = 10000 * (1 + ret_xle).cumprod()
    
    total_days = len(df)
    total_ret = (df['Equity'].iloc[-1] / 10000 - 1) * 100
    cagr = ((df['Equity'].iloc[-1] / 10000) ** (252 / total_days) - 1) * 100
    xle_cagr = ((df['XLE_Hold'].iloc[-1] / 10000) ** (252 / total_days) - 1) * 100
    
    daily_ret = df['Strat_Ret']
    sharpe = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252) if daily_ret.std() > 0 else 0
    
    cummax = df['Equity'].cummax()
    drawdown = (df['Equity'] - cummax) / cummax
    max_dd = abs(drawdown.min()) * 100
    
    win_months = df['Strat_Ret'].resample('ME').sum() > 0
    win_rate = (win_months.sum() / len(win_months)) * 100
    
    print("\n" + "=" * 50)
    print("📊 簡化版 2 因子策略 (週頻調倉 + 動態對沖) 回測結果")
    print("=" * 50)
    print(f"📈 策略總報酬率 : {total_ret:.2f}%")
    print(f"🚀 策略年化(CAGR): {cagr:.2f}%")
    print(f"⚖️ XLE 基準年化  : {xle_cagr:.2f}%")
    print(f"🏆 夏普比率      : {sharpe:.2f}")
    print(f"🛡️ 最大回撤      : {max_dd:.2f}%")
    print(f"🎯 月度勝率      : {win_rate:.1f}%")
    print(f"🔄 總進出場次數  : {int(df['Trades'].sum())} 次")
    print("=" * 50)

if __name__ == "__main__":
    run_optimized_backtest()
