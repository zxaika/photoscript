import os
import logging
import tempfile
import time
import csv
import json
from pathlib import Path
from datetime import datetime, timedelta
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
USERS_CSV = LOGS_DIR / "users.csv"
REQUESTS_CSV = LOGS_DIR / "requests.csv"
STATS_JSON = LOGS_DIR / "stats.json"

# Инициализация файлов
if not ERRORS_CSV.exists():
    with open(ERRORS_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'user_id', 'username', 'first_name', 'last_name', 'full_name', 'error_type', 'error_message'])

if not USERS_CSV.exists():
    with open(USERS_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['user_id', 'username', 'first_name', 'last_name', 'full_name', 'first_seen', 'last_seen', 'total_requests'])

if not REQUESTS_CSV.exists():
    with open(REQUESTS_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'user_id', 'username', 'full_name', 'mode', 'prompt', 'processing_time', 'status'])

if not STATS_JSON.exists():
    with open(STATS_JSON, 'w', encoding='utf-8') as f:
        json.dump({'total_requests': 0, 'users': {}}, f)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Состояния
STATE_SELECTING_MODE = 0
STATE_WAITING_CUSTOM_PROMPT = 2

# ========== ПРОМТЫ ==========
PROMPT_REMOVE_SHADOWS = """Remove all shadows from this photo completely. Make background pure white, studio lighting, even illumination, no shadows visible, professional product photography style, high quality, clean look, shadowless, bright and clear image"""

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ДАННЫМИ ==========
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

def log_user(user_info):
    """Логирует пользователя"""
    try:
        # Проверяем существование пользователя
        existing = []
        with open(USERS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            existing = list(reader)
        
        user_exists = False
        for row in existing:
            if row['user_id'] == user_info['user_id']:
                user_exists = True
                row['last_seen'] = datetime.now().isoformat()
                row['total_requests'] = str(int(row['total_requests']) + 1)
                break
        
        if not user_exists:
            new_row = {
                'user_id': user_info['user_id'],
                'username': user_info['username'],
                'first_name': user_info['first_name'],
                'last_name': user_info['last_name'],
                'full_name': user_info['full_name'],
                'first_seen': datetime.now().isoformat(),
                'last_seen': datetime.now().isoformat(),
                'total_requests': '1'
            }
            existing.append(new_row)
        
        # Перезаписываем файл
        with open(USERS_CSV, 'w', newline='', encoding='utf-8') as f:
            if existing:
                writer = csv.DictWriter(f, fieldnames=existing[0].keys())
                writer.writeheader()
                writer.writerows(existing)
                
    except Exception as e:
        logger.error(f"Error logging user: {e}")

def log_request(user_info, mode, prompt, processing_time, status='success'):
    """Логирует запрос"""
    try:
        with open(REQUESTS_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                user_info['user_id'],
                user_info['username'],
                user_info['full_name'],
                mode,
                prompt[:200],  # Обрезаем длинные промпты
                processing_time,
                status
            ])
        
        # Обновляем статистику
        with open(STATS_JSON, 'r', encoding='utf-8') as f:
            stats = json.load(f)
        
        stats['total_requests'] += 1
        if user_info['user_id'] not in stats['users']:
            stats['users'][user_info['user_id']] = {
                'username': user_info['username'],
                'full_name': user_info['full_name'],
                'requests': 0
            }
        stats['users'][user_info['user_id']]['requests'] += 1
        
        with open(STATS_JSON, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2)
            
    except Exception as e:
        logger.error(f"Error logging request: {e}")

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

def get_stats():
    """Получает статистику"""
    try:
        with open(STATS_JSON, 'r', encoding='utf-8') as f:
            stats = json.load(f)
        return stats
    except:
        return {'total_requests': 0, 'users': {}}

def get_users():
    """Получает список пользователей"""
    try:
        users = []
        with open(USERS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            users = list(reader)
        return users
    except:
        return []

def get_requests(limit=50):
    """Получает последние запросы"""
    try:
        requests = []
        with open(REQUESTS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            requests = list(reader)
        return requests[-limit:] if len(requests) > limit else requests
    except:
        return []

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

async def generate_with_agnes(image_url: str, prompt: str, size: str = None):
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
            }
        }

        if size and size != "auto":
            payload["size"] = size

        response = requests.post(AGNES_API_URL, headers=headers, json=payload, timeout=120)

        if response.status_code == 200:
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                return data["data"][0]["url"]
        else:
            logger.error(f"Agnes API error: {response.status_code} - {response.text}")
        return None
    except Exception as e:
        logger.error(f"Agnes error: {e}")
        return None

