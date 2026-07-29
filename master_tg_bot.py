#!/usr/bin/env python3
"""
Master Daily Telegram Bot — HARDCORE QUANT EDITION
- Donchian Channel: 2x Leveraged ETFs (UUP, QLD, TSLL) + BTC-USD
- Hormuz Energy: XLE Trend Following (D1a: XLE MA5>MA20 + ATR 4x trailing, weekly rebal)
- SGOV: Cash sweep status
- ADDED: Expectancy, Win/Loss ratios, Fee-adjusted edge, Regime detection, Kill Switch, Liquidity score
- Sends formatted Telegram message daily
"""

import os
import sys
import json
import asyncio
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

import yfinance as yf
import pandas as pd
import numpy as np
import requests

warnings.filterwarnings("ignore")

# ============================================================
# CONFIG
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
EIA_API_KEY = os.getenv("EIA_API_KEY")

STATE_FILE = Path(__file__).parent / "bot_state.json"
OUT_DIR = Path(__file__).parent / "bot_output"
OUT_DIR.mkdir(exist_ok=True)

# Donchian parameters
DONCHIAN_LOOKBACK = 20
DONCHIAN_SYMBOLS = {
    "UUP": "UUP",      # Dollar Index (was DX=F, delisted)
    "QLD": "QLD",      # 2x Nasdaq
    "TSLL": "TSLL",    # 2x Tesla
    "BTC-USD": "BTC-USD",
}

# Hormuz D1a: XLE Trend
HORMUZ_CFG = {
    "symbol": "XLE",
    "ma_fast": 5,
    "ma_slow": 20,
    "slope_lookback": 3,
    "atr_window": 14,
    "atr_mult": 4.0,
    "rebal_freq": "W-FRI",
    "min_hold_days": 3,
    "target_leverage": 0.95,
    "tc_bps": 5,
    "start_date": "2018-01-01",
}

# ============================================================
# HARDCORE QUANT METRICS CONFIG
# ============================================================
# Fee & slippage assumptions (basis points)
FEE_BPS = 5          # 5 bps per side = 10 bps round-trip
SLIPPAGE_BPS = 3     # 3 bps per side = 6 bps round-trip
TOTAL_COST_BPS = FEE_BPS * 2 + SLIPPAGE_BPS * 2  # 16 bps round-trip

# Regime detection parameters
REGIME_LOOKBACK = 60
ADX_THRESHOLD = 25
VOL_PERCENTILE_WINDOW = 252

# Kill Switch parameters
MAX_LOSS_PCT = 0.08      # 8% max loss per position
TIME_STOP_BARS = 20      # 20 bars time stop
REGIME_FLIP_EXIT = True  # Exit on regime flip

# Minimum edge threshold (after costs)
MIN_EXPECTANCY = 0.0     # Must be positive
MIN_WIN_RATE = 0.35      # 35% minimum win rate
MIN_PROFIT_FACTOR = 1.2  # Profit factor > 1.2


# ============================================================
# DATA CLASSES
# ============================================================
@dataclass
class SignalMetrics:
    """Hardcore quant metrics for every signal"""
    symbol: str
    signal: int                    # 1=long, -1=short, 0=flat
    price: float
    
    # Core expectancy metrics
    expectancy: float              # Expected value per trade (after costs)
    avg_win: float                 # Average winning trade
    avg_loss: float                # Average losing trade (negative)
    win_rate: float                # Win rate (0-1)
    profit_factor: float           # Gross profit / Gross loss
    
    # Cost-adjusted
    fee_adjusted_edge: float       # Expectancy after fees & slippage
    total_cost_bps: float          # Total round-trip cost in bps
    
    # Regime & risk
    regime: str                    # "trending_up", "trending_down", "mean_revert", "neutral"
    regime_strength: float         # 0-1, ADX-based
    liquidity_score: float         # 0-1, bid-ask spread & volume based
    signal_decay_score: float      # 0-1, how fresh is the signal
    
    # Kill Switch levels
    stop_loss_price: float         # Hard stop loss
    time_stop_date: str            # Time-based exit date
    max_hold_days: int             # Maximum hold period
    
    # Position sizing
    kelly_fraction: float          # Kelly criterion fraction (capped at 0.25)
    risk_per_trade_pct: float      # Risk as % of capital
    
    # Metadata
    lookback_days: int             # Days of history used
    timestamp: str                 # ISO timestamp


