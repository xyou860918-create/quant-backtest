import yfinance as yf
import pandas as pd
import requests
import os
from datetime import datetime
import warnings

warnings.filterwarnings('ignore')

# 預設帶入你的金鑰
EIA_API_KEY = os.getenv("EIA_API_KEY", "LG2OcZHmhIOBcSOw98z5T8A47ojsxZkO92JOhc0I")

def get_factor1_shipping():
    """Factor 1: 原油船運緊縮 (代理: TNK 油輪股價 vs 50日均線)"""
    print("🚢 正在獲取 原油船運指標 (YF - TNK)...")
    try:
        df = yf.download("TNK", period="100d", progress=False)
        close_col = df['Close']['TNK'] if isinstance(df.columns, pd.MultiIndex) else df['Close']
        data = close_col.ffill()
        
        current_price = float(data.iloc[-1])
        ma50 = float(data.rolling(50).mean().iloc[-1])
        
        return {"Price": current_price, "MA50": ma50, "Status": "✅ YF 解鎖"}
    except Exception as e:
        return {"Price": None, "MA50": None, "Status": f"❌ 錯誤: {e}"}

def get_factor2_opec_production():
    """Factor 2: 海灣出口 (代理: EIA International OPEC 原油產量)"""
    print("🛢️ 正在獲取 OPEC 原油產量 (EIA API)...")
    url = "https://api.eia.gov/v2/international/data/"
    params = {
        "api_key": EIA_API_KEY,
        "frequency": "monthly",
        "data[0]": "value",
        "facets[activityId][]": "1",         
        "facets[productId][]": "57",         
        "facets[countryRegionId][]": "OPEC", 
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 1
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        if 'response' in data and 'data' in data['response'] and len(data['response']['data']) > 0:
            val_kbd = float(data['response']['data'][0]['value'])
            val_mbd = val_kbd / 10  
            period = data['response']['data'][0]['period']
            return {"Production_Mbd": val_mbd, "Status": f"✅ EIA 解鎖 ({period})"}
        return {"Production_Mbd": None, "Status": "❌ 無數據回傳"}
    except Exception as e:
        return {"Production_Mbd": None, "Status": f"❌ 錯誤: {e}"}

def get_factor3_us_inventory():
    """Factor 3: 全美原油庫存 (代理: EIA U.S. Ending Stocks)"""
    print("🛢️ 正在獲取 全美原油總庫存 (EIA API)...")
    url = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
    params = {
        "api_key": EIA_API_KEY,
        "frequency": "weekly",
        "data[0]": "value",
        "facets[series][]": "WCESTUS1",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 1
    }
    try:
        res = requests.get(url, params=params, timeout=10)
        data = res.json()
        if 'response' in data and 'data' in data['response'] and len(data['response']['data']) > 0:
            val_k_bbl = float(data['response']['data'][0]['value'])
            val_m_bbl = val_k_bbl / 1000 
            period = data['response']['data'][0]['period']
            return {"Inventory_Mbbl": val_m_bbl, "Status": f"✅ EIA 解鎖 ({period})"}
    except Exception as e:
        return {"Inventory_Mbbl": None, "Status": f"❌ 錯誤: {e}"}

def get_factor4_and_5():
    """Factor 4 & 5: 裂解價差與布蘭特均線"""
    print("📈 正在獲取 Yahoo Finance 市場報價...")
    try:
        df = yf.download(["BZ=F", "HO=F"], period="100d", progress=False)
        close_df = df['Close'] if isinstance(df.columns, pd.MultiIndex) else df
        close_df = close_df.ffill()
        
        close_df['Crack'] = (close_df['HO=F'] * 42) - close_df['BZ=F']
        current_crack = float(close_df['Crack'].iloc[-1])
        ma60_crack = float(close_df['Crack'].rolling(60).mean().iloc[-1])
        crack_ratio = current_crack / ma60_crack if ma60_crack > 0 else 0
        
        brent_ma5 = float(close_df['BZ=F'].rolling(5).mean().iloc[-1])
        brent_slope = brent_ma5 - float(close_df['BZ=F'].rolling(5).mean().iloc[-4])
        
        return {
            "Crack_Ratio": crack_ratio, 
            "Brent_MA5": brent_ma5, 
            "Brent_Slope": brent_slope,
            "Status": "✅ YF 解鎖"
        }
    except Exception as e:
        return {"Crack_Ratio": None, "Brent_MA5": None, "Brent_Slope": None, "Status": f"❌ 錯誤: {e}"}

def run_5factor_dashboard():
    print("="*60)
    print("🔍 Hormuz 完整 5 因子體制評分卡 (實盤全 API 驅動版)")
    print("="*60)
    
    f1 = get_factor1_shipping()
    f2 = get_factor2_opec_production()
    f3 = get_factor3_us_inventory()
    f45 = get_factor4_and_5()
    
    # 評分邏輯 (更新 Factor 1 與 Factor 3 閾值)
    s1 = 1 if (f1.get('Price') and f1.get('MA50') and f1['Price'] > f1['MA50']) else 0
    s2 = 1 if (f2.get('Production_Mbd') and f2['Production_Mbd'] < 2600) else 0
    s3 = 1 if (f3.get('Inventory_Mbbl') and f3['Inventory_Mbbl'] < 420) else 0  # 全美庫存警戒線為 420M
    s4 = 1 if (f45.get('Crack_Ratio') and f45['Crack_Ratio'] > 1.5) else 0
    s5 = 1 if (f45.get('Brent_MA5') and f45['Brent_MA5'] > 95 and f45['Brent_Slope'] > 0) else 0
    
    score = s1 + s2 + s3 + s4 + s5
    
    # 防呆字串
    ship_str = f"TNK=${f1['Price']:.2f} (MA50=${f1['MA50']:.2f})" if f1.get('Price') else "N/A"
    prod_str = f"{f2['Production_Mbd']:.1f} 萬桶/日" if f2.get('Production_Mbd') else "N/A"
    inv_str = f"{f3['Inventory_Mbbl']:.1f} 百萬桶" if f3.get('Inventory_Mbbl') else "N/A"
    crack_str = f"{f45['Crack_Ratio']:.2f}x" if f45.get('Crack_Ratio') else "N/A"
    ma5_str = f"${f45['Brent_MA5']:.2f}" if f45.get('Brent_MA5') else "N/A"
    slope_str = f"{f45['Brent_Slope']:.2f}" if f45.get('Brent_Slope') else "N/A"
    
    print("\n📊 各因子即時狀態：")
    print(f"1️⃣ 船運指數 (油輪強勢)       : {ship_str} [{f1['Status']}] -> 得分: {s1}")
    print(f"2️⃣ OPEC產量 (<2600萬桶)     : {prod_str} [{f2['Status']}] -> 得分: {s2}")
    print(f"3️⃣ 全美原油庫存 (<420M桶)    : {inv_str} [{f3['Status']}] -> 得分: {s3}")
    print(f"4️⃣ 裂解價差 (>基準1.5x)     : {crack_str} [{f45['Status']}] -> 得分: {s4}")
    print(f"5️⃣ 布蘭特油價 (MA5>95且加速): MA5={ma5_str}, 坡度={slope_str} [{f45['Status']}] -> 得分: {s5}")
    
    print("-" * 60)
    print(f"🏆 總分: {score} / 5")
    
    if score >= 2:
        print("🚨 訊號：【進場做多能源】(地緣緊縮/供需失衡觸發！建議: LONG XLE / SHORT JETS)")
    else:
        print("🛡️ 訊號：【FLAT 空手觀望】(條件不足，資金停泊 SGOV)")
    print("="*60)

if __name__ == "__main__":
    run_5factor_dashboard()
