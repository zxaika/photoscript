import os
import logging
import tempfile
import time
import csv
import json
from pathlib import Path
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

load_dotenv()

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
AGNES_API_KEY = os.getenv("AGNES_API_KEY")

# Agnes API
AGNES_API_URL = "https://apihub.agnes-ai.com/v1/images/generations"

# Папки
BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
TEMP_DIR = BASE_DIR / "temp"
LOGS_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

# Файлы логов
ERRORS_CSV = LOGS_DIR / "errors.csv"

if not ERRORS_CSV.exists():
    with open(ERRORS_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(
            ['timestamp', 'user_id', 'username', 'first_name', 'last_name', 'full_name', 'error_type', 'error_message'])

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Состояния
STATE_SELECTING_MODE = 0
STATE_WAITING_CUSTOM_PROMPT = 2

# ========== ПРОМТЫ ==========

# 1. Убрать тени + белый фон + студийный свет
PROMPT_REMOVE_SHADOWS = """Remove all shadows from this photo completely. Make background pure white, studio lighting, even illumination, no shadows visible, professional product photography style, high quality, clean look, shadowless, bright and clear image"""

# ========== ФУНКЦИИ ==========
def get_user_info(user):
    user_id = str(user.id)
    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    if not full_name and username:
        full_name = f"@{username}"
    elif not full_name:
        full_name = f"User_{user_id}"

    return {
        'user_id': user_id,
        'username': username,
        'first_name': first_name,
        'last_name': last_name,
        'full_name': full_name
    }


def log_error(user_info, error_type, error_message):
    with open(ERRORS_CSV, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().isoformat(),
            user_info['user_id'],
            user_info['username'],
            user_info['first_name'],
            user_info['last_name'],
            user_info['full_name'],
            error_type,
            error_message
        ])


def upload_to_temp_hosting(image_path):
    """Загружает фото на бесплатный хостинг, возвращает URL"""
    try:
        url = "https://freeimage.host/api/1/upload"
        api_key = "6d207e02198a847aa98d0a2a901485a5"

        with open(image_path, 'rb') as f:
            files = {'source': f}
            data = {'key': api_key, 'format': 'json'}
            response = requests.post(url, files=files, data=data, timeout=30)

        if response.status_code == 200:
            result = response.json()
            if result.get('status_code') == 200:
                return result['image']['url']
        return None
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return None


async def generate_with_agnes(image_url: str, prompt: str):
    """Отправляет запрос в Agnes API"""
    if not AGNES_API_KEY:
        logger.error("AGNES_API_KEY не задан")
        return None

    try:
        headers = {
            "Authorization": f"Bearer {AGNES_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "agnes-image-2.0-flash",
            "prompt": prompt,
            "tags": ["img2img"],
            "extra_body": {
                "image": [image_url],
                "response_format": "url"
            },
            "size": "1024x1024",
            "seed": int(time.time())
        }

        response = requests.post(AGNES_API_URL, headers=headers, json=payload, timeout=120)

        if response.status_code == 200:
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["url"]
        return None
    except Exception as e:
        logger.error(f"Agnes error: {e}")
        return None


# ========== ФУНКЦИИ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_info = get_user_info(user)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Убрать тени + белый фон", callback_data="mode_shadows")],
        [InlineKeyboardButton("✏️ Свой запрос", callback_data="mode_custom")]
    ])

    welcome_text = f"""
👋 **Привет, {user_info['full_name']}!**

Я ИИ-бот для обработки фото. Выбери нужную функцию:

🧹 **Убрать тени + белый фон** — студийный свет, удаление теней, белый фон

✏️ **Свой запрос** — напиши что хочешь сделать с фото

💰 **Бесплатно** | ⏱️ 10-30 сек
    """
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=keyboard)
    context.user_data['state'] = STATE_SELECTING_MODE


async def mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора режима"""
    query = update.callback_query
    await query.answer()

    mode = query.data

    if mode == "mode_shadows":
        context.user_data['mode'] = 'shadows'
        context.user_data['state'] = None
        await query.message.reply_text(
            "🧹 **Режим: Убрать тени + белый фон + студийный свет**\n\n"
            "📤 Отправь фото:"
        )

    elif mode == "mode_custom":
        context.user_data['mode'] = 'custom'
        context.user_data['state'] = STATE_WAITING_CUSTOM_PROMPT
        await query.message.reply_text(
            "✏️ **Режим: Свой запрос**\n\n"
            "Напиши, что ты хочешь сделать с фото.\n\n"
            "**Примеры:**\n"
            "• «Сделай фото в стиле киберпанк»\n"
            "• «Добавь закат»\n"
            "• «Убери людей с фото»\n\n"
            "Напиши свой запрос:"
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает фото в зависимости от выбранного режима"""
    mode = context.user_data.get('mode')

    if not mode:
        await start(update, context)
        return

    if mode == 'custom':
        await handle_custom_photo(update, context)
        return

    await handle_general_photo(update, context)


