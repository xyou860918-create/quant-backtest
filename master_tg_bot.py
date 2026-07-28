import yfinance as yf
import pandas as pd
import requests
import os
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')

# ==========================================
# 參數與金鑰設定
# ==========================================
# 1. 唐奇安策略參數
SYMBOLS = ["USD", "QLD", "TSLL"]
ENTRY_WINDOW = 20
EXIT_WINDOW = 10
TRAILING_STOP_PCT = 0.15

# 2. 金鑰 (已帶入你的專屬配置)
EIA_API_KEY = os.getenv("EIA_API_KEY", "LG2OcZHmhIOBcSOw98z5T8A47ojsxZkO92JOhc0I")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8862675833:AAFMlmMTOtBwI2sDjwlFgY39Gg5pOzjGWr8")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8318133732")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload).raise_for_status()
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}")

# ==========================================
# 模組一：唐奇安 2x 槓桿策略
# ==========================================
def calculate_strategy_state(symbol):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=180)
    df = yf.download(symbol, start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"), progress=False)
    if df.empty: return f"❌ {symbol}: 無法抓取資料\n"

    close_col = df['Close'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['Close']
    high_col = df['High'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['High']
    low_col = df['Low'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['Low']
    data = pd.DataFrame({'price': close_col, 'high': high_col, 'low': low_col}).dropna()

    data['upper'] = data['high'].shift(1).rolling(ENTRY_WINDOW).max()
    data['lower'] = data['low'].shift(1).rolling(EXIT_WINDOW).min()
    data = data.dropna().copy()

    position, peak_price, signal = 0, 0.0, "HOLD"
    for i in range(len(data)):
        p, u, l = data['price'].iloc[i], data['upper'].iloc[i], data['lower'].iloc[i]
        signal = "HOLD"
        if position == 0:
            if p > u: position, peak_price, signal = 1, p, "BUY"
        elif position == 1:
            peak_price = max(peak_price, p)
            if p < peak_price * (1 - TRAILING_STOP_PCT): position, signal = 0, "SELL_TRAILING"
            elif p < l: position, signal = 0, "SELL_DONCHIAN"

    last_price = float(data['price'].iloc[-1])
    last_upper, last_lower = float(data['upper'].iloc[-1]), float(data['lower'].iloc[-1])

    status_msg = ""
    if position == 1:
        stop_price = peak_price * (1 - TRAILING_STOP_PCT)
        if signal == "BUY": status_msg = f"🟢 *今日買進 (BUY)* | 買入價: `${last_price:.2f}`"
        elif signal == "HOLD": status_msg = f"🛡️ *多單持倉中* | 15% 停利線: `${stop_price:.2f}`"
    else:
        if signal == "SELL_TRAILING": status_msg = f"🛑 *15% 移動停利出場* | 賣出價: `${last_price:.2f}`"
        elif signal == "SELL_DONCHIAN": status_msg = f"🔴 *跌破 10 日停損* | 賣出價: `${last_price:.2f}`"
        else:
            diff_pct = ((last_upper / last_price) - 1) * 100
            status_msg = f"⚪ *空手觀望* | 距突破線差: `{diff_pct:.1f}%`"

    return f"📌 *{symbol}* (${last_price:.2f})\n• {status_msg}\n"

def get_sgov_status():
    try:
        df = yf.download("SGOV", period="5d", progress=False)
        last_price = float((df['Close']["SGOV"] if isinstance(df.columns, pd.MultiIndex) else df['Close']).iloc[-1])
        return f"🏦 *閒置資金停泊區 (SGOV)*: `${last_price:.2f}`\n"
    except: return "🏦 *閒置資金停泊區 (SGOV)*: 抓取失敗\n"

# ==========================================
# 模組二：Hormuz 5 因子地緣模型
# ==========================================
def get_factor1_shipping():
    try:
        df = yf.download("TNK", period="100d", progress=False)
        data = (df['Close']['TNK'] if isinstance(df.columns, pd.MultiIndex) else df['Close']).ffill()
        return {"Price": float(data.iloc[-1]), "MA50": float(data.rolling(50).mean().iloc[-1])}
    except: return {}

def get_factor2_opec_production():
    try:
        res = requests.get("https://api.eia.gov/v2/international/data/", params={"api_key": EIA_API_KEY, "frequency": "monthly", "data[0]": "value", "facets[activityId][]": "1", "facets[productId][]": "57", "facets[countryRegionId][]": "OPEC", "sort[0][column]": "period", "sort[0][direction]": "desc", "offset": 0, "length": 1}, timeout=10).json()
        return {"Production_Mbd": float(res['response']['data'][0]['value']) / 10}
    except: return {}

def get_factor3_us_inventory():
    try:
        res = requests.get("https://api.eia.gov/v2/petroleum/stoc/wstk/data/", params={"api_key": EIA_API_KEY, "frequency": "weekly", "data[0]": "value", "facets[series][]": "WCESTUS1", "sort[0][column]": "period", "sort[0][direction]": "desc", "offset": 0, "length": 1}, timeout=10).json()
        return {"Inventory_Mbbl": float(res['response']['data'][0]['value']) / 1000}
    except: return {}

def get_factor4_and_5():
    try:
        df = yf.download(["BZ=F", "HO=F"], period="100d", progress=False)
        close_df = (df['Close'] if isinstance(df.columns, pd.MultiIndex) else df).ffill()
        close_df['Crack'] = (close_df['HO=F'] * 42) - close_df['BZ=F']
        c_crack, ma60 = float(close_df['Crack'].iloc[-1]), float(close_df['Crack'].rolling(60).mean().iloc[-1])
        ma5 = float(close_df['BZ=F'].rolling(5).mean().iloc[-1])
        slope = ma5 - float(close_df['BZ=F'].rolling(5).mean().iloc[-4])
        return {"Crack_Ratio": c_crack/ma60 if ma60 > 0 else 0, "Brent_MA5": ma5, "Brent_Slope": slope}
    except: return {}

def generate_5factor_report():
    f1, f2, f3, f45 = get_factor1_shipping(), get_factor2_opec_production(), get_factor3_us_inventory(), get_factor4_and_5()
    
    s1 = 1 if f1.get('Price') and f1['Price'] > f1['MA50'] else 0
    s2 = 1 if f2.get('Production_Mbd') and f2['Production_Mbd'] < 2600 else 0
    s3 = 1 if f3.get('Inventory_Mbbl') and f3['Inventory_Mbbl'] < 420 else 0
    s4 = 1 if f45.get('Crack_Ratio') and f45['Crack_Ratio'] > 1.5 else 0
    s5 = 1 if f45.get('Brent_MA5') and f45['Brent_MA5'] > 95 and f45['Brent_Slope'] > 0 else 0
    
    score = s1 + s2 + s3 + s4 + s5
    
    msg = "🌍 *Hormuz 5 因子地緣評分卡*\n"
    msg += f"• 油輪運費(TNK): `{s1}` 分\n"
    msg += f"• OPEC產量緊縮: `{s2}` 分\n"
    msg += f"• 全美原油庫存: `{s3}` 分\n"
    msg += f"• 裂解價差優勢: `{s4}` 分\n"
    msg += f"• 油價動能加速: `{s5}` 分\n"
    msg += f"🏆 *總分: {score} / 5*\n"
    
    if score >= 2:
        msg += "🚨 *訊號*: 【進場做多能源】(建議: LONG XLE)"
    else:
        msg += "🛡️ *訊號*: 【FLAT 空手觀望】"
    return msg + "\n"

# ==========================================
# 主程式：組裝與發送
# ==========================================
def main():
    print("啟動整合策略運算...")
    messages = ["🤖 *量化交易每日綜合戰報*\n" + "—"*22 + "\n"]
    
    # 加入唐奇安戰報
    for sym in SYMBOLS:
        try: messages.append(calculate_strategy_state(sym))
        except Exception as e: messages.append(f"❌ {sym} 錯誤: {e}\n")
    
    messages.append(get_sgov_status())
    messages.append("—"*22 + "\n")
    
    # 加入能源 5 因子戰報
    try: messages.append(generate_5factor_report())
    except Exception as e: messages.append(f"❌ 5因子錯誤: {e}\n")
    
    final_message = "\n".join(messages)
    send_telegram_message(final_message)
    print("✅ 實盤計算與綜合推播完成")

if __name__ == "__main__":
    main()
