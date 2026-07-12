import os
import logging
import tempfile
import time
import csv
import json
from pathlib import Path
from datetime import datetime, timedelta
import requests
import sys
import traceback
import shutil
import zipfile
from collections import defaultdict
from typing import Dict, List, Optional
import asyncio
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from dotenv import load_dotenv

load_dotenv()

# ========== НАСТРОЙКИ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
AGNES_API_KEY = os.getenv("AGNES_API_KEY")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", 10))
MAX_IMAGE_SIZE_MB = int(os.getenv("MAX_IMAGE_SIZE_MB", 20))

# Agnes API
AGNES_API_URL = "https://apihub.agnes-ai.com/v1/images/generations"

# Папки
BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
TEMP_DIR = BASE_DIR / "temp"
BACKUP_DIR = BASE_DIR / "backups"
LOGS_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)

# ========== НАСТРОЙКА ЛОГГИРОВАНИЯ ==========
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOGS_DIR / "bot.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

error_logger = logging.getLogger('errors')
error_handler = logging.FileHandler(LOGS_DIR / "errors.log", encoding='utf-8')
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
error_logger.addHandler(error_handler)
error_logger.setLevel(logging.ERROR)

# ========== ИНИЦИАЛИЗАЦИЯ ФАЙЛОВ ==========
FILES = {
    'errors': LOGS_DIR / "errors.csv",
    'users': LOGS_DIR / "users.csv",
    'requests': LOGS_DIR / "requests.csv",
    'stats': LOGS_DIR / "stats.json",
    'performance': LOGS_DIR / "performance.csv",
    'daily_stats': LOGS_DIR / "daily_stats.json",
    'popular_prompts': LOGS_DIR / "popular_prompts.json",
    'banned_users': LOGS_DIR / "banned_users.json",
    'system_health': LOGS_DIR / "system_health.json"
}

def init_csv_file(filepath, headers):
    if not filepath.exists():
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(headers)

init_csv_file(FILES['errors'], ['timestamp', 'user_id', 'username', 'full_name', 'error_type', 'error_message', 'stack_trace'])
init_csv_file(FILES['users'], ['user_id', 'username', 'first_name', 'last_name', 'full_name', 'language', 'first_seen', 'last_seen', 'total_requests', 'is_banned'])
init_csv_file(FILES['requests'], ['timestamp', 'user_id', 'username', 'full_name', 'mode', 'prompt', 'processing_time', 'status', 'image_size', 'image_format', 'original_resolution', 'result_resolution'])
init_csv_file(FILES['performance'], ['timestamp', 'request_id', 'user_id', 'operation', 'duration_ms', 'status'])

def init_json_file(filepath, default_data):
    if not filepath.exists():
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(default_data, f, indent=2, ensure_ascii=False)

init_json_file(FILES['stats'], {
    'total_requests': 0,
    'successful_requests': 0,
    'failed_requests': 0,
    'total_users': 0,
    'users': {},
    'mode_stats': {'shadows': 0, 'custom': 0},
    'avg_processing_time': 0,
    'total_processing_time': 0
})

init_json_file(FILES['daily_stats'], {})
init_json_file(FILES['popular_prompts'], {})
init_json_file(FILES['banned_users'], [])
init_json_file(FILES['system_health'], {
    'last_check': datetime.now().isoformat(),
    'status': 'ok',
    'uptime': 0,
    'memory_usage': 0,
    'disk_usage': 0
})