# ========== АДМИН-КОМАНДЫ ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главная админ-панель"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("📝 Последние запросы", callback_data="admin_requests")],
        [InlineKeyboardButton("⚠️ Ошибки", callback_data="admin_errors")],
        [InlineKeyboardButton("🗑️ Очистить кэш", callback_data="admin_clear")],
        [InlineKeyboardButton("📤 Экспорт данных", callback_data="admin_export")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="admin_refresh")]
    ])
    
    stats = get_stats()
    users = get_users()
    
    text = f"""
👑 **Админ-панель**

📊 **Общая статистика:**
• Всего запросов: {stats['total_requests']}
• Всего пользователей: {len(stats['users'])}
• Активных сегодня: {len([u for u in users if datetime.fromisoformat(u['last_seen']) > datetime.now() - timedelta(days=1)])}

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

Выбери действие:
    """
    
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок админ-панели"""
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != ADMIN_ID:
        await query.message.reply_text("⛔ Нет доступа")
        return
    
    action = query.data
    
    if action == "admin_stats":
        await admin_stats_command(update, context)
    elif action == "admin_users":
        await admin_users_command(update, context)
    elif action == "admin_requests":
        await admin_requests_command(update, context)
    elif action == "admin_errors":
        await admin_errors_command(update, context)
    elif action == "admin_clear":
        await admin_clear_command(update, context)
    elif action == "admin_export":
        await admin_export_command(update, context)
    elif action == "admin_refresh":
        await admin_panel(update, context)

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику"""
    stats = get_stats()
    users = get_users()
    
    # Статистика по дням
    today = datetime.now().date()
    week_ago = today - timedelta(days=7)
    
    requests_today = 0
    requests_week = 0
    
    all_requests = get_requests(1000)
    for req in all_requests:
        try:
            req_date = datetime.fromisoformat(req['timestamp']).date()
            if req_date == today:
                requests_today += 1
            if req_date >= week_ago:
                requests_week += 1
        except:
            pass
    
    text = f"""
📊 **Детальная статистика**

📈 **Общая:**
• Всего запросов: {stats['total_requests']}
• Всего пользователей: {len(stats['users'])}
• Запросов сегодня: {requests_today}
• Запросов за неделю: {requests_week}

👤 **Топ пользователей:**
"""
    # Топ 5 пользователей
    top_users = sorted(stats['users'].items(), key=lambda x: x[1]['requests'], reverse=True)[:5]
    for i, (user_id, data) in enumerate(top_users, 1):
        name = data.get('full_name', data.get('username', f'User_{user_id}'))
        text += f"{i}. {name[:20]} — {data['requests']} запросов\n"
    
    # Кнопка назад
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список пользователей"""
    users = get_users()
    
    if not users:
        await query.message.edit_text("📭 Нет пользователей")
        return
    
    text = "👥 **Список пользователей:**\n\n"
    
    # Показываем последних 20 пользователей
    for user in users[-20:]:
        name = user['full_name'] or user['username'] or f"User_{user['user_id']}"
        last_seen = datetime.fromisoformat(user['last_seen']).strftime('%d.%m %H:%M')
        text += f"• {name[:25]} — {user['total_requests']} запросов, последний: {last_seen}\n"
    
    text += f"\nВсего: {len(users)} пользователей"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Экспорт пользователей", callback_data="admin_export_users")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает последние запросы"""
    requests = get_requests(20)
    
    if not requests:
        await query.message.edit_text("📭 Нет запросов")
        return
    
    text = "📝 **Последние запросы:**\n\n"
    
    for req in reversed(requests):
        try:
            timestamp = datetime.fromisoformat(req['timestamp']).strftime('%d.%m %H:%M')
            name = req['full_name'] or req['username'] or f"User_{req['user_id']}"
            prompt = req['prompt'][:40] + "..." if len(req['prompt']) > 40 else req['prompt']
            status_emoji = "✅" if req['status'] == 'success' else "❌"
            text += f"{status_emoji} [{timestamp}] {name[:15]} — {req['mode']}\n   «{prompt}»\n"
        except:
            continue
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Экспорт запросов", callback_data="admin_export_requests")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ошибки"""
    if not ERRORS_CSV.exists():
        await query.message.edit_text("📭 Нет ошибок")
        return
    
    try:
        errors = []
        with open(ERRORS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            errors = list(reader)
        
        if not errors:
            await query.message.edit_text("📭 Нет ошибок")
            return
        
        text = "⚠️ **Последние ошибки:**\n\n"
        for err in errors[-10:]:
            timestamp = datetime.fromisoformat(err['timestamp']).strftime('%d.%m %H:%M')
            name = err['full_name'] or err['username'] or f"User_{err['user_id']}"
            text += f"• [{timestamp}] {name[:15]} — {err['error_type']}\n  {err['error_message'][:50]}\n"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Скачать все ошибки", callback_data="admin_download_errors")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает временные файлы"""
    count = 0
    for file in TEMP_DIR.iterdir():
        if file.is_file():
            file.unlink()
            count += 1
    
    await query.message.edit_text(f"🗑️ Очищено {count} временных файлов")
    
    # Возвращаемся в админ-панель
    await asyncio.sleep(2)
    await admin_panel(update, context)

async def admin_export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт данных"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Экспорт статистики", callback_data="admin_export_stats")],
        [InlineKeyboardButton("👥 Экспорт пользователей", callback_data="admin_export_users")],
        [InlineKeyboardButton("📝 Экспорт запросов", callback_data="admin_export_requests")],
        [InlineKeyboardButton("⚠️ Экспорт ошибок", callback_data="admin_download_errors")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text("📤 **Экспорт данных**\n\nВыбери что экспортировать:", 
                                 parse_mode="Markdown", reply_markup=keyboard)

async def admin_export_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт пользователей в CSV"""
    if USERS_CSV.exists():
        with open(USERS_CSV, 'rb') as f:
            await query.message.reply_document(f, filename=f"users_{datetime.now().strftime('%Y%m%d')}.csv")
    else:
        await query.message.edit_text("📭 Нет данных")

async def admin_export_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт запросов в CSV"""
    if REQUESTS_CSV.exists():
        with open(REQUESTS_CSV, 'rb') as f:
            await query.message.reply_document(f, filename=f"requests_{datetime.now().strftime('%Y%m%d')}.csv")
    else:
        await query.message.edit_text("📭 Нет данных")

async def admin_download_errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скачивает файл с ошибками"""
    if ERRORS_CSV.exists():
        with open(ERRORS_CSV, 'rb') as f:
            await query.message.reply_document(f, filename=f"errors_{datetime.now().strftime('%Y%m%d')}.csv")
    else:
        await query.message.edit_text("📭 Нет ошибок")

async def admin_export_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт статистики в JSON"""
    stats = get_stats()
    stats['export_time'] = datetime.now().isoformat()
    
    # Создаем временный файл
    temp_file = TEMP_DIR / f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    
    with open(temp_file, 'rb') as f:
        await query.message.reply_document(f, filename=f"stats_{datetime.now().strftime('%Y%m%d')}.json")
    
    temp_file.unlink(missing_ok=True)

async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возврат в админ-панель"""
    await admin_panel(update, context)

# ========== ФУНКЦИИ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_info = get_user_info(user)
    log_user(user_info)

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

🎨 **Размер изображения** определяется автоматически!
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
            "📤 Отправь фото:\n\n"
            "🔄 Размер изображения сохранится автоматически"
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
            "🔄 Размер изображения сохранится автоматически\n\n"
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
            log_request(user_info, mode_name, prompt, 0, 'error')
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
                    caption=f"✅ **Готово!**\n\n⏱️ {processing_time} сек.\n\n🔄 Размер сохранен автоматически\n\n📤 Отправь /start для новой обработки"
                )

            output_path.unlink(missing_ok=True)
            await status_msg.delete()
            
            # Логируем успешный запрос
            log_request(user_info, mode_name, prompt, processing_time, 'success')
        else:
            await status_msg.edit_text("❌ Ошибка ИИ. Попробуйте другое фото")
            log_error(user_info, "ai_failed", f"Agnes API returned no result")
            log_request(user_info, mode_name, prompt, int(time.time() - start_time), 'error')

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        log_error(user_info, "processing_error", str(e))
        log_request(user_info, mode_name, prompt, int(time.time() - start_time), 'error')

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
            log_request(user_info, "Свой запрос", custom_prompt, 0, 'error')
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
                    caption=f"✅ **Готово!**\n\n📝 Запрос: «{custom_prompt[:80]}»\n⏱️ {processing_time} сек.\n\n🔄 Размер сохранен автоматически"
                )

            output_path.unlink(missing_ok=True)
            await status_msg.delete()
            context.user_data['custom_prompt'] = None
            
            # Логируем успешный запрос
            log_request(user_info, "Свой запрос", custom_prompt, processing_time, 'success')
        else:
            await status_msg.edit_text("❌ Ошибка ИИ")
            log_request(user_info, "Свой запрос", custom_prompt, int(time.time() - start_time), 'error')

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        log_request(user_info, "Свой запрос", custom_prompt, int(time.time() - start_time), 'error')

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
            f"✅ **Запрос сохранен!**\n\n📝 «{custom_prompt}»\n\n📤 Теперь отправь фото:\n\n🔄 Размер сохранится автоматически"
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Отправь /start")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Справка:**\n\n"
        "/start — главное меню\n"
        "/cancel — отмена\n"
        "/admin — админ-панель (только для администратора)\n\n"
        "**Режимы:**\n"
        "🧹 Убрать тени — белый фон + студийный свет\n"
        "✏️ Свой запрос — напиши что хочешь\n\n"
        "🔄 Размер изображения определяется автоматически",
        parse_mode="Markdown"
    )

# ========== ЗАПУСК ==========
def main():
    import asyncio
    
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
    app.add_handler(CommandHandler("admin", admin_panel))

    # Обработчики админ-панели
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(mode_handler, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(mode_handler, pattern="^cancel$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_prompt))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("=" * 50)
    print("✅ Бот запущен!")
    print("🎨 Режимы: Убрать тени | Свой запрос")
    print("🔄 Размер изображения: автоматический")
    print(f"👑 Админ ID: {ADMIN_ID}")
    print("📊 /admin - админ-панель")
    print("=" * 50)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