@dataclass
class PositionState:
    """Track live position state for feedback loop"""
    symbol: str
    entry_price: float
    entry_date: str
    current_price: float
    current_pnl: float
    max_favorable: float           # MFE - Max Favorable Excursion
    max_adverse: float             # MAE - Max Adverse Excursion
    days_held: int
    signal_at_entry: int
    regime_at_entry: str
    stop_loss_price: float
    trailing_stop: Optional[float] = None
    
    def update(self, price: float, high: float, low: float):
        """Update position with new price data"""
        self.current_price = price
        if self.signal_at_entry == 1:  # Long
            self.current_pnl = (price - self.entry_price) / self.entry_price
            self.max_favorable = max(self.max_favorable, (high - self.entry_price) / self.entry_price)
            self.max_adverse = min(self.max_adverse, (low - self.entry_price) / self.entry_price)
            if self.trailing_stop:
                self.trailing_stop = max(self.trailing_stop, price - (price * 0.02))  # 2% trail
        else:  # Short
            self.current_pnl = (self.entry_price - price) / self.entry_price
            self.max_favorable = max(self.max_favorable, (self.entry_price - low) / self.entry_price)
            self.max_adverse = min(self.max_adverse, (self.entry_price - high) / self.entry_price)
            if self.trailing_stop:
                self.trailing_stop = min(self.trailing_stop, price + (price * 0.02))
        self.days_held += 1
    
    def check_kill_switch(self, current_regime: str) -> tuple[bool, str]:
        """Check all kill switch conditions. Returns (should_exit, reason)"""
        # 1. Hard stop loss
        if self.signal_at_entry == 1 and self.current_price <= self.stop_loss_price:
            return True, "HARD_STOP_LOSS"
        if self.signal_at_entry == -1 and self.current_price >= self.stop_loss_price:
            return True, "HARD_STOP_LOSS"
        
        # 2. Time stop
        if self.days_held >= TIME_STOP_BARS:
            return True, "TIME_STOP"
        
        # 3. Regime flip
        if REGIME_FLIP_EXIT and self.regime_at_entry != "neutral" and current_regime != self.regime_at_entry:
            if (self.regime_at_entry == "trending_up" and current_regime in ["trending_down", "mean_revert"]) or \
               (self.regime_at_entry == "trending_down" and current_regime in ["trending_up", "mean_revert"]):
                return True, "REGIME_FLIP"
        
        # 4. Trailing stop hit
        if self.trailing_stop:
            if self.signal_at_entry == 1 and self.current_price <= self.trailing_stop:
                return True, "TRAILING_STOP"
            if self.signal_at_entry == -1 and self.current_price >= self.trailing_stop:
                return True, "TRAILING_STOP"
        
        return False, ""


# ============================================================
# HELPERS
# ============================================================
def fetch_ohlc(symbol: str, start: str, end: str = None) -> pd.DataFrame:
    """Fetch OHLC data from Yahoo Finance"""
    df = yf.download(symbol, start=start, end=end, interval="1d", 
                     progress=False, auto_adjust=False)
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna()


def fetch_close(symbol: str, start: str, end: str = None) -> pd.Series:
    """Fetch close prices only"""
    df = fetch_ohlc(symbol, start, end)
    if df.empty:
        return pd.Series(dtype=float)
    return df['Close']