async def handle_general_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка для режима shadows"""
    user = update.effective_user
    user_info = get_user_info(user)
    mode = context.user_data.get('mode', 'shadows')

    if mode == 'shadows':
        prompt = PROMPT_REMOVE_SHADOWS
        mode_name = "Убрать тени + белый фон + студийный свет"

    status_msg = await update.message.reply_text(
        f"📸 **{mode_name}**\n\n⏳ Обработка... 10-30 секунд",
        parse_mode="Markdown"
    )

    photo_file = await update.message.photo[-1].get_file()
    input_path = TEMP_DIR / f"input_{user.id}_{int(time.time())}.jpg"
    await photo_file.download_to_drive(str(input_path))

    start_time = time.time()

    try:
        await status_msg.edit_text(f"📤 Загружаю фото...")
        image_url = upload_to_temp_hosting(str(input_path))

        if not image_url:
            await status_msg.edit_text("❌ Ошибка загрузки фото")
            log_error(user_info, "upload_failed", "Cannot upload to hosting")
            return

        result_url = await generate_with_agnes(image_url, prompt)

        if result_url:
            processing_time = int(time.time() - start_time)

            response = requests.get(result_url, timeout=60)
            output_path = TEMP_DIR / f"output_{user.id}_{int(time.time())}.png"
            with open(output_path, 'wb') as f:
                f.write(response.content)

            with open(output_path, 'rb') as f:
                await update.message.reply_photo(
                    f,
                    caption=f"✅ **Готово!**\n\n⏱️ {processing_time} сек.\n\n📤 Отправь /start для новой обработки"
                )

            output_path.unlink(missing_ok=True)
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Ошибка ИИ. Попробуйте другое фото")
            log_error(user_info, "ai_failed", f"Agnes API returned no result")

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        log_error(user_info, "processing_error", str(e))

    finally:
        input_path.unlink(missing_ok=True)


async def handle_custom_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка для режима 'свой запрос'"""
    user = update.effective_user
    user_info = get_user_info(user)

    custom_prompt = context.user_data.get('custom_prompt')
    if not custom_prompt:
        context.user_data['waiting_for_prompt'] = True
        await update.message.reply_text(
            "✏️ **Сначала напиши свой запрос текстом,**\n"
            "а потом отправь фото!\n\n"
            "Или отправь /cancel для отмены."
        )
        return

    status_msg = await update.message.reply_text(
        f"✏️ **Твой запрос:**\n«{custom_prompt[:100]}»\n\n⏳ 10-30 секунд",
        parse_mode="Markdown"
    )

    photo_file = await update.message.photo[-1].get_file()
    input_path = TEMP_DIR / f"input_{user.id}_{int(time.time())}.jpg"
    await photo_file.download_to_drive(str(input_path))

    start_time = time.time()

    try:
        image_url = upload_to_temp_hosting(str(input_path))
        if not image_url:
            await status_msg.edit_text("❌ Ошибка загрузки фото")
            return

        result_url = await generate_with_agnes(image_url, custom_prompt)

        if result_url:
            processing_time = int(time.time() - start_time)

            response = requests.get(result_url, timeout=60)
            output_path = TEMP_DIR / f"output_{user.id}_{int(time.time())}.png"
            with open(output_path, 'wb') as f:
                f.write(response.content)

            with open(output_path, 'rb') as f:
                await update.message.reply_photo(
                    f,
                    caption=f"✅ **Готово!**\n\n📝 Запрос: «{custom_prompt[:80]}»\n⏱️ {processing_time} сек."
                )

            output_path.unlink(missing_ok=True)
            await status_msg.delete()
            context.user_data['custom_prompt'] = None
        else:
            await status_msg.edit_text("❌ Ошибка ИИ")

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")

    finally:
        input_path.unlink(missing_ok=True)


async def handle_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет кастомный промпт"""
    if context.user_data.get('waiting_for_prompt'):
        custom_prompt = update.message.text.strip()
        if len(custom_prompt) > 500:
            await update.message.reply_text("❌ Запрос слишком длинный (макс 500 символов)")
            return

        context.user_data['custom_prompt'] = custom_prompt
        context.user_data['waiting_for_prompt'] = False

        await update.message.reply_text(
            f"✅ **Запрос сохранен!**\n\n📝 «{custom_prompt}»\n\n📤 Теперь отправь фото:"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Отправь /start")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Справка:**\n\n"
        "/start — главное меню\n"
        "/cancel — отмена\n\n"
        "**Режимы:**\n"
        "🧹 Убрать тени — белый фон + студийный свет\n"
        "✏️ Свой запрос — напиши что хочешь",
        parse_mode="Markdown"
    )


# ========== ЗАПУСК ==========
def main():
    if not BOT_TOKEN:
        print("❌ Нет BOT_TOKEN в .env")
        return

    if not AGNES_API_KEY:
        print("❌ Нет AGNES_API_KEY в .env")
        return

    app = Application.builder().token(BOT_TOKEN).connect_timeout(60).read_timeout(60).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_command))

    # Админ-команды
    app.add_handler(CommandHandler("stat", admin_stats))
    app.add_handler(CommandHandler("errors", admin_errors))
    app.add_handler(CommandHandler("clear", admin_clear))

    # Обработчики
    app.add_handler(CallbackQueryHandler(mode_handler, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(mode_handler, pattern="^cancel$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_prompt))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("=" * 50)
    print("✅ Бот запущен!")
    print("🎨 Режимы: Убрать тени | Свой запрос")
    print("=" * 50)

    app.run_polling(drop_pending_updates=True)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа")
        return
    await update.message.reply_text("📊 Статистика бота\nАктивен")


async def admin_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа")
        return
    if ERRORS_CSV.exists():
        with open(ERRORS_CSV, 'rb') as f:
            await update.message.reply_document(f, filename="errors.csv")
    else:
        await update.message.reply_text("Нет ошибок")


async def admin_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа")
        return
    count = 0
    for file in TEMP_DIR.iterdir():
        if file.is_file():
            file.unlink()
            count += 1
    await update.message.reply_text(f"🗑️ Очищено {count} файлов")


if __name__ == "__main__":
    from datetime import datetime

    main()
