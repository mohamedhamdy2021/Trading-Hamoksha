import yfinance as yf
import pandas as pd
import feedparser
import requests
from transformers import pipeline
import time
from datetime import datetime
import pytz
import os
import json

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

# ملف حفظ الذاكرة (عشان البوت ميضيعش الصفقات بين كل تشغيلة والتانية في السيرفر)
STATE_FILE = "bot_state.json"

class SmartMoneyBot:
    def __init__(self, symbol="EURUSD=X", initial_balance=1000.0, risk_per_trade=0.01):
        self.symbol = symbol
        self.balance = initial_balance
        self.risk_per_trade = risk_per_trade
        self.position = None
        self.entry_price = 0.0
        self.history = []
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.balance = data.get("balance", self.balance)
                    self.position = data.get("position", None)
                    self.entry_price = data.get("entry_price", 0.0)
                    self.history = data.get("history", [])
                print(f"📂 State Loaded: Balance {self.balance}$, Position: {self.position}")
            except Exception as e:
                print(f"Error loading state: {e}")

    def save_state(self):
        data = {
            "balance": self.balance,
            "position": self.position,
            "entry_price": self.entry_price,
            "history": self.history
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving state: {e}")

    def fetch_market_data(self):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching market data for {self.symbol}...")
        data = yf.download(self.symbol, period="5d", interval="5m", progress=False)
        return data

    def fetch_news_sentiment(self, query="EUR USD Forex"):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping and Analyzing News for '{query}'...")
        feed_url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}"
        feed = feedparser.parse(feed_url)
        
        sentiments = []
        for entry in feed.entries[:5]:
            result = sentiment_analyzer(entry.title)[0]
            label = result['label'].lower()
            if 'positive' in label:
                sentiments.append(1)
            elif 'negative' in label:
                sentiments.append(-1)
            else:
                sentiments.append(0)
                
        if not sentiments:
            return 0
            
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
        # 7 AM to 10 PM UTC
        if 7 <= hour <= 22:
            return True
        return False

    def detect_order_blocks(self, data):
        if len(data) < 3:
            return None
        c1 = data.iloc[-4]
        c2 = data.iloc[-3]
        c3 = data.iloc[-2]
        def get_val(row, col):
            if isinstance(data.columns, pd.MultiIndex): return row[(col, self.symbol)]
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

    def check_liquidity_grab(self, data):
        if len(data) < 21:
            return 0
        def get_val(row, col):
            if isinstance(data.columns, pd.MultiIndex): return row[(col, self.symbol)]
            else: return row[col]
        lookback_data = data.iloc[-21:-1]
        current_candle = data.iloc[-1]
        if isinstance(data.columns, pd.MultiIndex):
            previous_high = lookback_data[('High', self.symbol)].max()
            previous_low = lookback_data[('Low', self.symbol)].min()
        else:
            previous_high = lookback_data['High'].max()
            previous_low = lookback_data['Low'].min()
        current_price = get_val(current_candle, 'Close')
        current_high = get_val(current_candle, 'High')
        current_low = get_val(current_candle, 'Low')
        
        if current_high > previous_high and current_price < previous_high:
            print("❗ Liquidity Sweep at Resistance detected!")
            return -1
        if current_low < previous_low and current_price > previous_low:
            print("❗ Liquidity Sweep at Support detected!")
            return 1
        return 0 

    def analyze_market_state(self, data):
        current_time = data.index[-1]
        if not self.is_kill_zone(current_time):
            print("💤 Outside of Kill Zones (Low Volume).")
            return 0
        signal = self.check_liquidity_grab(data)
        ob = self.detect_order_blocks(data)
        if ob:
            ob_msg = f"📦 *Order Block detected:* `{ob['type']}` near `{ob['price']:.5f}`"
            print(ob_msg)
            # Notified in terminal, skipped TG to save spam
        return signal

    def execute_trade(self, signal, current_price, news_sentiment):
        if signal == 1 and news_sentiment == -1:
            msg = "⚠️ BUY signal ignored: Bad news sentiment."
            print(msg)
            return msg
        if signal == -1 and news_sentiment == 1:
            msg = "⚠️ SELL signal ignored: Positive news sentiment."
            print(msg)
            return msg
            
        action_msg = ""
        if self.position is None:
            if signal == 1:
                self.position = "BUY"
                self.entry_price = float(current_price)
                action_msg = f"🟩 *OPENING BUY* on {self.symbol}\n📍 *Entry:* `{current_price:.5f}`\n💰 *Balance:* `{self.balance:.2f}$`"
                print(action_msg)
            elif signal == -1:
                self.position = "SELL"
                self.entry_price = float(current_price)
                action_msg = f"🟥 *OPENING SELL* on {self.symbol}\n📍 *Entry:* `{current_price:.5f}`\n💰 *Balance:* `{self.balance:.2f}$`"
                print(action_msg)
        else:
            # Simple Exit Logic
            if (self.position == "BUY" and signal == -1) or \
               (self.position == "SELL" and signal == 1):
                action_msg = self.close_position(float(current_price))
                
        self.save_state()
        return action_msg

    def close_position(self, current_price):
        if self.position == "BUY":
            profit_loss = (current_price - self.entry_price) * 100000 
        elif self.position == "SELL":
            profit_loss = (self.entry_price - current_price) * 100000
            
        self.balance += profit_loss
        status = "🟢 WIN" if profit_loss > 0 else "🔴 LOSS"
        
        msg = f"⚪ *CLOSING {self.position}* on {self.symbol}\n💵 *P/L:* `{profit_loss:.2f}$` ({status})\n🏦 *New Balance:* `{self.balance:.2f}$`"
        print(msg)
        
        self.history.append({'Type': self.position, 'Entry': self.entry_price, 'Exit': current_price, 'P/L': profit_loss, 'Status': status})
        self.position = None
        
        # عند قفل الصفقة، يبعت رسالة الملخص تلقائياً
        self.send_summary()
        return msg

    def send_summary(self):
        if not self.history:
            return
            
        wins = sum(1 for t in self.history if "WIN" in t.get('Status', ''))
        total_trades = len(self.history)
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
        
        summary_msg = "📊 *Trading Session Summary*\n"
        summary_msg += f"💰 *Current Balance:* `{self.balance:.2f}$`\n"
        summary_msg += f"📈 *Win Rate:* `{win_rate:.1f}%` ({wins}/{total_trades})\n\n"
        summary_msg += "📜 *Recent Trades History:*\n"
        
        # هنجيب آخر 5 صفقات بس عشان الرسالة متطولش جداً
        for i, trade in enumerate(self.history[-5:], 1):
            emoji = "🟢" if "WIN" in trade.get('Status', '') else "🔴"
            summary_msg += f"{emoji} {trade['Type']} | P/L: `{trade['P/L']:.2f}$`\n"
            
        print(summary_msg.replace('*', '').replace('`', ''))
        send_telegram_message(summary_msg)

    def run_one_iteration(self):
        try:
            data = self.fetch_market_data()
            if isinstance(data.columns, pd.MultiIndex):
                current_price = data.iloc[-1][('Close', self.symbol)]
            else:
                current_price = data.iloc[-1]['Close']
            
            signal = self.analyze_market_state(data)
            news_sentiment = 0
            
            if signal != 0:
                news_sentiment = self.fetch_news_sentiment(query="EUR USD Forex Economy")
            
            msg = self.execute_trade(signal, current_price, news_sentiment)
            
            # بنعمل Send بس لرسايل الدخول والخروج المهمة!
            if msg and "ignored" not in msg:
                send_telegram_message(msg)
            elif signal == 0:
                print(f"Price: {float(current_price):.5f} | Signal: {signal}")
                
            print("-" * 50)
            
        except Exception as e:
            err_msg = f"Error during iteration: {e}"
            print(err_msg)
            send_telegram_message(f"⚠️ *Bot Error:* {e}")

if __name__ == "__main__":
    bot = SmartMoneyBot()
    print(f"🤖 Smart Money Bot Starting... Balance: {bot.balance}$")
    bot.run_one_iteration()