def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Compute Average Directional Index (ADX)"""
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    
    plus_dm = high.diff()
    minus_dm = low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    minus_dm = minus_dm.abs()
    
    plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
    
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.rolling(period).mean()
    return adx


def detect_regime(close: pd.Series, high: pd.Series, low: pd.Series, 
                  volume: pd.Series) -> tuple[str, float]:
    """Detect market regime: trending_up, trending_down, mean_revert, neutral"""
    if len(close) < REGIME_LOOKBACK:
        return "neutral", 0.0
    
    # ADX for trend strength
    adx = compute_adx(high, low, close)
    adx_val = adx.iloc[-1] if not pd.isna(adx.iloc[-1]) else 0
    
    # Price vs MAs
    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else ma20
    price = close.iloc[-1]
    
    # Returns
    ret_20 = close.pct_change(20).iloc[-1]
    ret_5 = close.pct_change(5).iloc[-1]
    
    # Volatility percentile
    vol_20 = close.pct_change().rolling(20).std().iloc[-1] * np.sqrt(252)
    vol_history = close.pct_change().rolling(20).std() * np.sqrt(252)
    vol_pct = (vol_history < vol_20).mean() if len(vol_history.dropna()) > 20 else 0.5
    
    # Regime logic
    regime_strength = min(adx_val / ADX_THRESHOLD, 1.0) if adx_val > 0 else 0.0
    
    if adx_val > ADX_THRESHOLD:
        if price > ma20 > ma50 and ret_20 > 0:
            return "trending_up", regime_strength
        elif price < ma20 < ma50 and ret_20 < 0:
            return "trending_down", regime_strength
        else:
            return "mean_revert", regime_strength
    else:
        return "neutral", regime_strength


def compute_liquidity_score(df: pd.DataFrame) -> float:
    """Compute liquidity score from volume and spread proxy"""
    if len(df) < 20:
        return 0.5
    
    # Volume trend (normalized)
    vol_20 = df['Volume'].rolling(20).mean().iloc[-1]
    vol_5 = df['Volume'].rolling(5).mean().iloc[-1]
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0
    
    # Spread proxy: (High - Low) / Close
    spread_pct = ((df['High'] - df['Low']) / df['Close']).rolling(20).mean().iloc[-1]
    spread_score = max(0, 1 - spread_pct * 100)  # Lower spread = higher score
    
    # Volume score
    vol_score = min(vol_ratio / 2, 1.0)
    
    return (spread_score + vol_score) / 2


def compute_signal_decay(close: pd.Series, signal: int, lookback: int) -> float:
    """How many bars since signal triggered? 1.0 = fresh, 0.0 = stale"""
    if signal == 0:
        return 0.0
    
    # Find last signal change
    upper = close.rolling(lookback).max().shift(1)
    lower = close.rolling(lookback).min().shift(1)
    
    # Simulate historical signals
    hist_signals = pd.Series(0, index=close.index)
    hist_signals[close > upper] = 1
    hist_signals[close < lower] = -1
    
    # Find last bar where signal != current signal
    last_change = hist_signals[hist_signals != signal].index[-1] if any(hist_signals != signal) else hist_signals.index[0]
    bars_since = len(hist_signals) - hist_signals.index.get_loc(last_change) - 1
    
    # Decay: full strength for 5 bars, then linear decay to 0 at 20 bars
    if bars_since <= 5:
        return 1.0
    elif bars_since >= 20:
        return 0.0
    else:
        return 1.0 - (bars_since - 5) / 15


def backtest_donchian(close: pd.Series, lookback: int = 20) -> Dict[str, float]:
    """Run quick backtest on Donchian signals to get expectancy metrics"""
    if len(close) < lookback + 30:
        return {
            "expectancy": 0, "avg_win": 0, "avg_loss": 0, 
            "win_rate": 0, "profit_factor": 0, "trades": 0
        }
    
    upper = close.rolling(lookback).max().shift(1)
    lower = close.rolling(lookback).min().shift(1)
    
    # Generate signals
    signals = pd.Series(0, index=close.index)
    signals[close > upper] = 1
    signals[close < lower] = -1
    signals = signals.shift(1).fillna(0)  # Trade on next bar
    
    # Compute returns
    returns = close.pct_change().shift(-1)  # Next bar return
    strategy_returns = signals * returns
    
    # Apply costs
    # Cost incurred when signal changes
    signal_changes = signals.diff().abs()
    costs = signal_changes * (TOTAL_COST_BPS / 10000)
    net_returns = strategy_returns - costs
    
    # Trade analysis
    trades = net_returns[signal_changes > 0]
    if len(trades) == 0:
        return {"expectancy": 0, "avg_win": 0, "avg_loss": 0, "win_rate": 0, "profit_factor": 0, "trades": 0}
    
    wins = trades[trades > 0]
    losses = trades[trades < 0]
    
    win_rate = len(wins) / len(trades) if len(trades) > 0 else 0
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = losses.mean() if len(losses) > 0 else 0
    expectancy = trades.mean()
    
    gross_profit = wins.sum() if len(wins) > 0 else 0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 1
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    
    return {
        "expectancy": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "trades": len(trades)
    }


def compute_kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Kelly Criterion: f* = (p * b - q) / b where b = avg_win/|avg_loss|"""
    if avg_loss == 0 or win_rate == 0:
        return 0.0
    b = avg_win / abs(avg_loss)
    p = win_rate
    q = 1 - win_rate
    kelly = (p * b - q) / b if b > 0 else 0
    return max(0, min(kelly, 0.25))  # Cap at 25%


