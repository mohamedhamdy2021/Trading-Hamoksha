import yfinance as yf
import pandas as pd
import feedparser
import requests
from transformers import pipeline
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

# ملف حفظ الذاكرة
STATE_FILE = "bot_state.json"

class SmartMoneyBotMulti:
    def __init__(self, initial_balance=1000.0):
        # قائمة الأزواج الرئيسية اللي هنراقبها
        self.symbols = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "GC=F"] # ضفنا اليورو، الباوند، الين، والذهب
        
        # أسماء مبسطة للأخبار
        self.news_queries = {
            "EURUSD=X": "EUR USD Forex",
            "GBPUSD=X": "GBP USD Forex",
            "USDJPY=X": "USD JPY Forex",
            "GC=F": "Gold XAU USD Market"
        }
        
        self.balance = initial_balance
        # سجلات منفصلة لكل زوج
        self.positions = {sym: None for sym in self.symbols}
        self.entry_prices = {sym: 0.0 for sym in self.symbols}
        self.history = {sym: [] for sym in self.symbols}
        
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.balance = data.get("balance", self.balance)
                    self.positions = data.get("positions", self.positions)
                    self.entry_prices = data.get("entry_prices", self.entry_prices)
                    self.history = data.get("history", self.history)
                print(f"📂 State Loaded: Balance {self.balance:.2f}$")
            except Exception as e:
                print(f"Error loading state: {e}")

    def save_state(self):
        data = {
            "balance": self.balance,
            "positions": self.positions,
            "entry_prices": self.entry_prices,
            "history": self.history
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            print(f"Error saving state: {e}")

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
        # 7 AM to 10 PM UTC
        if 7 <= hour <= 22: return True
        return False

    def detect_order_blocks(self, data, symbol):
        if len(data) < 3: return None
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
        if signal == 1 and news_sentiment == -1:
            print(f"[{symbol}] ⚠️ BUY ignored: Bad news.")
            return None
        if signal == -1 and news_sentiment == 1:
            print(f"[{symbol}] ⚠️ SELL ignored: Positive news.")
            return None
            
        action_msg = None
        current_pos = self.positions[symbol]
        
        if current_pos is None:
            if signal == 1:
                self.positions[symbol] = "BUY"
                self.entry_prices[symbol] = float(current_price)
                action_msg = f"🟩 *OPENING BUY* on {symbol}\n📍 *Entry:* `{current_price:.5f}`\n💰 *Total Balance:* `{self.balance:.2f}$`"
                print(action_msg)
            elif signal == -1:
                self.positions[symbol] = "SELL"
                self.entry_prices[symbol] = float(current_price)
                action_msg = f"🟥 *OPENING SELL* on {symbol}\n📍 *Entry:* `{current_price:.5f}`\n💰 *Total Balance:* `{self.balance:.2f}$`"
                print(action_msg)
        else:
            if (current_pos == "BUY" and signal == -1) or (current_pos == "SELL" and signal == 1):
                action_msg = self.close_position(symbol, float(current_price))
                
        self.save_state()
        return action_msg

    def close_position(self, symbol, current_price):
        current_pos = self.positions[symbol]
        entry = self.entry_prices[symbol]
        
        # معادلة أرباح موحدة تناسب كل الأزواج بناءً على نسبة التحرك من نقطة الدخول
        # افترضنا إن حجم الدخول بـ 100,000 دولار كرافعة مالية موحدة للتبسيط
        if current_pos == "BUY":
            profit_loss = ((current_price - entry) / entry) * 100000 
        elif current_pos == "SELL":
            profit_loss = ((entry - current_price) / entry) * 100000
            
        self.balance += profit_loss
        status = "🟢 WIN" if profit_loss > 0 else "🔴 LOSS"
        
        msg = f"⚪ *CLOSING {current_pos}* on {symbol}\n💵 *P/L:* `{profit_loss:.2f}$` ({status})\n🏦 *New Balance:* `{self.balance:.2f}$`"
        print(msg)
        
        self.history[symbol].append({
            'Type': current_pos, 
            'Entry': entry, 
            'Exit': current_price, 
            'P/L': profit_loss, 
            'Status': status,
            'Time': datetime.now().strftime('%Y-%m-%d %H:%M')
        })
        
        self.positions[symbol] = None
        self.entry_prices[symbol] = 0.0
        
        # إرسال ملخص الزوج ده بعد القفلة
        self.send_symbol_summary(symbol)
        return msg

    def send_symbol_summary(self, symbol):
        hist = self.history[symbol]
        if not hist: return
            
        wins = sum(1 for t in hist if "WIN" in t.get('Status', ''))
        total = len(hist)
        win_rate = (wins / total) * 100 if total > 0 else 0
        total_profit = sum(t['P/L'] for t in hist)
        
        summary_msg = f"📊 *{symbol} Performance Summary*\n"
        summary_msg += f"🏅 *Total P/L from {symbol}:* `{total_profit:.2f}$`\n"
        summary_msg += f"📈 *Win Rate:* `{win_rate:.1f}%` ({wins}/{total})\n\n"
        summary_msg += "📜 *Last 3 Trades:*\n"
        
        for t in hist[-3:]:
            emoji = "🟢" if "WIN" in t.get('Status', '') else "🔴"
            summary_msg += f"{emoji} {t['Type']} | P/L: `{t['P/L']:.2f}$`\n"
            
        print(summary_msg.replace('*', '').replace('`', ''))
        send_telegram_message(summary_msg)

    def run_all(self):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Starting multi-symbol scan...")
        for symbol in self.symbols:
            try:
                data = self.fetch_market_data(symbol)
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
                
                if signal != 0:
                    print(f"[{symbol}] Price: {float(current_price):.5f} | Signal: {signal}")
                
            except Exception as e:
                print(f"[{symbol}] Error: {e}")
        print("-" * 50)

if __name__ == "__main__":
    bot = SmartMoneyBotMulti()
    print(f"🤖 Smart Money Bot Starting for {len(bot.symbols)} symbols... Balance: {bot.balance}$")
    bot.run_all()
