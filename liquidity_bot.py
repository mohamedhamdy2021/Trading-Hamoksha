import yfinance as yf
import pandas as pd
import feedparser
import requests
from transformers import pipeline
from datetime import datetime
import pytz
import os
import json
import time

# --- إعدادات تليجرام ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram tokens not set (Local run without TG notifications).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")

print("Loading AI Model...")
try:
    sentiment_analyzer = pipeline("sentiment-analysis", model="mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis")
except Exception as e:
    sentiment_analyzer = pipeline("sentiment-analysis")

STATE_FILE = "bot_state.json"

POSITION_SIZE = 100       # كل صفقة بتدخل بـ 100 دولار ثابتة
BROKER_FEE = 0.001        # عمولة 0.1% لكل عملية (دخول أو خروج)

# أسماء عرض مبسطة للأزواج (عشان الرسائل تبقى واضحة)
DISPLAY_NAMES = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "USDCHF=X": "USD/CHF",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD",
    "GBPJPY=X": "GBP/JPY",
    "EURJPY=X": "EUR/JPY",
    "EURGBP=X": "EUR/GBP",
    "AUDNZD=X": "AUD/NZD",
    "EURCHF=X": "EUR/CHF",
    "GBPCHF=X": "GBP/CHF",
    "CADJPY=X": "CAD/JPY",
    "GC=F":     "🥇 GOLD"
}