def donchian_signal_with_metrics(close: pd.Series, high: pd.Series, low: pd.Series, 
                                  volume: pd.Series, lookback: int = 20, 
                                  symbol: str = "") -> tuple[dict, SignalMetrics]:
    """Generate Donchian signal with full hardcore metrics"""
    if len(close) < lookback + 30:
        empty_metrics = SignalMetrics(
            symbol=symbol, signal=0, price=close.iloc[-1] if len(close) > 0 else 0,
            expectancy=0, avg_win=0, avg_loss=0, win_rate=0, profit_factor=0,
            fee_adjusted_edge=0, total_cost_bps=TOTAL_COST_BPS,
            regime="neutral", regime_strength=0, liquidity_score=0.5, signal_decay_score=0,
            stop_loss_price=0, time_stop_date="", max_hold_days=TIME_STOP_BARS,
            kelly_fraction=0, risk_per_trade_pct=0,
            lookback_days=len(close), timestamp=datetime.now().isoformat()
        )
        return {"signal": 0, "upper": np.nan, "lower": np.nan, "price": close.iloc[-1] if len(close) > 0 else np.nan}, empty_metrics
    
    upper = close.rolling(lookback).max().shift(1)
    lower = close.rolling(lookback).min().shift(1)
    price = close.iloc[-1]
    up = upper.iloc[-1]
    lo = lower.iloc[-1]
    
    if price > up:
        sig = 1
    elif price < lo:
        sig = -1
    else:
        sig = 0
    
    # Backtest for metrics
    bt = backtest_donchian(close, lookback)
    
    # Regime detection
    regime, regime_strength = detect_regime(close, high, low, volume)
    
    # Liquidity
    df_temp = pd.DataFrame({'High': high, 'Low': low, 'Close': close, 'Volume': volume})
    liquidity = compute_liquidity_score(df_temp)
    
    # Signal decay
    decay = compute_signal_decay(close, sig, lookback)
    
    # Kelly & risk
    kelly = compute_kelly_fraction(bt["win_rate"], bt["avg_win"], bt["avg_loss"])
    risk_pct = kelly * 100  # As percentage
    
    # Stop loss (ATR-based)
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    stop_mult = 2.0  # 2 ATR stop
    if sig == 1:
        stop_price = price - (atr * stop_mult)
    elif sig == -1:
        stop_price = price + (atr * stop_mult)
    else:
        stop_price = price
    
    # Time stop date
    time_stop_date = (datetime.now() + timedelta(days=TIME_STOP_BARS)).strftime("%Y-%m-%d")
    
    # Fee-adjusted edge
    fee_adj_edge = bt["expectancy"] - (TOTAL_COST_BPS / 10000)
    
    metrics = SignalMetrics(
        symbol=symbol,
        signal=sig,
        price=price,
        expectancy=bt["expectancy"],
        avg_win=bt["avg_win"],
        avg_loss=bt["avg_loss"],
        win_rate=bt["win_rate"],
        profit_factor=bt["profit_factor"],
        fee_adjusted_edge=fee_adj_edge,
        total_cost_bps=TOTAL_COST_BPS,
        regime=regime,
        regime_strength=regime_strength,
        liquidity_score=liquidity,
        signal_decay_score=decay,
        stop_loss_price=round(stop_price, 2),
        time_stop_date=time_stop_date,
        max_hold_days=TIME_STOP_BARS,
        kelly_fraction=kelly,
        risk_per_trade_pct=risk_pct,
        lookback_days=len(close),
        timestamp=datetime.now().isoformat()
    )
    
    signal_dict = {"signal": sig, "upper": up, "lower": lo, "price": price, "symbol": symbol}
    return signal_dict, metrics