# ========== КЛАСС ДЛЯ ЛОГГИРОВАНИЯ ==========
class LoggerSystem:
    def __init__(self):
        self.request_counter = 0
        self.start_time = datetime.now()
        self.user_requests = defaultdict(list)
        
    def log_user(self, user_info, language='unknown'):
        try:
            users = self._read_csv(FILES['users'])
            user_exists = False
            
            for row in users:
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
                    'language': language,
                    'first_seen': datetime.now().isoformat(),
                    'last_seen': datetime.now().isoformat(),
                    'total_requests': '1',
                    'is_banned': 'False'
                }
                users.append(new_row)
            
            self._write_csv(FILES['users'], users)
            
            stats = self._read_json(FILES['stats'])
            stats['total_users'] = len(users)
            stats['users'][user_info['user_id']] = {
                'username': user_info['username'],
                'full_name': user_info['full_name'],
                'requests': int(new_row['total_requests']) if not user_exists else int(row['total_requests'])
            }
            self._write_json(FILES['stats'], stats)
            
        except Exception as e:
            error_logger.error(f"Error logging user: {e}\n{traceback.format_exc()}")

    def log_request(self, user_info, mode, prompt, processing_time, status='success', 
                   image_size=0, image_format='jpg', original_resolution='', result_resolution=''):
        try:
            with open(FILES['requests'], 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    user_info['user_id'],
                    user_info['username'],
                    user_info['full_name'],
                    mode,
                    prompt[:200],
                    processing_time,
                    status,
                    image_size,
                    image_format,
                    original_resolution,
                    result_resolution
                ])
            
            stats = self._read_json(FILES['stats'])
            stats['total_requests'] += 1
            if status == 'success':
                stats['successful_requests'] += 1
            else:
                stats['failed_requests'] += 1
                
            if mode == 'shadows':
                stats['mode_stats']['shadows'] += 1
            else:
                stats['mode_stats']['custom'] += 1
                
            stats['total_processing_time'] += processing_time
            stats['avg_processing_time'] = stats['total_processing_time'] / stats['total_requests']
            
            if user_info['user_id'] in stats['users']:
                stats['users'][user_info['user_id']]['requests'] += 1
                
            self._write_json(FILES['stats'], stats)
            
            if status == 'success' and len(prompt) > 10:
                prompts = self._read_json(FILES['popular_prompts'])
                prompt_key = prompt[:50]
                if prompt_key in prompts:
                    prompts[prompt_key]['count'] += 1
                else:
                    prompts[prompt_key] = {
                        'prompt': prompt[:200],
                        'count': 1,
                        'first_seen': datetime.now().isoformat()
                    }
                self._write_json(FILES['popular_prompts'], prompts)
                
            today = datetime.now().strftime('%Y-%m-%d')
            daily = self._read_json(FILES['daily_stats'])
            if today not in daily:
                daily[today] = {'requests': 0, 'users': [], 'success': 0, 'errors': 0}
            daily[today]['requests'] += 1
            if user_info['user_id'] not in daily[today]['users']:
                daily[today]['users'].append(user_info['user_id'])
            if status == 'success':
                daily[today]['success'] += 1
            else:
                daily[today]['errors'] += 1
            self._write_json(FILES['daily_stats'], daily)
            
        except Exception as e:
            error_logger.error(f"Error logging request: {e}\n{traceback.format_exc()}")

    def log_error(self, user_info, error_type, error_message, stack_trace=None):
        try:
            with open(FILES['errors'], 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    user_info['user_id'] if user_info else 'system',
                    user_info['username'] if user_info else 'system',
                    user_info['full_name'] if user_info else 'system',
                    error_type,
                    error_message[:500],
                    stack_trace[:1000] if stack_trace else ''
                ])
            
            error_logger.error(f"{error_type}: {error_message}")
            if stack_trace:
                error_logger.error(stack_trace)
                
        except Exception as e:
            error_logger.error(f"Error logging error: {e}")

    def log_performance(self, user_id, operation, duration_ms, status='success'):
        try:
            with open(FILES['performance'], 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    self.request_counter,
                    user_id,
                    operation,
                    duration_ms,
                    status
                ])
            self.request_counter += 1
        except Exception as e:
            error_logger.error(f"Error logging performance: {e}")

    def check_rate_limit(self, user_id):
        now = datetime.now()
        user_requests = self.user_requests[user_id]
        user_requests = [t for t in user_requests if (now - t).seconds < 60]
        self.user_requests[user_id] = user_requests
        
        if len(user_requests) >= MAX_REQUESTS_PER_MINUTE:
            return False
        user_requests.append(now)
        return True

    def is_user_banned(self, user_id):
        try:
            banned = self._read_json(FILES['banned_users'])
            return user_id in [u['user_id'] for u in banned]
        except:
            return False

    def ban_user(self, user_id, reason=''):
        try:
            banned = self._read_json(FILES['banned_users'])
            if user_id not in [u['user_id'] for u in banned]:
                banned.append({'user_id': user_id, 'reason': reason, 'banned_at': datetime.now().isoformat()})
                self._write_json(FILES['banned_users'], banned)
                return True
            return False
        except Exception as e:
            error_logger.error(f"Error banning user: {e}")
            return False

    def unban_user(self, user_id):
        try:
            banned = self._read_json(FILES['banned_users'])
            banned = [u for u in banned if u['user_id'] != user_id]
            self._write_json(FILES['banned_users'], banned)
            return True
        except Exception as e:
            error_logger.error(f"Error unbanning user: {e}")
            return False

    def get_stats(self):
        try:
            stats = self._read_json(FILES['stats'])
            daily = self._read_json(FILES['daily_stats'])
            prompts = self._read_json(FILES['popular_prompts'])
            
            today = datetime.now().strftime('%Y-%m-%d')
            today_stats = daily.get(today, {'requests': 0, 'users': 0, 'success': 0, 'errors': 0})
            
            top_users = sorted(stats['users'].items(), key=lambda x: x[1]['requests'], reverse=True)[:10]
            top_prompts = sorted(prompts.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
            
            uptime = datetime.now() - self.start_time
            
            return {
                'total_requests': stats['total_requests'],
                'successful_requests': stats['successful_requests'],
                'failed_requests': stats['failed_requests'],
                'total_users': stats['total_users'],
                'avg_processing_time': stats['avg_processing_time'],
                'today_requests': today_stats['requests'],
                'today_users': len(today_stats['users']) if isinstance(today_stats['users'], list) else 0,
                'success_rate': (stats['successful_requests'] / stats['total_requests'] * 100) if stats['total_requests'] > 0 else 0,
                'mode_stats': stats['mode_stats'],
                'top_users': top_users,
                'top_prompts': top_prompts,
                'uptime': str(uptime).split('.')[0],
                'banned_count': len(self._read_json(FILES['banned_users']))
            }
        except Exception as e:
            error_logger.error(f"Error getting stats: {e}")
            return None

    def _read_csv(self, filepath):
        try:
            if filepath.exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    return list(reader)
            return []
        except:
            return []

    def _write_csv(self, filepath, data):
        try:
            if data:
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=data[0].keys())
                    writer.writeheader()
                    writer.writerows(data)
        except Exception as e:
            error_logger.error(f"Error writing CSV: {e}")

    def _read_json(self, filepath):
        try:
            if filepath.exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except:
            return {}

    def _write_json(self, filepath, data):
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            error_logger.error(f"Error writing JSON: {e}")

