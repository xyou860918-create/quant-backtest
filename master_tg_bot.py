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
SYMBOLS = ["USD", "QLD", "TSLL", "BTC-USD"]
ENTRY_WINDOW = 20
EXIT_WINDOW = 10
TRAILING_STOP_PCT = 0.15

# 防禦模式參數
ATR_SPIKE_RATIO = 1.5      # 波動率飆升倍數閾值
DEFENSIVE_TRAILING = 0.08  # 防禦模式下的移動停利 (8%)
DEFENSIVE_EXIT = 5         # 防禦模式下的破底停損 (5日)

# 金鑰讀取
EIA_API_KEY = os.getenv("EIA_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload).raise_for_status()
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}")

# ==========================================
# 模組一：唐奇安 + ATR 動態避險策略
# ==========================================
def calculate_strategy_state(symbol):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=200) # 拉長天數以計算 60 日 ATR
    df = yf.download(symbol, start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"), progress=False)
    if df.empty: return f"❌ {symbol}: 無法抓取資料\n"

    close_col = df['Close'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['Close']
    high_col = df['High'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['High']
    low_col = df['Low'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['Low']
    data = pd.DataFrame({'price': close_col, 'high': high_col, 'low': low_col}).dropna()

    # 1. 唐奇安通道
    data['upper'] = data['high'].shift(1).rolling(ENTRY_WINDOW).max()
    data['lower_normal'] = data['low'].shift(1).rolling(EXIT_WINDOW).min()
    data['lower_defensive'] = data['low'].shift(1).rolling(DEFENSIVE_EXIT).min()

    # 2. ATR 波動率計算
    data['H-L'] = data['high'] - data['low']
    data['H-PC'] = abs(data['high'] - data['price'].shift(1))
    data['L-PC'] = abs(data['low'] - data['price'].shift(1))
    data['TR'] = data[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    data['ATR_14'] = data['TR'].rolling(14).mean()
    data['ATR_60'] = data['TR'].rolling(60).mean()
    
    data = data.dropna().copy()

    position, peak_price, signal = 0, 0.0, "HOLD"
    is_defensive_now = False

    for i in range(len(data)):
        p = data['price'].iloc[i]
        u = data['upper'].iloc[i]
        
        # 判斷當天是否觸發防禦模式
        atr_14 = data['ATR_14'].iloc[i]
        atr_60 = data['ATR_60'].iloc[i]
        is_defensive = (atr_60 > 0) and (atr_14 > atr_60 * ATR_SPIKE_RATIO)
        if i == len(data) - 1: is_defensive_now = is_defensive

        current_trailing = DEFENSIVE_TRAILING if is_defensive else TRAILING_STOP_PCT
        current_lower = data['lower_defensive'].iloc[i] if is_defensive else data['lower_normal'].iloc[i]

        signal = "HOLD"
        if position == 0:
            if p > u: position, peak_price, signal = 1, p, "BUY"
        elif position == 1:
            peak_price = max(peak_price, p)
            if p < peak_price * (1 - current_trailing): position, signal = 0, "SELL_TRAILING"
            elif p < current_lower: position, signal = 0, "SELL_DONCHIAN"

    last_price = float(data['price'].iloc[-1])
    last_upper = float(data['upper'].iloc[-1])
    
    # 狀態訊息組合
    status_msg = ""
    mode_tag = "🛡️ 防禦模式啟動" if is_defensive_now else "常規模式"
    trailing_pct_display = 8 if is_defensive_now else 15

    if position == 1:
        stop_price = peak_price * (1 - (DEFENSIVE_TRAILING if is_defensive_now else TRAILING_STOP_PCT))
        if signal == "BUY": status_msg = f"🟢 *今日買進 (BUY)* | 買入價: `${last_price:.2f}`"
        elif signal == "HOLD": status_msg = f"📈 *持倉中* ({mode_tag}) | {trailing_pct_display}% 停利線: `${stop_price:.2f}`"
    else:
        if signal == "SELL_TRAILING": status_msg = f"🛑 *{trailing_pct_display}% 移動停利出場* | 賣出價: `${last_price:.2f}`"
        elif signal == "SELL_DONCHIAN": status_msg = f"🔴 *跌破停損線出場* | 賣出價: `${last_price:.2f}`"
        else:
            diff_pct = ((last_upper / last_price) - 1) * 100
            status_msg = f"⚪ *空手觀望* ({mode_tag}) | 距突破線差: `{diff_pct:.1f}%`"

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
    msg += f"• 油輪運費(TNK): `{s1}` 分\n• OPEC產量緊縮: `{s2}` 分\n• 全美原油庫存: `{s3}` 分\n"
    msg += f"• 裂解價差優勢: `{s4}` 分\n• 油價動能加速: `{s5}` 分\n🏆 *總分: {score} / 5*\n"
    
    if score >= 2: msg += "🚨 *訊號*: 【進場做多能源】(建議: LONG XLE)"
    else: msg += "🛡️ *訊號*: 【FLAT 空手觀望】"
    return msg + "\n"

# ==========================================
# 主程式
# ==========================================
def main():
    print("啟動整合策略運算 (內建 ATR 避險)...")
    messages = ["🤖 *量化交易每日綜合戰報*\n" + "—"*22 + "\n"]
    
    for sym in SYMBOLS:
        try: messages.append(calculate_strategy_state(sym))
        except Exception as e: messages.append(f"❌ {sym} 錯誤: {e}\n")
    
    messages.append(get_sgov_status())
    messages.append("—"*22 + "\n")
    
    try: messages.append(generate_5factor_report())
    except Exception as e: messages.append(f"❌ 5因子錯誤: {e}\n")
    
    final_message = "\n".join(messages)
    send_telegram_message(final_message)
    print("✅ 實盤計算與綜合推播完成")

if __name__ == "__main__":
    main()