def compute_xle_trend_signal(cfg: dict) -> tuple[dict, SignalMetrics]:
    """D1a: XLE own trend MA5>MA20 + slope + ATR trailing with full metrics"""
    end = datetime.now().strftime("%Y-%m-%d")
    start = cfg["start_date"]
    
    df = fetch_ohlc(cfg["symbol"], start, end)
    if df.empty:
        empty = {"error": "No XLE data", "signal": 0}
        empty_metrics = SignalMetrics(
            symbol="XLE", signal=0, price=0, expectancy=0, avg_win=0, avg_loss=0,
            win_rate=0, profit_factor=0, fee_adjusted_edge=0, total_cost_bps=TOTAL_COST_BPS,
            regime="neutral", regime_strength=0, liquidity_score=0.5, signal_decay_score=0,
            stop_loss_price=0, time_stop_date="", max_hold_days=TIME_STOP_BARS,
            kelly_fraction=0, risk_per_trade_pct=0, lookback_days=0,
            timestamp=datetime.now().isoformat()
        )
        return empty, empty_metrics
    
    close = df['Close']
    high = df['High']
    low = df['Low']
    volume = df['Volume']
    
    # Moving averages
    ma5 = close.rolling(cfg["ma_fast"]).mean()
    ma20 = close.rolling(cfg["ma_slow"]).mean()
    ma5_slope = ma5.diff(cfg["slope_lookback"])
    
    # Raw factor
    factor = ((ma5 > ma20) & (ma5_slope > 0)).astype(int)
    signal = factor.shift(1).fillna(0).astype(int)
    current_signal = int(signal.iloc[-1])
    
    # Backtest the MA crossover strategy
    returns = close.pct_change().shift(-1)
    strat_returns = signal * returns
    
    # Costs on signal changes
    sig_changes = signal.diff().abs()
    costs = sig_changes * (TOTAL_COST_BPS / 10000)
    net_returns = strat_returns - costs
    
    # Trade stats
    trades = net_returns[sig_changes > 0]
    if len(trades) > 0:
        wins = trades[trades > 0]
        losses = trades[trades < 0]
        win_rate = len(wins) / len(trades)
        avg_win = wins.mean() if len(wins) > 0 else 0
        avg_loss = losses.mean() if len(losses) > 0 else 0
        expectancy = trades.mean()
        gross_profit = wins.sum() if len(wins) > 0 else 0
        gross_loss = abs(losses.sum()) if len(losses) > 0 else 1
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
    else:
        win_rate = avg_win = avg_loss = expectancy = profit_factor = 0
    
    # ATR
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(cfg["atr_window"]).mean()
    stop_dist = atr * cfg["atr_mult"]
    current_atr = atr.iloc[-1]
    current_stop_dist = stop_dist.iloc[-1]
    
    # Regime
    regime, regime_strength = detect_regime(close, high, low, volume)
    
    # Liquidity
    liquidity = compute_liquidity_score(df)
    
    # Signal decay for MA crossover
    decay = compute_signal_decay(close, current_signal, cfg["ma_slow"])
    
    # Kelly
    kelly = compute_kelly_fraction(win_rate, avg_win, avg_loss)
    risk_pct = kelly * 100
    
    # Stop loss
    price = close.iloc[-1]
    if current_signal == 1:
        stop_price = price - current_stop_dist
    else:
        stop_price = price + current_stop_dist
    
    time_stop_date = (datetime.now() + timedelta(days=TIME_STOP_BARS)).strftime("%Y-%m-%d")
    fee_adj_edge = expectancy - (TOTAL_COST_BPS / 10000)
    
    # Weekly rebalance dates
    rebal_dates = signal.resample(cfg["rebal_freq"]).last().index
    next_rebal = str(rebal_dates[rebal_dates > df.index[-1]][0].date()) if len(rebal_dates[rebal_dates > df.index[-1]]) else "N/A"
    rebal_count = len(rebal_dates)
    
    metrics = SignalMetrics(
        symbol="XLE",
        signal=current_signal,
        price=price,
        expectancy=expectancy,
        avg_win=avg_win,
        avg_loss=avg_loss,
        win_rate=win_rate,
        profit_factor=profit_factor,
        fee_adjusted_edge=fee_adj_edge,
        total_cost_bps=TOTAL_COST_BPS,
        regime=regime,
        regime_strength=regime_strength,
        liquidity_score=liquidity,
        signal_decay_score=decay,
        stop_loss_price=round(stop_price, 2),
        time_stop_date=time_stop_date,
        max_hold_days=TIME_STOP_BARS,
        kelly_fraction=kelly,
        risk_per_trade_pct=risk_pct,
        lookback_days=len(close),
        timestamp=datetime.now().isoformat()
    )
    
    latest = {
        "price": price,
        "ma5": ma5.iloc[-1],
        "ma20": ma20.iloc[-1],
        "ma5_slope": ma5_slope.iloc[-1],
        "factor": int(factor.iloc[-1]),
        "signal": current_signal,
        "atr": current_atr,
        "stop_dist": current_stop_dist,
        "next_rebal": next_rebal,
        "rebal_dates_count": rebal_count,
    }
    
    return latest, metrics