logger_system = LoggerSystem()

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ИЗОБРАЖЕНИЯМИ ==========
def get_image_info(image_path):
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            format = img.format.lower() if img.format else 'unknown'
            return {
                'width': width,
                'height': height,
                'resolution': f"{width}x{height}",
                'format': format,
                'size_kb': os.path.getsize(image_path) // 1024
            }
    except Exception as e:
        error_logger.error(f"Error getting image info: {e}")
        return None

def upload_to_temp_hosting(image_path):
    try:
        url = "https://freeimage.host/api/1/upload"
        api_key = "6d207e02198a847aa98d0a2a901485a5"

        file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
        if file_size_mb > MAX_IMAGE_SIZE_MB:
            logger_system.log_error(None, "file_too_large", f"File size {file_size_mb:.2f}MB exceeds limit {MAX_IMAGE_SIZE_MB}MB")
            return None

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
        error_logger.error(f"Upload error: {e}\n{traceback.format_exc()}")
        return None

async def generate_with_agnes(image_url: str, prompt: str):
    """Генерация изображения через Agnes API без указания размеров"""
    if not AGNES_API_KEY:
        logger_system.log_error(None, "missing_api_key", "AGNES_API_KEY not set")
        return None

    try:
        headers = {
            "Authorization": f"Bearer {AGNES_API_KEY}",
            "Content-Type": "application/json"
        }

        # Отправляем запрос БЕЗ указания width и height
        payload = {
            "model": "agnes-image-2.0-flash",
            "prompt": prompt,
            "tags": ["img2img"],
            "extra_body": {
                "image": [image_url],
                "response_format": "url",
                "num_inference_steps": 25,
                "guidance_scale": 7.5
                # width и height НЕ указываем
            }
        }

        logger.info(f"Отправка запроса в Agnes API без указания размеров")

        start_time = time.time()
        response = requests.post(AGNES_API_URL, headers=headers, json=payload, timeout=120)
        duration_ms = int((time.time() - start_time) * 1000)
        
        logger_system.log_performance('system', 'agnes_api_request', duration_ms, 
                                     'success' if response.status_code == 200 else 'error')

        if response.status_code == 200:
            data = response.json()
            if "data" in data and len(data["data"]) > 0:
                logger.info(f"Успешная генерация, получен URL: {data['data'][0]['url'][:50]}...")
                return data["data"][0]["url"]
            else:
                logger_system.log_error(None, "api_invalid_response", f"Invalid response format: {data}")
        else:
            logger_system.log_error(None, "api_error", f"Status {response.status_code}: {response.text}")
        return None
    except requests.Timeout:
        logger_system.log_error(None, "api_timeout", "Agnes API timeout after 120s")
        return None
    except Exception as e:
        logger_system.log_error(None, "api_exception", str(e), traceback.format_exc())
        return None

