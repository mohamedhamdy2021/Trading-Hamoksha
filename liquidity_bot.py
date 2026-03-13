import yfinance as yf
import pandas as pd
import feedparser
import requests
from transformers import pipeline
import time
from datetime import datetime
import pytz
import os

# --- إعدادات تليجرام ---
# البوت هيقرأ الـ Token والـ Chat ID من بيئة التشغيل (Environment Variables) اللي هنحطها في GitHub
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


print("Loading AI Model (this may take a minute on the first run)...")
try:
    sentiment_analyzer = pipeline("sentiment-analysis", model="mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis")
except Exception as e:
    print("Falling back to default model...")
    sentiment_analyzer = pipeline("sentiment-analysis")


class SmartMoneyBot:
    def __init__(self, symbol="EURUSD=X", initial_balance=1000.0, risk_per_trade=0.01):
        self.symbol = symbol
        self.balance = initial_balance
        self.risk_per_trade = risk_per_trade
        self.position = None
        self.entry_price = 0.0
        self.history = []

    def fetch_market_data(self):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching market data for {self.symbol}...")
        # Get 5-minute interval data
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
        """
        1️⃣ Kill Zones Strategy (Times of High Volatility)
        London Session typically: 07:00 - 16:00 UTC
        NY Session typically: 13:00 - 22:00 UTC
        Smart money maneuvers happen here.
        """
        if current_time.tzinfo is None:
            current_time = pytz.utc.localize(current_time)
        else:
            current_time = current_time.astimezone(pytz.utc)
            
        hour = current_time.hour
        # Combine London and NY Sessions: 7 AM to 10 PM UTC
        if 7 <= hour <= 22:
            return True
        return False

    def detect_order_blocks(self, data):
        """
        2️⃣ Order Blocks Strategy (Hedge Fund footprint)
        Detecting the last opposite candle before a strong impulsive block move.
        """
        if len(data) < 3:
            return None
        
        c1 = data.iloc[-4] # Older candle
        c2 = data.iloc[-3] # The Order Block candidate
        c3 = data.iloc[-2] # The momentum (impulsive) candle
        
        def get_val(row, col):
            if isinstance(data.columns, pd.MultiIndex): return row[(col, self.symbol)]
            else: return row[col]
            
        o2, c_2 = get_val(c2, 'Open'), get_val(c2, 'Close')
        o3, c_3 = get_val(c3, 'Open'), get_val(c3, 'Close')
        
        body_2 = abs(c_2 - o2)
        body_3 = abs(c_3 - o3)
        
        # Bullish Order Block (c2 is small bearish, c3 is large bullish momentum)
        if c_2 < o2 and c_3 > o3 and body_3 > (body_2 * 2):
            return {"type": "bullish_ob", "price": get_val(c2, 'Low')}
            
        # Bearish Order Block (c2 is small bullish, c3 is large bearish momentum)
        if c_2 > o2 and c_3 < o3 and body_3 > (body_2 * 2):
            return {"type": "bearish_ob", "price": get_val(c2, 'High')}
            
        return None

    def check_liquidity_grab(self, data):
        """
        3️⃣ Liquidity Sweep Strategy (Fake Breakouts)
        """
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
        
        # Fake Breakout Top -> SELL
        if current_high > previous_high and current_price < previous_high:
            print("❗ Liquidity Sweep at Resistance detected!")
            return -1
        
        # Fake Breakout Bottom -> BUY
        if current_low < previous_low and current_price > previous_low:
            print("❗ Liquidity Sweep at Support detected!")
            return 1
            
        return 0 

    def analyze_market_state(self, data):
        current_time = data.index[-1]
        
        # 1. Kill Zone Filter
        if not self.is_kill_zone(current_time):
            print("💤 Outside of Kill Zones (Low Volume). Skipping technical analysis to avoid fake moves.")
            return 0
            
        # 2. Check Liquidity Sweep
        signal = self.check_liquidity_grab(data)
        
        # 3. Check Order Blocks
        ob = self.detect_order_blocks(data)
        if ob:
            ob_msg = f"📦 *Order Block detected:* `{ob['type']}` near `{ob['price']:.5f}`"
            print(ob_msg)
            # We can optionally send order block alerts to TG
            send_telegram_message(ob_msg)
            
        return signal

    def execute_trade(self, signal, current_price, news_sentiment):
        # AI Filter
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
            # Simple Exit Logic if reversal happens
            if (self.position == "BUY" and signal == -1) or \
               (self.position == "SELL" and signal == 1):
                action_msg = self.close_position(float(current_price))
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
        
        self.history.append({'Type': self.position, 'Entry': self.entry_price, 'Exit': current_price, 'P/L': profit_loss})
        self.position = None
        return msg

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
            
            if msg and "ignored" not in msg:
                send_telegram_message(msg)
            elif signal == 0:
                status_msg = f"🔍 *Market Check Complete*\nPrice: `{float(current_price):.5f}`\nSignal: `{signal}`\n(No entry opportunities right now)"
                print(status_msg.replace('*', '').replace('`', ''))
                send_telegram_message(status_msg)
            print("-" * 50)
            
        except Exception as e:
            err_msg = f"Error during iteration: {e}"
            print(err_msg)
            send_telegram_message(f"⚠️ *Bot Error:* {e}")

if __name__ == "__main__":
    bot = SmartMoneyBot()
    startup_msg = f"🤖 *Smart Money Bot Initialized (Test Run)!*\n🌐 *Symbol:* `{bot.symbol}`\n💰 *Starting Balance:* `{bot.balance}$`"
    print(startup_msg.replace('*', '').replace('`', ''))
    
    # رسالة تجريبية أول ما يفتح عشان نتأكد إن الربط شغال
    send_telegram_message(startup_msg) 
    
    bot.run_one_iteration()
