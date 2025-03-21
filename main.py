import os
import logging
import sqlite3
from datetime import datetime
from typing import Dict, Optional
import requests
import pytz
import google.generativeai as genai
from gtts import gTTS

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv





# Налаштування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TIMEZONEDB_API_KEY = os.getenv("TIMEZONEDB_API_KEY")
WEATHERAPI_API_KEY = os.getenv("WEATHERAPI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Ініціалізація Gemini
genai.configure(api_key=GEMINI_API_KEY)
generation_config = {
    "temperature": 0.9,
    "top_p": 1,
    "top_k": 1,
    "max_output_tokens": 2048,
}
model = genai.GenerativeModel('gemini-1.5-flash', generation_config=generation_config)

# Ініціалізація бази даних
def init_db():
    conn = sqlite3.connect('weather_bot.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            latitude REAL,
            longitude REAL,
            notification_time TEXT,
            timezone TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_user_settings(user_id: int, lat: float = None, lon: float = None, 
                    notification_time: str = None, timezone: str = None):
    conn = sqlite3.connect('weather_bot.db')
    c = conn.cursor()
    
    current = get_user_settings(user_id)
    new_lat = lat if lat is not None else current.get('latitude')
    new_lon = lon if lon is not None else current.get('longitude')
    new_time = notification_time if notification_time is not None else current.get('notification_time')
    new_tz = timezone if timezone is not None else current.get('timezone')
    
    c.execute('''
        INSERT OR REPLACE INTO users 
        (user_id, latitude, longitude, notification_time, timezone)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, new_lat, new_lon, new_time, new_tz))
    
    conn.commit()
    conn.close()

def get_user_settings(user_id: int) -> Dict:
    conn = sqlite3.connect('weather_bot.db')
    c = conn.cursor()
    c.execute('SELECT latitude, longitude, notification_time, timezone FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return {
            'latitude': result[0],
            'longitude': result[1],
            'notification_time': result[2],
            'timezone': result[3]
        }
    return {}

def get_timezone(lat: float, lon: float) -> str:
    try:
        url = f"http://api.timezonedb.com/v2.1/get-time-zone?key={TIMEZONEDB_API_KEY}&format=json&by=position&lat={lat}&lng={lon}"
        response = requests.get(url, timeout=10)
        data = response.json()
        return data.get('zoneName', 'Europe/Kiev')
    except Exception as e:
        logging.error(f"Помилка отримання часового поясу: {str(e)}")
        return "Europe/Kiev"

async def get_weather(lat: float, lon: float) -> Optional[Dict]:
    try:
        url = f"http://api.weatherapi.com/v1/current.json?key={WEATHERAPI_API_KEY}&q={lat},{lon}&lang=uk"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"Помилка отримання погоди: {str(e)}")
        return None

async def generate_weather_description(weather_data: Dict) -> str:
    try:
        prompt = f"""
        Напиши креативний прогноз погоди згідно з цими даними:
        {weather_data}
        Використовуй емодзі, жартівливий стиль та корисні поради.
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Помилка генерації опису: {str(e)}")
        return "🌤️ Прогноз погоди на сьогодні: гарна погода!"

async def send_daily_weather(bot, user_id: int):
    try:
        settings = get_user_settings(user_id)
        if not settings.get('latitude') or not settings.get('longitude'):
            return
            
        weather_data = await get_weather(settings['latitude'], settings['longitude'])
        if not weather_data:
            return
            
        description = await generate_weather_description(weather_data)
        tts = gTTS(text=description, lang='uk')
        tts.save("weather.mp3")
        
        await bot.send_message(
            chat_id=user_id,
            text=description
        )
        await bot.send_audio(
            chat_id=user_id,
            audio=open("weather.mp3", "rb")
        )
        os.remove("weather.mp3")
        
    except Exception as e:
        logging.error(f"Помилка відправки прогнозу: {str(e)}")

scheduler = AsyncIOScheduler()

async def setup_scheduler(app: Application) -> None:
    scheduler.start()
    restore_jobs(app.bot)

def restore_jobs(bot):
    conn = sqlite3.connect('weather_bot.db')
    c = conn.cursor()
    c.execute('SELECT user_id, notification_time, timezone FROM users WHERE notification_time IS NOT NULL')
    for user_id, time_str, timezone in c.fetchall():
        if time_str and timezone:
            hour, minute = map(int, time_str.split(':'))
            scheduler.add_job(
                send_daily_weather,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone),
                args=[bot, user_id],
                id=f"user_{user_id}",
                replace_existing=True
            )
    conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buttons = [
        [KeyboardButton("Відправити місцезнаходження 📍", request_location=True)],
        [KeyboardButton("Отримати прогноз зараз 🌤️")],  # Додаємо нову кнопку
        [KeyboardButton("Налаштувати сповіщення ⏰")]
    ]
    await update.message.reply_text(
        f"Привіт! Надішли своє місцезнаходження або вибери опцію з меню.",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )

async def handle_instant_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    settings = get_user_settings(user_id)

    if settings.get('latitude') and settings.get('longitude'):
        weather_text = await get_weather(settings['latitude'], settings['longitude'])
        await send_daily_weather(context.bot, user_id)
    else:
        await update.message.reply_text("🔍 Спочатку відправте своє місцезнаходження, щоб отримати прогноз.")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    location = update.message.location
    timezone = get_timezone(location.latitude, location.longitude)
    
    save_user_settings(
        user_id=user_id,
        lat=location.latitude,
        lon=location.longitude,
        timezone=timezone
    )
    
    await update.message.reply_text(
        f"📍 Локацію збережено!\n"
        f"Часовий пояс: {timezone}\n"
        "Тепер встановіть час сповіщень:"
    )

async def handle_time_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏰ Введіть час у форматі HH:MM (наприклад 08:00):")

async def handle_time_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    try:
        time_str = update.message.text
        hour, minute = map(int, time_str.split(':'))
        settings = get_user_settings(user_id)
        
        if not settings.get('timezone'):
            await update.message.reply_text("❌ Спочатку вкажіть локацію!")
            return
            
        scheduler.add_job(
            send_daily_weather,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=settings['timezone']),
            args=[context.bot, user_id],
            id=f"user_{user_id}",
            replace_existing=True
        )
        save_user_settings(user_id=user_id, notification_time=time_str)
        await update.message.reply_text(f"✅ Сповіщення встановлено на {time_str} вашого часу!")
        
    except ValueError:
        await update.message.reply_text("‼️ Невірний формат! Використовуйте HH:MM")
    except Exception as e:
        logging.error(f"Помилка: {str(e)}")
        await update.message.reply_text("‼️ Сталася помилка")
        

def main() -> None:
    application = Application.builder()\
        .token(os.getenv("TELEGRAM_BOT_TOKEN"))\
        .post_init(setup_scheduler)\
        .build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.Text(["Налаштувати сповіщення ⏰"]), handle_time_setup))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r'^\d{1,2}:\d{2}$'), handle_time_input))
    application.add_handler(MessageHandler(filters.Text(["Отримати прогноз зараз 🌤️"]), handle_instant_forecast))
    application.run_polling()

if __name__ == "__main__":
    main()