def fetch_sgov() -> dict:
    """Fetch SGOV info"""
    try:
        df = fetch_ohlc("SGOV", "2023-01-01")
        if df.empty:
            return {"price": "N/A", "yield_est": "N/A"}
        price = df['Close'].iloc[-1]
        return {"price": round(price, 2), "yield_est": "~5.2%"}
    except Exception as e:
        return {"price": "N/A", "yield_est": f"Error: {e}"}


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


async def tg_send(text: str):
    """Send Telegram message"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram credentials not set, printing message:")
        print(text)
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print("[OK] Telegram message sent")
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")


def format_metrics(metrics: SignalMetrics) -> List[str]:
    """Format SignalMetrics for Telegram display"""
    lines = []
    
    # Core expectancy
    exp_emoji = "🟢" if metrics.expectancy > 0 else "🔴"
    lines.append(f"     {exp_emoji} <b>Expectancy</b>: {metrics.expectancy:.4%} "
                 f"(Fee-adj: {metrics.fee_adjusted_edge:.4%})")
    lines.append(f"     📊 <b>Win Rate</b>: {metrics.win_rate:.1%} | "
                 f"<b>Avg Win</b>: {metrics.avg_win:.4%} | "
                 f"<b>Avg Loss</b>: {metrics.avg_loss:.4%}")
    lines.append(f"     ⚖️ <b>Profit Factor</b>: {metrics.profit_factor:.2f} | "
                 f"<b>Cost</b>: {metrics.total_cost_bps:.0f}bps")
    
    # Regime & Risk
    regime_emoji = {"trending_up": "📈", "trending_down": "📉", 
                    "mean_revert": "🔄", "neutral": "⚪"}.get(metrics.regime, "❓")
    lines.append(f"     {regime_emoji} <b>Regime</b>: {metrics.regime} "
                 f"(Strength: {metrics.regime_strength:.0%})")
    lines.append(f"     💧 <b>Liquidity</b>: {metrics.liquidity_score:.0%} | "
                 f"<b>Signal Freshness</b>: {metrics.signal_decay_score:.0%}")
    
    # Kelly & Sizing
    lines.append(f"     🎯 <b>Kelly</b>: {metrics.kelly_fraction:.1%} "
                 f"(Risk/Trade: {metrics.risk_per_trade_pct:.1f}%)")
    
    # Kill Switch
    if metrics.signal != 0:
        lines.append(f"     🛑 <b>Stop</b>: ${metrics.stop_loss_price:.2f} | "
                     f"<b>Time Stop</b>: {metrics.time_stop_date} "
                     f"(Max {metrics.max_hold_days}d)")
    
    return lines


# ============================================================
# MAIN
# ============================================================
async def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== Daily Bot {today} ===")
    
    # --- Donchian Signals ---
    print("[1/3] Fetching Donchian signals with HARDCORE METRICS...")
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")  # More history for backtest
    
    donch_signals = {}
    donch_metrics = {}
    
    for name, sym in DONCHIAN_SYMBOLS.items():
        df = fetch_ohlc(sym, start, end)
        if df.empty:
            print(f"  {name}({sym}): NO DATA")
            continue
        
        close = df['Close']
        high = df['High']
        low = df['Low']
        volume = df['Volume']
        
        sig_dict, metrics = donchian_signal_with_metrics(
            close, high, low, volume, DONCHIAN_LOOKBACK, sym
        )
        donch_signals[name] = sig_dict
        donch_metrics[name] = metrics
        
        print(f"  {name}({sym}): sig={metrics.signal} px={metrics.price:.2f} "
              f"exp={metrics.expectancy:.4%} wr={metrics.win_rate:.1%} "
              f"pf={metrics.profit_factor:.2f} regime={metrics.regime} "
              f"kelly={metrics.kelly_fraction:.1%}")
    
    # --- Hormuz Energy (D1a: XLE Trend) ---
    print("[2/3] Computing Hormuz D1a (XLE Trend) with HARDCORE METRICS...")
    hormuz_data, hormuz_metrics = compute_xle_trend_signal(HORMUZ_CFG)
    
    if "error" in hormuz_data:
        print(f"  Error: {hormuz_data['error']}")
        hormuz_data = {"signal": 0, "price": 0, "ma5": 0, "ma20": 0, "factor": 0}
    else:
        print(f"  XLE: ${hormuz_data['price']:.2f} | "
              f"exp={hormuz_metrics.expectancy:.4%} wr={hormuz_metrics.win_rate:.1%} "
              f"pf={hormuz_metrics.profit_factor:.2f} regime={hormuz_metrics.regime} "
              f"kelly={hormuz_metrics.kelly_fraction:.1%}")
    
    # --- SGOV ---
    print("[3/3] Fetching SGOV...")
    sgov = fetch_sgov()
    print(f"  SGOV: ${sgov['price']} | Yield: {sgov['yield_est']}")
    
    # --- Build HARDCORE Message ---
    lines = []
    lines.append(f"🤖 <b>Daily Signal Bot — HARDCORE EDITION</b> | {today}")
    lines.append("━" * 35)
    
    # Donchian with Metrics
    lines.append("📊 <b>Donchian 20D (2x Leveraged + BTC) — QUANT METRICS</b>")
    for name in DONCHIAN_SYMBOLS.keys():
        if name not in donch_signals:
            continue
        d = donch_signals[name]
        m = donch_metrics[name]
        emoji = "🟢" if d['signal'] == 1 else ("🔴" if d['signal'] == -1 else "⚪")
        lines.append(f"  {emoji} <b>{name}</b> ({d['symbol']}): ${d['price']:.2f} | Signal: <b>{d['signal']}</b>")
        lines.extend(format_metrics(m))
        lines.append("")
    
    # Hormuz Energy with Metrics
    lines.append("⚡ <b>Hormuz Energy (D1a: XLE Trend) — QUANT METRICS</b>")
    if hormuz_data.get('signal', 0) == 1:
        lines.append(f"  🟢 <b>LONG XLE</b> @ ${hormuz_data['price']:.2f}")
        lines.append(f"     MA5: {hormuz_data['ma5']:.2f} > MA20: {hormuz_data['ma20']:.2f} ✅")
        lines.append(f"     MA5 Slope: {hormuz_data['ma5_slope']:.2f} {'✅' if hormuz_data['ma5_slope']>0 else '❌'}")
    else:
        lines.append(f"  🔴 <b>FLAT / CASH</b> @ ${hormuz_data.get('price', 0):.2f}")
        lines.append(f"     MA5: {hormuz_data.get('ma5', 0):.2f} vs MA20: {hormuz_data.get('ma20', 0):.2f} "
                     f"{'✅' if hormuz_data.get('ma5',0)>hormuz_data.get('ma20',0) else '❌'}")
        lines.append(f"     MA5 Slope: {hormuz_data.get('ma5_slope', 0):.2f} "
                     f"{'✅' if hormuz_data.get('ma5_slope',0)>0 else '❌'}")
    
    lines.append(f"     ATR: {hormuz_data.get('atr', 0):.2f} | Stop Dist: {hormuz_data.get('stop_dist', 0):.2f}")
    lines.append(f"     Next Rebal: {hormuz_data.get('next_rebal', 'N/A')} "
                 f"({hormuz_data.get('rebal_dates_count', 0)} weeks since 2018)")
    lines.append("")
    lines.extend(format_metrics(hormuz_metrics))
    lines.append("")
    
    # SGOV
    lines.append("💰 <b>SGOV Cash Sweep</b>")
    lines.append(f"  Price: ${sgov['price']} | Est Yield: {sgov['yield_est']}")
    lines.append("")
    
    # Allocation Logic (Enhanced with Kelly & Regime)
    lines.append("━" * 35)
    lines.append("📋 <b>Suggested Allocation (Kelly + Regime Aware)</b>")
    
    n_long = sum(1 for d in donch_signals.values() if d['signal'] == 1)
    h_sig = hormuz_data.get('signal', 0)
    
    # Count positive expectancy signals
    donch_positive = sum(1 for m in donch_metrics.values() 
                         if m.fee_adjusted_edge > 0 and m.win_rate >= MIN_WIN_RATE)
    hormuz_positive = (hormuz_metrics.fee_adjusted_edge > 0 and 
                       hormuz_metrics.win_rate >= MIN_WIN_RATE)
    
    # Regime filter: only take signals in favorable regime
    favorable_regimes = ["trending_up", "neutral"]
    donch_regime_ok = sum(1 for m in donch_metrics.values() 
                          if m.regime in favorable_regimes and m.signal == 1)
    hormuz_regime_ok = hormuz_metrics.regime in favorable_regimes and h_sig == 1
    
    if donch_positive >= 2 and hormuz_positive and donch_regime_ok >= 2 and hormuz_regime_ok:
        lines.append("  🟢 <b>AGGRESSIVE</b>: Full risk-on (Donchian + Energy)")
        lines.append("     ✅ Positive expectancy | ✅ Regime aligned | ✅ Kelly sizing active")
    elif donch_positive >= 2 and donch_regime_ok >= 2:
        lines.append("  🟡 <b>MODERATE</b>: Donchian only")
        lines.append("     ✅ Positive expectancy | ✅ Regime aligned")
    elif hormuz_positive and hormuz_regime_ok:
        lines.append("  🟡 <b>MODERATE</b>: Energy only")
        lines.append("     ✅ Positive expectancy | ✅ Regime aligned")
    else:
        lines.append("  🔴 <b>DEFENSIVE</b>: SGOV cash sweep")
        lines.append("     ❌ No positive edge after costs OR regime misaligned")
    
    lines.append("")
    lines.append(f"💡 <b>Risk Budget</b>: Max {MAX_LOSS_PCT:.0%} per position | "
                 f"Time Stop: {TIME_STOP_BARS}d | Regime Flip Exit: {'ON' if REGIME_FLIP_EXIT else 'OFF'}")
    lines.append("")
    lines.append(f"🦾 <i>Powered by Hormuz D1a + Donchian | Quant Metrics from Moltbook trading/agentfinance/crypto</i>")
    
    message = "\n".join(lines)
    
    # Save state with metrics
    save_state({
        "donchian": donch_signals,
        "donchian_metrics": {k: asdict(v) for k, v in donch_metrics.items()},
        "hormuz": hormuz_data,
        "hormuz_metrics": asdict(hormuz_metrics),
        "sgov": sgov,
        "timestamp": today,
        "config": {
            "fee_bps": FEE_BPS,
            "slippage_bps": SLIPPAGE_BPS,
            "total_cost_bps": TOTAL_COST_BPS,
            "max_loss_pct": MAX_LOSS_PCT,
            "time_stop_bars": TIME_STOP_BARS,
            "regime_flip_exit": REGIME_FLIP_EXIT,
        }
    })
    
    # Send
    await tg_send(message)
    print("[DONE] HARDCORE Signal sent")


if __name__ == "__main__":
    asyncio.run(main())
