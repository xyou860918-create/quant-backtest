import yfinance as yf
import pandas as pd
import requests
import os
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')

SYMBOLS = ["USD", "QLD", "TSLL"]
ENTRY_WINDOW = 20
EXIT_WINDOW = 10
TRAILING_STOP_PCT = 0.15

# 改回安全讀取環境變數，不上傳真實金鑰
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ 未設定 Telegram 金鑰，略過推播")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}).raise_for_status()
    except Exception as e:
        print(f"❌ Telegram 發送失敗: {e}")

def calculate_strategy_state(symbol):
    end_date = datetime.now()
    start_date = end_date - timedelta(days=180)
    df = yf.download(symbol, start=start_date.strftime("%Y-%m-%d"), end=end_date.strftime("%Y-%m-%d"), progress=False)
    
    if df.empty:
        return f"❌ {symbol}: 無法抓取資料"

    close_col = df['Close'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['Close']
    high_col = df['High'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['High']
    low_col = df['Low'][symbol] if isinstance(df.columns, pd.MultiIndex) else df['Low']

    data = pd.DataFrame({'price': close_col, 'high': high_col, 'low': low_col}).dropna()

    data['upper'] = data['high'].shift(1).rolling(ENTRY_WINDOW).max()
    data['lower'] = data['low'].shift(1).rolling(EXIT_WINDOW).min()
    data = data.dropna().copy()

    position = 0
    peak_price = 0.0
    signal = "HOLD"
    
    for i in range(len(data)):
        p = data['price'].iloc[i]
        u = data['upper'].iloc[i]
        l = data['lower'].iloc[i]
        signal = "HOLD"
        
        if position == 0:
            if p > u:
                position = 1
                peak_price = p
                signal = "BUY"
        elif position == 1:
            peak_price = max(peak_price, p)
            if p < peak_price * (1 - TRAILING_STOP_PCT):
                position = 0
                signal = "SELL_TRAILING"
            elif p < l:
                position = 0
                signal = "SELL_DONCHIAN"

    last_date = data.index[-1].strftime("%Y-%m-%d")
    last_price = float(data['price'].iloc[-1])
    last_upper = float(data['upper'].iloc[-1])
    last_lower = float(data['lower'].iloc[-1])
    
    status_msg = ""
    if position == 1:
        stop_price = peak_price * (1 - TRAILING_STOP_PCT)
        if signal == "BUY":
            status_msg = f"🟢 *強勢突破！今日買進 (BUY)*\n• 買入價: `${last_price:.2f}`"
        elif signal == "HOLD":
            status_msg = f"🛡️ *多單持倉中 (Holding)*\n• 波段最高價: `${peak_price:.2f}`\n• 15% 移動停利線: `${stop_price:.2f}`"
    else:
        if signal == "SELL_TRAILING":
            status_msg = f"🛑 *觸發 15% 移動停利！今日平倉*\n• 賣出價: `${last_price:.2f}`\n👉 資金建議轉入 SGOV 停泊"
        elif signal == "SELL_DONCHIAN":
            status_msg = f"🔴 *跌破 10 日低點！今日停損*\n• 賣出價: `${last_price:.2f}`\n👉 資金建議轉入 SGOV 停泊"
        else:
            diff_pct = ((last_upper / last_price) - 1) * 100
            status_msg = f"⚪ *空手觀望 (Cash)*\n• 距離進場突破線還差: `{diff_pct:.1f}%`"

    return f"📌 *{symbol} 每日戰報 ({last_date})*\n• 最新收盤: `${last_price:.2f}`\n• 唐奇安區間: `${last_lower:.2f}` ~ `${last_upper:.2f}`\n\n{status_msg}\n"

def get_sgov_status():
    try:
        df = yf.download("SGOV", period="5d", progress=False)
        close_col = df['Close']["SGOV"] if isinstance(df.columns, pd.MultiIndex) else df['Close']
        return f"🏦 *閒置資金停泊區*\n• SGOV 最新收盤: `${float(close_col.iloc[-1]):.2f}`\n_(當上方標的處於空手狀態時，請將資金放置於此生息)_"
    except Exception as e:
        return f"🏦 *閒置資金停泊區*\n• SGOV 報價抓取失敗: {e}"

def main():
    messages = [calculate_strategy_state(sym) for sym in SYMBOLS]
    messages.append(get_sgov_status())
    send_telegram_message("🤖 *唐奇安 2x 槓桿策略實盤機器人*\n" + "—"*22 + "\n\n" + "\n".join(messages))

if __name__ == "__main__":
    main()