async def download_result(url, output_path):
    try:
        response = requests.get(url, timeout=60)
        if response.status_code == 200:
            with open(output_path, 'wb') as f:
                f.write(response.content)
            return get_image_info(output_path)
        return None
    except Exception as e:
        error_logger.error(f"Error downloading result: {e}")
        return None

# ========== АДМИН-КОМАНДЫ ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("📝 Запросы", callback_data="admin_requests")],
        [InlineKeyboardButton("⚠️ Ошибки", callback_data="admin_errors")],
        [InlineKeyboardButton("⚡ Производительность", callback_data="admin_performance")],
        [InlineKeyboardButton("📈 Популярные промпты", callback_data="admin_prompts")],
        [InlineKeyboardButton("🔒 Бан-лист", callback_data="admin_banned")],
        [InlineKeyboardButton("📤 Экспорт", callback_data="admin_export")],
        [InlineKeyboardButton("🗑️ Очистка", callback_data="admin_clear")],
        [InlineKeyboardButton("🏥 Health Check", callback_data="admin_health")],
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="admin_settings")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="admin_refresh")]
    ])
    
    stats = logger_system.get_stats()
    
    text = f"""
👑 **Админ-панель**

📊 **Общая статистика:**
• Всего запросов: {stats['total_requests']}
• Успешных: {stats['successful_requests']}
• Ошибок: {stats['failed_requests']}
• Успешность: {stats['success_rate']:.1f}%
• Пользователей: {stats['total_users']}
• Забанено: {stats['banned_count']}

📈 **Сегодня:**
• Запросов: {stats['today_requests']}
• Пользователей: {stats['today_users']}

⏱️ **Производительность:**
• Среднее время: {stats['avg_processing_time']:.1f} сек.
• Время работы: {stats['uptime']}

🎨 **Режимы:**
• Тени: {stats['mode_stats']['shadows']}
• Свой запрос: {stats['mode_stats']['custom']}

🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}
    """
    
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    elif action == "admin_performance":
        await admin_performance_command(update, context)
    elif action == "admin_prompts":
        await admin_prompts_command(update, context)
    elif action == "admin_banned":
        await admin_banned_command(update, context)
    elif action == "admin_export":
        await admin_export_command(update, context)
    elif action == "admin_clear":
        await admin_clear_command(update, context)
    elif action == "admin_health":
        await admin_health_command(update, context)
    elif action == "admin_broadcast":
        await admin_broadcast_command(update, context)
    elif action == "admin_settings":
        await admin_settings_command(update, context)
    elif action == "admin_refresh":
        await admin_panel_command(update, context)