class SmartMoneyBotMulti:
    def __init__(self, initial_balance=1500.0):
        # 15 زوج: 14 فوركس + الذهب
        self.symbols = [
            "EURUSD=X",  # EUR/USD
            "GBPUSD=X",  # GBP/USD
            "USDJPY=X",  # USD/JPY
            "USDCHF=X",  # USD/CHF (الفرنك السويسري)
            "AUDUSD=X",  # AUD/USD (الدولار الأسترالي)
            "USDCAD=X",  # USD/CAD (الدولار الكندي)
            "NZDUSD=X",  # NZD/USD (الدولار النيوزيلندي)
            "GBPJPY=X",  # GBP/JPY (الباوند/ين - الزوج المجنون)
            "EURJPY=X",  # EUR/JPY
            "EURGBP=X",  # EUR/GBP
            "AUDNZD=X",  # AUD/NZD
            "EURCHF=X",  # EUR/CHF
            "GBPCHF=X",  # GBP/CHF
            "CADJPY=X",  # CAD/JPY
            "GC=F"       # GOLD (الذهب)
        ]
        
        # أسماء مبسطة للأخبار
        self.news_queries = {
            "EURUSD=X": "EUR USD Forex",
            "GBPUSD=X": "GBP USD Forex",
            "USDJPY=X": "USD JPY Forex",
            "USDCHF=X": "USD CHF Swiss Franc",
            "AUDUSD=X": "AUD USD Australian Dollar",
            "USDCAD=X": "USD CAD Canadian Dollar Oil",
            "NZDUSD=X": "NZD USD New Zealand Dollar",
            "GBPJPY=X": "GBP JPY Forex",
            "EURJPY=X": "EUR JPY Forex",
            "EURGBP=X": "EUR GBP Forex",
            "AUDNZD=X": "AUD NZD Forex",
            "EURCHF=X": "EUR CHF Forex",
            "GBPCHF=X": "GBP CHF Forex",
            "CADJPY=X": "CAD JPY Forex",
            "GC=F":     "Gold XAU USD Market"
        }
        
        self.balance = initial_balance
        self.positions = {sym: None for sym in self.symbols}
        self.entry_prices = {sym: 0.0 for sym in self.symbols}
        self.history = {sym: [] for sym in self.symbols}
        self.shadow_trades = {sym: None for sym in self.symbols}
        self.shadow_entry_prices = {sym: 0.0 for sym in self.symbols}
        self.shadow_history = {sym: [] for sym in self.symbols}
        
        self.load_state()

    def get_display_name(self, symbol):
        return DISPLAY_NAMES.get(symbol, symbol)

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.balance = data.get("balance", self.balance)
                    self.positions = data.get("positions", self.positions)
                    self.entry_prices = data.get("entry_prices", self.entry_prices)
                    self.history = data.get("history", self.history)
                    self.shadow_trades = data.get("shadow_trades", self.shadow_trades)
                    self.shadow_entry_prices = data.get("shadow_entry_prices", self.shadow_entry_prices)
                    self.shadow_history = data.get("shadow_history", self.shadow_history)
                print(f"📂 State Loaded: Balance {self.balance:.2f}$")
            except Exception as e:
                print(f"Error loading state: {e}")

    def save_state(self):
        data = {
            "balance": self.balance,
            "positions": self.positions,
            "entry_prices": self.entry_prices,
            "history": self.history,
            "shadow_trades": self.shadow_trades,
            "shadow_entry_prices": self.shadow_entry_prices,
            "shadow_history": self.shadow_history
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving state: {e}")

    def get_open_trade_count(self):
        return sum(1 for v in self.positions.values() if v is not None)

    def get_locked_balance(self):
        return self.get_open_trade_count() * POSITION_SIZE

    def get_available_balance(self):
        return self.balance - self.get_locked_balance()

    def fetch_market_data(self, symbol):
        data = yf.download(symbol, period="5d", interval="5m", progress=False)
        return data

    def fetch_news_sentiment(self, query):
        feed_url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}"
        feed = feedparser.parse(feed_url)
        
        sentiments = []
        for entry in feed.entries[:5]:
            result = sentiment_analyzer(entry.title)[0]
            label = result['label'].lower()
            if 'positive' in label: sentiments.append(1)
            elif 'negative' in label: sentiments.append(-1)
            else: sentiments.append(0)
                
        if not sentiments: return 0
        overall_sentiment = sum(sentiments)
        if overall_sentiment > 0: return 1
        elif overall_sentiment < 0: return -1
        return 0

    def is_kill_zone(self, current_time):
        if current_time.tzinfo is None:
            current_time = pytz.utc.localize(current_time)
        else:
            current_time = current_time.astimezone(pytz.utc)
        hour = current_time.hour
        if 7 <= hour <= 22: return True
        return False

    def detect_order_blocks(self, data, symbol):
        if len(data) < 4: return None
        c1, c2, c3 = data.iloc[-4], data.iloc[-3], data.iloc[-2]
        
        def get_val(row, col):
            if isinstance(data.columns, pd.MultiIndex): return row[(col, symbol)]
            else: return row[col]
            
        o2, c_2 = get_val(c2, 'Open'), get_val(c2, 'Close')
        o3, c_3 = get_val(c3, 'Open'), get_val(c3, 'Close')
        body_2 = abs(c_2 - o2)
        body_3 = abs(c_3 - o3)
        
        if c_2 < o2 and c_3 > o3 and body_3 > (body_2 * 2):
            return {"type": "bullish_ob", "price": get_val(c2, 'Low')}
        if c_2 > o2 and c_3 < o3 and body_3 > (body_2 * 2):
            return {"type": "bearish_ob", "price": get_val(c2, 'High')}
        return None

    def check_liquidity_grab(self, data, symbol):
        if len(data) < 21: return 0
        
        def get_val(row, col):
            if isinstance(data.columns, pd.MultiIndex): return row[(col, symbol)]
            else: return row[col]
            
        lookback_data = data.iloc[-21:-1]
        current_candle = data.iloc[-1]
        
        if isinstance(data.columns, pd.MultiIndex):
            previous_high = lookback_data[('High', symbol)].max()
            previous_low = lookback_data[('Low', symbol)].min()
        else:
            previous_high = lookback_data['High'].max()
            previous_low = lookback_data['Low'].min()
            
        current_price = get_val(current_candle, 'Close')
        current_high = get_val(current_candle, 'High')
        current_low = get_val(current_candle, 'Low')
        
        if current_high > previous_high and current_price < previous_high: return -1
        if current_low < previous_low and current_price > previous_low: return 1
        return 0 

    def analyze_market_state(self, data, symbol):
        current_time = data.index[-1]
        if not self.is_kill_zone(current_time): return 0
        return self.check_liquidity_grab(data, symbol)

    def execute_trade(self, symbol, signal, current_price, news_sentiment):
        display = self.get_display_name(symbol)
        
        # فلتر الأخبار
        if signal == 1 and news_sentiment == -1:
            print(f"[{display}] ⚠️ BUY ignored: Bad news.")
            return None
        if signal == -1 and news_sentiment == 1:
            print(f"[{display}] ⚠️ SELL ignored: Positive news.")
            return None
            
        action_msg = None
        current_pos = self.positions[symbol]
        shadow_pos = self.shadow_trades[symbol]
        
        # معالجة Shadow Trades
        if shadow_pos is not None:
            if (shadow_pos == "BUY" and signal == -1) or (shadow_pos == "SELL" and signal == 1):
                self.close_shadow_trade(symbol, float(current_price))
        
        if current_pos is None:
            if signal != 0:
                available = self.get_available_balance()
                entry_fee = POSITION_SIZE * BROKER_FEE
                
                if available >= POSITION_SIZE:
                    direction = "BUY" if signal == 1 else "SELL"
                    self.positions[symbol] = direction
                    self.entry_prices[symbol] = float(current_price)
                    self.balance -= entry_fee
                    
                    reason = "Liquidity Sweep at Support" if signal == 1 else "Liquidity Sweep at Resistance"
                    emoji = "🟩" if signal == 1 else "🟥"
                    open_count = self.get_open_trade_count()
                    
                    action_msg = (f"{emoji} *FOREX {direction}* on *{display}*\n"
                                  f"📍 *Entry:* `{current_price:.5f}`\n"
                                  f"💵 *Deal Size:* `{POSITION_SIZE}$`\n"
                                  f"💸 *Entry Fee:* `-{entry_fee:.2f}$`\n"
                                  f"📂 *Open Trades:* `{open_count}/{len(self.symbols)}`\n"
                                  f"💰 *Balance:* `{self.balance:.2f}$`\n"
                                  f"💥 _Reason: {reason}_")
                    print(action_msg)
                else:
                    direction = "BUY" if signal == 1 else "SELL"
                    reason = "Liquidity Sweep at Support" if signal == 1 else "Liquidity Sweep at Resistance"
                    
                    self.shadow_trades[symbol] = direction
                    self.shadow_entry_prices[symbol] = float(current_price)
                    
                    open_count = self.get_open_trade_count()
                    action_msg = (f"⚠️ *SIGNAL (Not Enough Balance)* ⚠️\n"
                                  f"📊 *Pair:* `{display}`\n"
                                  f"📍 *Recommended:* `{direction}` at `{current_price:.5f}`\n"
                                  f"📂 *Open Trades:* `{open_count}/{len(self.symbols)}`\n"
                                  f"💰 *Available:* `{available:.2f}$` (need `{POSITION_SIZE}$`)\n"
                                  f"💥 _Reason: {reason}_\n"
                                  f"📌 _Will track result without affecting balance_")
                    print(action_msg)
        else:
            if (current_pos == "BUY" and signal == -1) or (current_pos == "SELL" and signal == 1):
                action_msg = self.close_position(symbol, float(current_price))
                
        self.save_state()
        return action_msg

    def close_position(self, symbol, current_price):
        display = self.get_display_name(symbol)
        current_pos = self.positions[symbol]
        entry = self.entry_prices[symbol]
        
        if current_pos == "BUY":
            gross_pnl = POSITION_SIZE * ((current_price - entry) / entry)
        elif current_pos == "SELL":
            gross_pnl = POSITION_SIZE * ((entry - current_price) / entry)
            
        exit_fee = POSITION_SIZE * BROKER_FEE
        net_pnl = gross_pnl - exit_fee
        total_fees = (POSITION_SIZE * BROKER_FEE) * 2
            
        pct_change = ((current_price - entry) / entry) * 100
        self.balance += gross_pnl - exit_fee
        status = "🟢 WIN" if net_pnl > 0 else "🔴 LOSS"
        
        msg = (f"💸 *FOREX TRADE CLOSED* 💸\n"
               f"📊 *Pair:* `{display}`\n"
               f"🔄 *Type:* `{current_pos}`\n"
               f"📍 *Entry:* `{entry:.5f}`\n"
               f"🏁 *Exit:* `{current_price:.5f}`\n"
               f"📊 *Change:* `{pct_change:+.2f}%`\n"
               f"💰 *Gross P/L:* `{gross_pnl:+.2f}$`\n"
               f"💸 *Total Fees:* `-{total_fees:.2f}$` (0.1% x2)\n"
               f"💵 *Net P/L:* `{net_pnl:+.2f}$` ({status})\n"
               f"🏦 *New Balance:* `{self.balance:.2f}$`")
        print(msg)
        
        self.history[symbol].append({
            'Type': current_pos, 
            'Entry': entry, 
            'Exit': current_price, 
            'Gross_PnL': round(gross_pnl, 2),
            'Fees': round(total_fees, 2),
            'P/L': round(net_pnl, 2), 
            'Status': status,
            'Time': datetime.now().strftime('%Y-%m-%d %H:%M')
        })
        
        self.positions[symbol] = None
        self.entry_prices[symbol] = 0.0
        
        self.send_symbol_summary(symbol)
        return msg

    def close_shadow_trade(self, symbol, current_price):
        display = self.get_display_name(symbol)
        shadow_pos = self.shadow_trades[symbol]
        entry = self.shadow_entry_prices[symbol]
        
        if shadow_pos == "BUY":
            gross_pnl = POSITION_SIZE * ((current_price - entry) / entry)
        elif shadow_pos == "SELL":
            gross_pnl = POSITION_SIZE * ((entry - current_price) / entry)
            
        total_fees = (POSITION_SIZE * BROKER_FEE) * 2
        net_pnl = gross_pnl - total_fees
        pct_change = ((current_price - entry) / entry) * 100
        status = "🟢 WIN" if net_pnl > 0 else "🔴 LOSS"
        
        msg = (f"👻 *SHADOW TRADE RESULT (Not in Balance)* 👻\n"
               f"📊 *Pair:* `{display}`\n"
               f"🔄 *Type:* `{shadow_pos}`\n"
               f"📍 *Entry:* `{entry:.5f}`\n"
               f"🏁 *Exit:* `{current_price:.5f}`\n"
               f"📊 *Change:* `{pct_change:+.2f}%`\n"
               f"💵 *Would-be Net P/L:* `{net_pnl:+.2f}$` ({status})\n"
               f"📌 _This trade was NOT executed (insufficient balance)_")
        print(msg)
        send_telegram_message(msg)
        
        self.shadow_history[symbol].append({
            'Type': shadow_pos,
            'Entry': entry,
            'Exit': current_price,
            'P/L': round(net_pnl, 2),
            'Status': status,
            'Time': datetime.now().strftime('%Y-%m-%d %H:%M')
        })
        
        self.shadow_trades[symbol] = None
        self.shadow_entry_prices[symbol] = 0.0
        self.save_state()

    def send_symbol_summary(self, symbol):
        display = self.get_display_name(symbol)
        hist = self.history[symbol]
        if not hist: return
            
        wins = sum(1 for t in hist if "WIN" in t.get('Status', ''))
        total = len(hist)
        win_rate = (wins / total) * 100 if total > 0 else 0
        total_profit = sum(t['P/L'] for t in hist)
        total_fees = sum(t.get('Fees', 0) for t in hist)
        
        summary_msg = f"📊 *{display} Performance Summary*\n"
        summary_msg += f"🏅 *Net P/L:* `{total_profit:+.2f}$`\n"
        summary_msg += f"💸 *Total Fees Paid:* `{total_fees:.2f}$`\n"
        summary_msg += f"📈 *Win Rate:* `{win_rate:.1f}%` ({wins}/{total})\n\n"
        summary_msg += "📜 *Recent Trades:*\n"
        
        for t in hist[-3:]:
            emoji = "🟢" if "WIN" in t.get('Status', '') else "🔴"
            summary_msg += f"{emoji} {t['Type']} | Entry: `{t['Entry']:.5f}` → Exit: `{t['Exit']:.5f}` | Net: `{t['P/L']:+.2f}$`\n"
            
        print(summary_msg.replace('*', '').replace('`', ''))
        send_telegram_message(summary_msg)

    def run_all(self):
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(self.symbols)} Forex pairs...")
        print(f"💰 Balance: {self.balance:.2f}$ | 📂 Open: {self.get_open_trade_count()}/{len(self.symbols)} | 🔓 Available: {self.get_available_balance():.2f}$")
        for symbol in self.symbols:
            try:
                data = self.fetch_market_data(symbol)
                if data.empty or len(data) < 21:
                    continue
                    
                if isinstance(data.columns, pd.MultiIndex):
                    current_price = data.iloc[-1][('Close', symbol)]
                else:
                    current_price = data.iloc[-1]['Close']
                
                signal = self.analyze_market_state(data, symbol)
                news_sentiment = 0
                
                if signal != 0:
                    news_sentiment = self.fetch_news_sentiment(query=self.news_queries[symbol])
                
                msg = self.execute_trade(symbol, signal, current_price, news_sentiment)
                
                if msg:
                    send_telegram_message(msg)
                
                display = self.get_display_name(symbol)
                if signal != 0:
                    print(f"[{display}] Price: {float(current_price):.5f} | Signal: {signal}")
                else:
                    print(f"[{display}] Price: {float(current_price):.5f} | Signal: 0 (No trade)")
                
            except Exception as e:
                display = self.get_display_name(symbol)
                print(f"[{display}] Error: {e}")
        print("-" * 60)

if __name__ == "__main__":
    bot = SmartMoneyBotMulti()
    print(f"🤖 Forex Bot Ready | Balance: {bot.balance:.2f}$ | Monitoring: {len(bot.symbols)} pairs")
    
    if os.getenv('GITHUB_ACTIONS'):
        bot.run_all()
    else:
        while True:
            bot.run_all()
            print("Waiting 5 minutes for the next candle... ⏳")
            time.sleep(300)