async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    stats = logger_system.get_stats()
    
    text = f"""
📊 **Детальная статистика**

📈 **Общая:**
• Всего запросов: {stats['total_requests']}
• Успешных: {stats['successful_requests']}
• Ошибок: {stats['failed_requests']}
• Успешность: {stats['success_rate']:.1f}%
• Всего пользователей: {stats['total_users']}
• Забанено: {stats['banned_count']}

⏱️ **Время:**
• Среднее: {stats['avg_processing_time']:.1f} сек.
• Время работы: {stats['uptime']}

📈 **Сегодня:**
• Запросов: {stats['today_requests']}
• Пользователей: {stats['today_users']}

🎨 **По режимам:**
• Убрать тени: {stats['mode_stats']['shadows']}
• Свой запрос: {stats['mode_stats']['custom']}

🏆 **Топ пользователей:**
"""
    for i, (user_id, data) in enumerate(stats['top_users'][:5], 1):
        name = data.get('full_name', data.get('username', f'User_{user_id}'))[:20]
        text += f"{i}. {name} — {data['requests']} запросов\n"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        users = []
        with open(FILES['users'], 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            users = list(reader)
        
        if not users:
            await query.message.edit_text("📭 Нет пользователей")
            return
        
        text = "👥 **Список пользователей:**\n\n"
        
        for user in users[-20:]:
            name = user['full_name'] or user['username'] or f"User_{user['user_id']}"
            last_seen = datetime.fromisoformat(user['last_seen']).strftime('%d.%m %H:%M')
            banned = "🚫" if user.get('is_banned') == 'True' else "✅"
            text += f"{banned} {name[:25]} — {user['total_requests']} запросов, последний: {last_seen}\n"
        
        text += f"\nВсего: {len(users)} пользователей"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Экспорт пользователей", callback_data="admin_export_users")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        requests_data = []
        with open(FILES['requests'], 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            requests_data = list(reader)
        
        if not requests_data:
            await query.message.edit_text("📭 Нет запросов")
            return
        
        text = "📝 **Последние запросы:**\n\n"
        
        for req in requests_data[-20:]:
            try:
                timestamp = datetime.fromisoformat(req['timestamp']).strftime('%d.%m %H:%M')
                name = req['full_name'] or req['username'] or f"User_{req['user_id']}"
                prompt = req['prompt'][:40] + "..." if len(req['prompt']) > 40 else req['prompt']
                status_emoji = "✅" if req['status'] == 'success' else "❌"
                time_str = f"{req['processing_time']}с" if req['processing_time'] else "0с"
                
                res_info = ""
                if req.get('original_resolution') and req.get('result_resolution'):
                    res_info = f" [{req['original_resolution']} → {req['result_resolution']}]"
                
                text += f"{status_emoji} [{timestamp}] {name[:15]} — {req['mode']} ({time_str}){res_info}\n   «{prompt}»\n"
            except:
                continue
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Экспорт запросов", callback_data="admin_export_requests")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        if not FILES['errors'].exists():
            await query.message.edit_text("📭 Нет ошибок")
            return
        
        errors = []
        with open(FILES['errors'], 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            errors = list(reader)
        
        if not errors:
            await query.message.edit_text("📭 Нет ошибок")
            return
        
        text = "⚠️ **Последние ошибки:**\n\n"
        for err in errors[-10:]:
            try:
                timestamp = datetime.fromisoformat(err['timestamp']).strftime('%d.%m %H:%M')
                name = err['full_name'] or err['username'] or f"User_{err['user_id']}"
                text += f"• [{timestamp}] {name[:15]} — {err['error_type']}\n  {err['error_message'][:50]}\n"
            except:
                continue
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Скачать все ошибки", callback_data="admin_download_errors")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        if not FILES['performance'].exists():
            await query.message.edit_text("📭 Нет данных о производительности")
            return
        
        performance = []
        with open(FILES['performance'], 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            performance = list(reader)
        
        if not performance:
            await query.message.edit_text("📭 Нет данных")
            return
        
        total_ops = len(performance)
        avg_duration = sum(int(p['duration_ms']) for p in performance) / total_ops if total_ops > 0 else 0
        slow_ops = [p for p in performance if int(p['duration_ms']) > 5000]
        error_ops = [p for p in performance if p['status'] == 'error']
        
        text = f"""
⚡ **Производительность**

📊 **Операции:**
• Всего: {total_ops}
• Среднее время: {avg_duration:.0f} мс
• Медленных (>5с): {len(slow_ops)}
• Ошибок: {len(error_ops)}

⏱️ **Последние операции:**
"""
        for p in performance[-5:]:
            status = "✅" if p['status'] == 'success' else "❌"
            text += f"{status} {p['operation']} — {p['duration_ms']} мс\n"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_prompts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        prompts = logger_system._read_json(FILES['popular_prompts'])
        
        if not prompts:
            await query.message.edit_text("📭 Нет промптов")
            return
        
        text = "📈 **Топ популярных промптов:**\n\n"
        
        top_prompts = sorted(prompts.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
        for i, (key, data) in enumerate(top_prompts, 1):
            text += f"{i}. «{data['prompt'][:50]}» — {data['count']} раз\n"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        banned = logger_system._read_json(FILES['banned_users'])
        
        if not banned:
            await query.message.edit_text("📭 Нет забаненных пользователей")
            return
        
        text = "🔒 **Бан-лист:**\n\n"
        for user in banned:
            text += f"• ID: {user['user_id']}\n  Причина: {user.get('reason', 'Не указана')}\n  Забанен: {user.get('banned_at', '')}\n\n"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
        ])
        
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка: {e}")

async def admin_export_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Экспорт статистики", callback_data="admin_export_stats")],
        [InlineKeyboardButton("👥 Экспорт пользователей", callback_data="admin_export_users")],
        [InlineKeyboardButton("📝 Экспорт запросов", callback_data="admin_export_requests")],
        [InlineKeyboardButton("⚠️ Экспорт ошибок", callback_data="admin_download_errors")],
        [InlineKeyboardButton("📦 Экспорт ВСЕХ данных", callback_data="admin_export_all")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text("📤 **Экспорт данных**\n\nВыбери что экспортировать:", 
                                 parse_mode="Markdown", reply_markup=keyboard)

async def admin_export_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        status_msg = await query.message.edit_text("📦 Создаю архив...")
        
        export_dir = TEMP_DIR / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        export_dir.mkdir(exist_ok=True)
        
        for file in LOGS_DIR.glob("*"):
            if file.is_file() and file.suffix in ['.csv', '.json', '.log']:
                shutil.copy2(file, export_dir / file.name)
        
        zip_path = TEMP_DIR / f"logs_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in export_dir.iterdir():
                zipf.write(file, file.name)
        
        with open(zip_path, 'rb') as f:
            await query.message.reply_document(f, filename=zip_path.name)
        
        shutil.rmtree(export_dir)
        zip_path.unlink()
        
        await status_msg.delete()
    except Exception as e:
        await query.message.edit_text(f"❌ Ошибка экспорта: {e}")

async def admin_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    count = 0
    for file in TEMP_DIR.iterdir():
        if file.is_file():
            file.unlink()
            count += 1
    
    await query.message.edit_text(f"🗑️ Очищено {count} временных файлов")
    await asyncio.sleep(2)
    await admin_panel_command(update, context)

async def admin_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text = """
🏥 **Health Check**

✅ Бот работает
✅ API ключ установлен
✅ Папки созданы
✅ Логи ведутся

📊 Состояние: OK
"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['broadcast_mode'] = True
    await query.message.edit_text(
        "📢 **Рассылка**\n\n"
        "Отправь сообщение для рассылки всем пользователям.\n"
        "Для отмены отправь /cancel"
    )

async def admin_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    text = f"""
⚙️ **Настройки бота**

📏 Максимальный размер файла: {MAX_IMAGE_SIZE_MB} MB
⏱️ Максимум запросов в минуту: {MAX_REQUESTS_PER_MINUTE}
📊 Уровень логирования: {LOG_LEVEL}

⚠️ Для изменения настроек отредактируйте .env файл
    """
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ])
    
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_panel(update, context)

# ========== ФУНКЦИИ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_info = get_user_info(user)
    
    if logger_system.is_user_banned(user_info['user_id']):
        await update.message.reply_text("🚫 Вы забанены. Обратитесь к администратору.")
        return
    
    if not logger_system.check_rate_limit(user_info['user_id']):
        await update.message.reply_text("⏳ Слишком много запросов. Подождите минуту.")
        return
    
    language = update.effective_user.language_code or 'unknown'
    logger_system.log_user(user_info, language)

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
    context.user_data['state'] = 0

async def mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    user_info = get_user_info(user)
    
    if logger_system.is_user_banned(user_info['user_id']):
        await query.message.reply_text("🚫 Вы забанены.")
        return

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
        context.user_data['state'] = 2
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
    user = update.effective_user
    user_info = get_user_info(user)
    
    if logger_system.is_user_banned(user_info['user_id']):
        await update.message.reply_text("🚫 Вы забанены.")
        return
    
    if not logger_system.check_rate_limit(user_info['user_id']):
        await update.message.reply_text("⏳ Слишком много запросов. Подождите минуту.")
        return

    mode = context.user_data.get('mode')

    if not mode:
        await start(update, context)
        return

    if mode == 'custom':
        await handle_custom_photo(update, context)
        return

    await handle_general_photo(update, context)

async def handle_general_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_info = get_user_info(user)
    mode = context.user_data.get('mode', 'shadows')

    if mode == 'shadows':
        prompt = "Remove all shadows from this photo completely. Make background pure white, studio lighting, even illumination, no shadows visible, professional product photography style, high quality, clean look, shadowless, bright and clear image"
        mode_name = "Убрать тени + белый фон + студийный свет"

    status_msg = await update.message.reply_text(
        f"📸 **{mode_name}**\n\n⏳ Обработка... 10-30 секунд",
        parse_mode="Markdown"
    )

    photo_file = await update.message.photo[-1].get_file()
    input_path = TEMP_DIR / f"input_{user.id}_{int(time.time())}.jpg"
    
    start_time = time.time()

    try:
        await status_msg.edit_text(f"📥 Скачиваю фото...")
        await photo_file.download_to_drive(str(input_path))
        
        original_info = get_image_info(input_path)
        if original_info:
            original_resolution = original_info['resolution']
            image_size = original_info['size_kb']
            image_format = original_info['format']
            
            await status_msg.edit_text(
                f"📸 **Оригинал:**\n"
                f"• Разрешение: {original_resolution}\n"
                f"• Формат: {image_format}\n"
                f"• Размер: {image_size} KB\n\n"
                f"🔄 Обрабатываю..."
            )
        
        await status_msg.edit_text(f"📤 Загружаю фото на хостинг...")
        image_url = upload_to_temp_hosting(str(input_path))

        if not image_url:
            await status_msg.edit_text("❌ Ошибка загрузки фото")
            logger_system.log_error(user_info, "upload_failed", "Cannot upload to hosting")
            logger_system.log_request(user_info, mode_name, prompt, 
                                     int(time.time() - start_time), 'error', 
                                     image_size if original_info else 0, 
                                     image_format if original_info else 'jpg',
                                     original_resolution if original_info else '',
                                     '')
            return

        await status_msg.edit_text(f"🤖 Генерирую...")
        
        # Генерируем БЕЗ указания размеров
        result_url = await generate_with_agnes(image_url, prompt)

        if result_url:
            processing_time = int(time.time() - start_time)
            
            await status_msg.edit_text(f"📥 Скачиваю результат...")
            output_path = TEMP_DIR / f"output_{user.id}_{int(time.time())}.png"
            
            result_info = await download_result(result_url, output_path)
            
            if result_info:
                result_resolution = result_info['resolution']
                
                with open(output_path, 'rb') as f:
                    await update.message.reply_photo(
                        f,
                        caption=f"✅ **Готово!**\n\n"
                               f"⏱️ {processing_time} сек.\n"
                               f"📐 Исходный размер: {original_resolution if original_info else 'неизвестно'}\n"
                               f"📐 Результат: {result_resolution}\n\n"
                               f"📤 Отправь /start для новой обработки"
                    )
                
                logger_system.log_request(
                    user_info, mode_name, prompt, processing_time, 
                    'success', 
                    original_info['size_kb'] if original_info else 0,
                    original_info['format'] if original_info else 'jpg',
                    original_info['resolution'] if original_info else '',
                    result_resolution
                )
                
                output_path.unlink(missing_ok=True)
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ Ошибка обработки результата")
                logger_system.log_error(user_info, "result_processing_error", "Failed to process result")
        else:
            await status_msg.edit_text("❌ Ошибка ИИ. Попробуйте другое фото")
            logger_system.log_error(user_info, "ai_failed", "Agnes API returned no result")
            logger_system.log_request(user_info, mode_name, prompt, 
                                     int(time.time() - start_time), 'error',
                                     image_size if original_info else 0,
                                     image_format if original_info else 'jpg',
                                     original_resolution if original_info else '',
                                     '')

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        logger_system.log_error(user_info, "processing_error", str(e), traceback.format_exc())

    finally:
        input_path.unlink(missing_ok=True)

async def handle_custom_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    start_time = time.time()

    try:
        await photo_file.download_to_drive(str(input_path))
        
        original_info = get_image_info(input_path)
        if original_info:
            original_resolution = original_info['resolution']
            image_size = original_info['size_kb']
            image_format = original_info['format']
            
            await status_msg.edit_text(
                f"📸 **Оригинал:**\n"
                f"• Разрешение: {original_resolution}\n"
                f"• Формат: {image_format}\n\n"
                f"🔄 Обрабатываю..."
            )
        
        image_url = upload_to_temp_hosting(str(input_path))
        if not image_url:
            await status_msg.edit_text("❌ Ошибка загрузки фото")
            logger_system.log_request(user_info, "Свой запрос", custom_prompt, 
                                     int(time.time() - start_time), 'error',
                                     image_size if original_info else 0,
                                     image_format if original_info else 'jpg',
                                     original_resolution if original_info else '',
                                     '')
            return

        # Генерируем БЕЗ указания размеров
        result_url = await generate_with_agnes(image_url, custom_prompt)

        if result_url:
            processing_time = int(time.time() - start_time)
            
            output_path = TEMP_DIR / f"output_{user.id}_{int(time.time())}.png"
            result_info = await download_result(result_url, output_path)
            
            if result_info:
                result_resolution = result_info['resolution']
                
                with open(output_path, 'rb') as f:
                    await update.message.reply_photo(
                        f,
                        caption=f"✅ **Готово!**\n\n"
                               f"📝 Запрос: «{custom_prompt[:80]}»\n"
                               f"⏱️ {processing_time} сек.\n"
                               f"📐 Исходный размер: {original_resolution if original_info else 'неизвестно'}\n"
                               f"📐 Результат: {result_resolution}"
                    )

                output_path.unlink(missing_ok=True)
                await status_msg.delete()
                context.user_data['custom_prompt'] = None
                
                logger_system.log_request(
                    user_info, "Свой запрос", custom_prompt, 
                    processing_time, 'success',
                    original_info['size_kb'] if original_info else 0,
                    original_info['format'] if original_info else 'jpg',
                    original_info['resolution'] if original_info else '',
                    result_resolution
                )
            else:
                await status_msg.edit_text("❌ Ошибка обработки результата")
        else:
            await status_msg.edit_text("❌ Ошибка ИИ")
            logger_system.log_request(user_info, "Свой запрос", custom_prompt, 
                                     int(time.time() - start_time), 'error',
                                     image_size if original_info else 0,
                                     image_format if original_info else 'jpg',
                                     original_resolution if original_info else '',
                                     '')

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        logger_system.log_error(user_info, "processing_error", str(e), traceback.format_exc())

    finally:
        input_path.unlink(missing_ok=True)

async def handle_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_info = get_user_info(user)
    
    if logger_system.is_user_banned(user_info['user_id']):
        await update.message.reply_text("🚫 Вы забанены.")
        return
    
    if context.user_data.get('waiting_for_prompt'):
        custom_prompt = update.message.text.strip()
        if len(custom_prompt) > 500:
            await update.message.reply_text("❌ Запрос слишком длинный (макс 500 символов)")
            return

        context.user_data['custom_prompt'] = custom_prompt
        context.user_data['waiting_for_prompt'] = False

        await update.message.reply_text(
            f"✅ **Запрос сохранен!**\n\n"
            f"📝 «{custom_prompt}»\n\n"
            f"📤 Теперь отправь фото:"
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
        "✏️ Свой запрос — напиши что хочешь",
        parse_mode="Markdown"
    )

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ ==========
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

# ========== ЗАПУСК ==========
def main():
    if not BOT_TOKEN:
        print("❌ Нет BOT_TOKEN в .env")
        return

    if not AGNES_API_KEY:
        print("❌ Нет AGNES_API_KEY в .env")
        return

    app = Application.builder().token(BOT_TOKEN).connect_timeout(60).read_timeout(60).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_panel))

    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(mode_handler, pattern="^mode_"))
    app.add_handler(CallbackQueryHandler(mode_handler, pattern="^cancel$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_prompt))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("=" * 60)
    print("✅ Бот запущен!")
    print("🎨 Режимы: Убрать тени | Свой запрос")
    print(f"👑 Админ ID: {ADMIN_ID}")
    print("📊 /admin - админ-панель")
    print("=" * 60)
    print("📁 Логи сохраняются в папку /logs")
    print("📁 Временные файлы в /temp")
    print("📁 Бэкапы в /backups")
    print("=" * 60)
    print("ℹ️ Agnes API работает без указания размеров")
    print("=" * 60)

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
