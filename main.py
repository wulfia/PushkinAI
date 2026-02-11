import os
import re
import sys
import time
import subprocess
import shutil
import atexit
try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None
try:
    import fcntl  # Linux/macOS
except ImportError:
    fcntl = None
from openai import OpenAI
import telebot
from dotenv import load_dotenv
import requests
from io import BytesIO
import json
import mimetypes
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Загружаем переменные окружения из файла .env
load_dotenv('data.env')

# Получаем токены из переменных окружения
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HUGGINGFACE_TOKEN = os.getenv('HUGGINGFACE_TOKEN')
# ID администратора (укажите свой Telegram ID)
ADMIN_ID = os.getenv('ADMIN_ID') 
MINI_APP_URL = os.getenv('MINI_APP_URL', '').strip()
MINI_APP_HOST = os.getenv('MINI_APP_HOST', '0.0.0.0').strip()
MINI_APP_PORT = int(os.getenv('PORT', os.getenv('MINI_APP_PORT', '8080')))
MINI_APP_ENABLED = os.getenv('MINI_APP_ENABLED', '1') == '1'
MINI_APP_AUTO_TUNNEL = os.getenv('MINI_APP_AUTO_TUNNEL', '1') == '1'
MINI_APP_TUNNEL_TIMEOUT = int(os.getenv('MINI_APP_TUNNEL_TIMEOUT', '25'))
BASE_DIR = Path(__file__).resolve().parent
RUNTIME_MINI_APP_URL = MINI_APP_URL
MINI_APP_TUNNEL_PROCESS = None
INSTANCE_LOCK_HANDLE = None
LITERATURE_SYSTEM_PROMPT = (
    "You are a literature analysis assistant. Answer only literature-related requests: "
    "analysis of books and poems, characters, conflicts, composition, style, author intent, "
    "historical context, and exam preparation. "
    "If the request is unrelated to literature, politely refuse and redirect to literature topics. "
    "When a user provides a work and an author, give a structured and detailed analysis in Russian."
)

# Рнициализируем бота
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

def is_admin(user_id):
    """Проверяет, является ли пользователь администратором"""
    return str(user_id) == ADMIN_ID

def start_cloudflare_tunnel(local_port):
    """Start free Cloudflare tunnel and return (process, public_url)."""
    cloudflared_path = shutil.which('cloudflared') or shutil.which('cloudflared.exe')
    if not cloudflared_path:
        local_binary = BASE_DIR / 'cloudflared.exe'
        if local_binary.exists():
            cloudflared_path = str(local_binary)
    if not cloudflared_path:
        print('[WARNING] cloudflared is not installed. Mini App auto-tunnel is unavailable.')
        return None, None

    command = [
        cloudflared_path,
        'tunnel',
        '--url',
        f'http://127.0.0.1:{local_port}',
        '--no-autoupdate'
    ]

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
    except Exception as e:
        print(f'[ERROR] Failed to start cloudflared: {e}')
        return None, None

    pattern = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com', re.IGNORECASE)
    deadline = time.time() + max(MINI_APP_TUNNEL_TIMEOUT, 5)
    recent_lines = []

    while time.time() < deadline:
        if process.poll() is not None:
            break

        line = process.stdout.readline()
        if not line:
            time.sleep(0.1)
            continue

        line = line.strip()
        if line and len(recent_lines) < 6:
            recent_lines.append(line)

        match = pattern.search(line)
        if match:
            return process, match.group(0)

    try:
        process.terminate()
    except Exception:
        pass

    if recent_lines:
        print('[WARNING] cloudflared output before timeout:')
        for logged_line in recent_lines:
            print(f'  {logged_line}')
    print('[WARNING] Could not get trycloudflare URL in time.')
    return None, None


def stop_mini_app_tunnel():
    """Gracefully stop cloudflared process if it is running."""
    global MINI_APP_TUNNEL_PROCESS
    if not MINI_APP_TUNNEL_PROCESS:
        return

    try:
        if MINI_APP_TUNNEL_PROCESS.poll() is None:
            MINI_APP_TUNNEL_PROCESS.terminate()
            MINI_APP_TUNNEL_PROCESS.wait(timeout=3)
    except Exception:
        try:
            MINI_APP_TUNNEL_PROCESS.kill()
        except Exception:
            pass
    finally:
        MINI_APP_TUNNEL_PROCESS = None


atexit.register(stop_mini_app_tunnel)


def acquire_instance_lock():
    """Prevent running multiple bot instances on one machine."""
    global INSTANCE_LOCK_HANDLE
    lock_path = BASE_DIR / '.pushkin_bot.lock'

    try:
        lock_file = open(lock_path, 'a+')
        lock_file.seek(0)
        if lock_file.read(1) == '':
            lock_file.write('0')
            lock_file.flush()
        lock_file.seek(0)
        if msvcrt:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            print('[WARNING] File locking is unavailable on this platform.')
        INSTANCE_LOCK_HANDLE = lock_file
        return True
    except OSError:
        return False
    except Exception as e:
        print(f'[ERROR] Failed to initialize instance lock: {e}')
        return False


def release_instance_lock():
    """Release process lock on exit."""
    global INSTANCE_LOCK_HANDLE
    if not INSTANCE_LOCK_HANDLE:
        return

    try:
        INSTANCE_LOCK_HANDLE.seek(0)
        if msvcrt:
            msvcrt.locking(INSTANCE_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl:
            fcntl.flock(INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        INSTANCE_LOCK_HANDLE.close()
    except Exception:
        pass
    INSTANCE_LOCK_HANDLE = None


atexit.register(release_instance_lock)


def format_ai_response(text):
    """
    Форматирует текст от нейросети, добавляя HTML-разметку
    для улучшения читаемости в Telegram
    """
    try:
        # Убираем лишние пробелы и переносы
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        
        # Форматируем заголовки
        text = re.sub(r'^(#+)\s*(.+)$', lambda m: f"<b>{m.group(2)}</b>\n", text, flags=re.MULTILINE)
        
        # Форматируем подзаголовки
        text = re.sub(r'^(\d+\.\s+[^:\n]+:|[А-Я][^:\n]+:)\s*$', lambda m: f"<b>{m.group(1)}</b>", text, flags=re.MULTILINE)
        
        # Форматируем списки
        lines = text.split('\n')
        formatted_lines = []
        
        for i, line in enumerate(lines):
            if not line.strip():
                formatted_lines.append('')
                continue
            
            list_match = re.match(r'^(\s*[-•*]\s+)(.+)', line)
            if list_match:
                prefix, content = list_match.groups()
                formatted_lines.append(f"• {content}")
                continue
            
            num_match = re.match(r'^(\s*\d+\.\s+)(.+)', line)
            if num_match:
                prefix, content = num_match.groups()
                formatted_lines.append(f"{content}")
                continue
            
            term_match = re.match(r'^([^-\n]+)\s+-\s+(.+)$', line)
            if term_match:
                term, definition = term_match.groups()
                formatted_lines.append(f"<b>{term.strip()}</b> - {definition}")
                continue
            
            if '«' in line or '"' in line or "'" in line:
                def format_quote(match):
                    return f"<i>{match.group(0)}</i>"
                
                line = re.sub(r'«[^»]+»', format_quote, line)
                line = re.sub(r'"[^"]+"', format_quote, line)
                line = re.sub(r"'[^']+'", format_quote, line)
                formatted_lines.append(line)
                continue
            
            if len(line) > 100 and not any(tag in line for tag in ['<b>', '<i>', '<code>']):
                formatted_lines.append(line)
            else:
                formatted_lines.append(line)
        
        text = '\n'.join(formatted_lines)
        
        # Добавляем форматирование для ключевых терминов
        key_terms = re.findall(r'\b([А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+)*)\b', text)
        for term in set(key_terms):
            if len(term.split()) <= 3:
                text = re.sub(rf'\b{re.escape(term)}\b', f"<b>{term}</b>", text)
        
        # Форматируем имена персонажей
        text = re.sub(r'\b(Онегин|Татьяна|Раскольников|Соня|Мастер|Маргарита|Пьер|Наташа|Андрей)\b', 
                     lambda m: f"<i>{m.group(1)}</i>", text, flags=re.IGNORECASE)
        
        # Добавляем форматирование для литературных терминов
        literary_terms = ['композиция', 'сюжет', 'фабула', 'конфликт', 'образ', 'персонаж', 
                         'характер', 'пейзаж', 'интерьер', 'диалог', 'монолог', 'символ', 
                         'метафора', 'эпитет', 'гипербола', 'аллегория', 'антитеза', 
                         'гротеск', 'ирония', 'сатира', 'лирика', 'эпос', 'драма']
        
        for term in literary_terms:
            text = re.sub(rf'\b({term})\b', rf"<b>\1</b>", text, flags=re.IGNORECASE)
        
        # Форматируем годы
        text = re.sub(r'\b(\d{4})(?:\s*года?)?\b', r'<code>\1</code>', text)
        
        # Форматируем названия произведений
        text = re.sub(r'«([^»]+)»', r'<i>«\1»</i>', text)
        text = re.sub(r'"([^"]+)"', r'<i>"\1"</i>', text)
        
        return text
        
    except Exception as e:
        print(f"[ERROR] Ошибка при форматировании текста: {e}")
        return text

def send_welcome_with_image(chat_id, max_retries=3):
    """Отправляет приветственное сообщение с изображением с повторными попытками"""
    
    start_text = """<b>Привет, я Pushkin AI!</b>

Я специализируюсь на анализе литературных произведений.

<b>Как использовать:</b>
1. Отправьте мне название произведения и автора
2. Я сделаю подробный литературный анализ

<i>Примеры запросов:</i>
• "Преступление и наказание, Федор Достоевский"
• "Евгений Онегин, Александр Пушкин"
• "Мастер и Маргарита, Михаил Булгаков"

<code>Важно:</code> Я занимаюсь только разбором литературных произведений"""
    
    # Сначала отправляем текстовое сообщение
    try:
        bot.send_message(chat_id, start_text, parse_mode='HTML')
        print(f"[LOG] Текстовое приветствие отправлено в чат {chat_id}")
    except Exception as e:
        print(f"[ERROR] Ошибка при отправке текста: {e}")
    
    # Затем пытаемся отправить изображение с повторными попытками
    image_path = "main.png"
    
    if not os.path.exists(image_path):
        print(f"[WARNING] Файл {image_path} не найден.")
        return
    
    for attempt in range(max_retries):
        try:
            print(f"[LOG] Попытка {attempt + 1} отправки изображения...")
            
            with open(image_path, 'rb') as photo:
                bot.send_photo(chat_id, photo, timeout=30)
                print(f"[LOG] Рзображение успешно отправлено РІ чат {chat_id}")
                break
                
        except Exception as e:
            print(f"[ERROR] Ошибка при отправке изображения (попытка {attempt + 1}): {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                wait_time = 2 * (attempt + 1)
                print(f"[LOG] Ожидание {wait_time} секунд перед повторной попыткой...")
                time.sleep(wait_time)
            else:
                print(f"[ERROR] Не удалось отправить изображение после {max_retries} попыток")
                break


def build_mini_app_markup():
    """Create an inline keyboard button to open Telegram Mini App."""
    if not RUNTIME_MINI_APP_URL:
        return None

    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            text="Open Pushkin AI Mini App",
            web_app=telebot.types.WebAppInfo(url=RUNTIME_MINI_APP_URL)
        )
    )
    return markup


def send_mini_app_button(chat_id):
    """Send Mini App open button to user."""
    markup = build_mini_app_markup()
    if markup:
        bot.send_message(
            chat_id,
            "Open Mini App for a mobile chat UI:",
            reply_markup=markup
        )
    else:
        bot.send_message(
            chat_id,
            "Mini App URL is not configured. Set MINI_APP_URL or enable MINI_APP_AUTO_TUNNEL=1."
        )




def send_start_message_with_mini_app(chat_id):
    """Send one message with bot purpose (without Mini App button)."""
    start_text = (
        """<b>Привет, я Pushkin AI!</b>
        
Я специализируюсь на анализе литературных произведений.
        
<b>Как использовать:</b>
1. Откройте веб-приложение (кнопка слева от клавиатуры)
2. Введите запрос 
        
<i>Примеры запросов:</i>
• "Преступление и наказание, Федор Достоевский"
• "Евгений Онегин, Александр Пушкин"
• "Мастер и Маргарита, Михаил Булгаков"
        
<code>Важно:</code> Я занимаюсь только разбором литературных произведений"""
    )
    bot.send_message(chat_id, start_text, parse_mode='HTML')

@bot.message_handler(commands=["miniapp"])
def miniapp_handler(message):
    """Command to send Mini App button."""
    send_mini_app_button(message.chat.id)


class MiniAppRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for Mini App frontend and API."""

    server_version = "PushkinMiniApp/1.0"

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path):
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404, "Not Found")
            return

        content = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = 'application/octet-stream'

        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self._send_file(BASE_DIR / 'index.html')
            return

        if path == '/health':
            self._send_json(200, {'status': 'ok'})
            return

        allowed_ext = {'.css', '.js', '.png', '.jpg', '.jpeg', '.svg', '.webp', '.ico'}
        requested = (BASE_DIR / path.lstrip('/')).resolve()

        if requested.suffix.lower() in allowed_ext and str(requested).startswith(str(BASE_DIR.resolve())):
            self._send_file(requested)
            return

        self.send_error(404, 'Not Found')

    def do_POST(self):
        if self.path.split('?', 1)[0] != '/api/chat':
            self.send_error(404, 'Not Found')
            return

        try:
            content_length = int(self.headers.get('Content-Length', '0'))
            if content_length <= 0 or content_length > 100000:
                self._send_json(400, {'error': 'Invalid request size'})
                return

            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode('utf-8'))
            message = str(payload.get('message', '')).strip()
            history = payload.get('history', [])
            if not isinstance(history, list):
                history = []

            if len(message) < 3:
                self._send_json(400, {'error': 'Please enter a longer prompt'})
                return

            reply = get_answer(message, history=history)
            self._send_json(200, {'reply': reply})

        except Exception as e:
            print(f"[ERROR] Mini App API error: {e}")
            self._send_json(500, {'error': 'Server error while processing request'})


def start_mini_app_server():
    """Run the embedded Mini App HTTP server in a background thread."""
    server = ThreadingHTTPServer((MINI_APP_HOST, MINI_APP_PORT), MiniAppRequestHandler)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"[LOG] Mini App server started at http://{MINI_APP_HOST}:{MINI_APP_PORT}")
    if RUNTIME_MINI_APP_URL:
        print(f"[LOG] Telegram Mini App URL: {RUNTIME_MINI_APP_URL}")
    else:
        print('[WARNING] MINI_APP_URL is not set yet')

    return server

@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    """Handler for /start and /help commands."""
    print(f"[LOG] /start from user {message.from_user.id}")
    send_start_message_with_mini_app(message.chat.id)

@bot.message_handler(commands=["reset"])
def reset_handler(message):
    """Команда для сброса и перезапуска бота (только для администратора)"""
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        print(f"[SECURITY] Неавторизованная попытка сброса от пользователя {user_id}")
        bot.send_message(message.chat.id, "⛔ У вас нет прав для выполнения этой команды.")
        return
    
    print(f"[ADMIN] Запрошен сброс системы пользователем {user_id}")
    
    # Отправляем подтверждение
    confirm_msg = bot.send_message(
        message.chat.id,
        "<b>🔄 Запущен процесс сброса системы...</b>\n\n"
        "<i>Статус:</i> Очистка кэша и перезапуск...",
        parse_mode='HTML'
    )
    
    try:
        # Шаг 1: Логируем событие
        log_message = f"""
        вљ пёЏ АДМРРќРРЎРўР РђРўРР'РќРћР• ДЕЙСТВРР• вљ пёЏ
        
        Рнициатор: {message.from_user.id} ({message.from_user.username})
        Время: {time.strftime('%Y-%m-%d %H:%M:%S')}
        Действие: СБРОС РПЕРЕЗАПУСК РЎРСТЕМЫ
        """
        print(log_message)
        
        # Шаг 2: Обновляем статус
        bot.edit_message_text(
            "<b>🔄 Запущен процесс сброса системы...</b>\n\n"
            "<i>Статус:</i> Останавливаю бота...",
            message.chat.id,
            confirm_msg.message_id,
            parse_mode='HTML'
        )
        
        # Шаг 3: Останавливаем polling (это остановит текущий процесс)
        bot.stop_polling()
        time.sleep(2)
        
        # Шаг 4: Обновляем статус
        bot.edit_message_text(
            "<b>🔄 Запущен процесс сброса системы...</b>\n\n"
            "<i>Статус:</i> Бот остановлен. Перезапускаюсь...",
            message.chat.id,
            confirm_msg.message_id,
            parse_mode='HTML'
        )
        
        # Шаг 5: Очищаем любые временные файлы или кэш
        temp_files = ['temp_optimized.png', 'temp_response.txt']
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    print(f"[ADMIN] Удален временный файл: {temp_file}")
                except:
                    pass
        
        # Шаг 6: Записываем логи о перезапуске
        with open('restart.log', 'a') as log_file:
            log_file.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Перезапуск инициирован пользователем {user_id}\n")
        
        # Шаг 7: Отправляем финальное сообщение
        final_message = f"""
<b>✅ Сброс системы выполнен успешно!</b>

<i>Выполненные действия:</i>
• Бот остановлен
• Временные файлы очищены
• Система перезапускается

<i>Время выполнения:</i> {time.strftime('%H:%M:%S')}
<i>Статус:</i> Переход в режим ожидания...
        """
        
        bot.edit_message_text(
            final_message,
            message.chat.id,
            confirm_msg.message_id,
            parse_mode='HTML'
        )
        
        print("[ADMIN] Сброс завершен. Перезапускаю бота через 3 секунды...")
        
        # Шаг 8: Перезапускаем бота
        time.sleep(3)
        
        # Способ 1: Перезапуск через subprocess (рекомендуется)
        python_executable = sys.executable
        script_path = os.path.abspath(__file__)
        
        # Запускаем новый процесс
        subprocess.Popen([python_executable, script_path])
        
        # Шаг 9: Завершаем текущий процесс
        sys.exit(0)
        
    except Exception as e:
        error_message = f"""
<b>❌ Ошибка при сбросе системы!</b>

<i>Ошибка:</i> <code>{str(e)}</code>

Пожалуйста, перезапустите бота вручную.
        """
        
        try:
            bot.edit_message_text(
                error_message,
                message.chat.id,
                confirm_msg.message_id,
                parse_mode='HTML'
            )
        except:
            bot.send_message(message.chat.id, error_message, parse_mode='HTML')
        
        print(f"[ERROR] Ошибка при выполнении сброса: {e}")

@bot.message_handler(commands=["image"])
def image_handler(message):
    """Отправляет только изображение по команде /image"""
    try:
        image_path = "main.png"
        if os.path.exists(image_path):
            print(f"[LOG] Отправка изображения по команде /image в чат {message.chat.id}")
            
            with open(image_path, 'rb') as photo:
                bot.send_photo(message.chat.id, photo, timeout=30)
            print(f"[LOG] Рзображение отправлено РїРѕ команде /image")
                
        else:
            bot.send_message(message.chat.id, "Рзображение РЅРµ найдено РЅР° сервере.")
    except Exception as e:
        print(f"[ERROR] Ошибка при отправке изображения: {e}")
        bot.send_message(message.chat.id, "Ошибка при отправке изображения.")

@bot.message_handler(commands=["about"])
def about_handler(message):
    """Обработчик команды /about"""
    about_text = """<b>Pushkin AI</b>
    
<i>Версия:</i> 1.0
<i>Назначение:</i> Анализ литературных произведений
<i>Рспользуемая модель:</i> DeepSeek-V3.2-Exp
<i>Разработчик:</i> [Ваше имя/организация]
    
<code>По вопросам сотрудничества:</code> ваш_email@example.com"""
    
    bot.send_message(message.chat.id, about_text, parse_mode='HTML')

@bot.message_handler(commands=["admin"])
def admin_handler(message):
    """Показывает информацию об административных командах"""
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ У вас нет прав для доступа к панели администратора.")
        return
    
    admin_text = f"""<b>👨‍💼 Панель администратора</b>

<i>Ваш ID:</i> <code>{user_id}</code>
<i>Время сервера:</i> {time.strftime('%Y-%m-%d %H:%M:%S')}

<b>Доступные команды:</b>
• /reset - Сбросить и перезапустить бота
• /status - Показать статус системы
• /logs - Показать последние логи

<b>Рнформация Рѕ системе:</b>
• Python: {sys.version.split()[0]}
• Бот: Pushkin AI v1.0
"""
    
    bot.send_message(message.chat.id, admin_text, parse_mode='HTML')

@bot.message_handler(commands=["status"])
def status_handler(message):
    """Показывает статус системы"""
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        bot.send_message(message.chat.id, "⛔ У вас нет прав для просмотра статуса.")
        return
    
    # Собираем информацию о системе
    import psutil
    
    try:
        # Рспользование памяти
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        status_text = f"""<b>📊 Статус системы</b>

<i>Время сервера:</i> {time.strftime('%Y-%m-%d %H:%M:%S')}

<b>Рспользование ресурсов:</b>
• CPU: {psutil.cpu_percent()}%
• RAM: {memory.percent}% ({memory.used / 1024 / 1024:.1f} MB / {memory.total / 1024 / 1024:.1f} MB)
• Disk: {disk.percent}% ({disk.used / 1024 / 1024 / 1024:.1f} GB / {disk.total / 1024 / 1024 / 1024:.1f} GB)

<b>Файлы системы:</b>
• main.png: {'✅ найден' if os.path.exists('main.png') else '❌ не найден'}
• .env: {'✅ найден' if os.path.exists('.env') else '❌ не найден'}

<b>Процессы:</b>
• Бот: ✅ запущен
• Подключение к API: ✅ активно
"""
        
        bot.send_message(message.chat.id, status_text, parse_mode='HTML')
        
    except ImportError:
        bot.send_message(
            message.chat.id,
            "<b>📊 Статус системы</b>\n\n"
            "<i>Время сервера:</i> {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "<code>Рнформация:</code> Установите библиотеку psutil для РїРѕРґСЂРѕР±РЅРѕР№ статистики\n"
            "<code>Команда:</code> pip install psutil",
            parse_mode='HTML'
        )
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"<b>❌ Ошибка при получении статуса:</b>\n\n<code>{str(e)}</code>",
            parse_mode='HTML'
        )

@bot.message_handler(func=lambda message: True)
def text_handler(message):
    """Обработчик всех текстовых сообщений"""
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id
        prompt = str(message.text)
        
        print(f"[LOG] Получен запрос от пользователя {user_id}: {prompt[:50]}...")
        
        if len(prompt) < 5:
            bot.send_message(
                chat_id, 
                "Пожалуйста, укажите полное название произведения и автора для анализа.\n\n" +
                "<i>Пример:</i> 'Война и мир, Лев Толстой'",
                parse_mode='HTML'
            )
            return
        
        # Отправляем сообщение о начале обработки
        status_msg = bot.send_message(chat_id, "🔄 <i>Анализирую произведение...</i>", parse_mode='HTML')
        status_message_id = status_msg.message_id
        
        # Показываем индикатор печати каждые 5 секунд
        def show_typing_indicator():
            while not hasattr(show_typing_indicator, 'stop'):
                try:
                    bot.send_chat_action(chat_id, 'typing')
                    time.sleep(5)
                except:
                    break
        
        # Запускаем индикатор печати в отдельном потоке
        import threading
        typing_thread = threading.Thread(target=show_typing_indicator)
        typing_thread.daemon = True
        typing_thread.start()
        
        try:
            # Получаем ответ от нейросети
            response = get_answer(prompt)
            
            # Останавливаем индикатор печати
            show_typing_indicator.stop = True
            typing_thread.join(timeout=1)
            
            # Форматируем ответ
            formatted_response = format_ai_response(response)
            
            # Проверяем длину ответа
            if len(formatted_response) > 4000:
                # Разбиваем на части
                parts = []
                current_part = ""
                
                for paragraph in formatted_response.split('\n\n'):
                    if len(current_part) + len(paragraph) + 2 < 4000:
                        current_part += paragraph + '\n\n'
                    else:
                        parts.append(current_part)
                        current_part = paragraph + '\n\n'
                
                if current_part:
                    parts.append(current_part)
                
                # Удаляем статусное сообщение
                try:
                    bot.delete_message(chat_id, status_message_id)
                except:
                    pass
                
                # Отправляем первую часть
                first_part = parts[0]
                if len(first_part) > 4000:
                    first_part = first_part[:4000]
                
                sent_msg = bot.send_message(chat_id, first_part, parse_mode='HTML')
                last_message_id = sent_msg.message_id
                
                # Отправляем остальные части как отдельные сообщения
                for i, part in enumerate(parts[1:], 1):
                    if len(part) > 4000:
                        part = part[:4000]
                    
                    # Добавляем номер части
                    part_with_number = f"<b>Часть {i+1}</b>\n\n{part}"
                    sent_msg = bot.send_message(chat_id, part_with_number, parse_mode='HTML')
                    last_message_id = sent_msg.message_id
                    
            else:
                # Удаляем статусное сообщение
                try:
                    bot.delete_message(chat_id, status_message_id)
                except:
                    pass
                
                # Отправляем форматированный ответ
                bot.send_message(chat_id, formatted_response, parse_mode='HTML')
            
            print(f'[LOG] Ответ успешно отправлен пользователю {user_id}, длина: {len(response)} символов')
            
        except Exception as e:
            # Останавливаем индикатор печати
            show_typing_indicator.stop = True
            
            # Удаляем статусное сообщение
            try:
                bot.delete_message(chat_id, status_message_id)
            except:
                pass
            
            error_msg = f"Произошла ошибка при анализе произведения:\n\n<code>{str(e)[:200]}</code>"
            bot.send_message(chat_id, error_msg, parse_mode='HTML')
            print(f"[ERROR] Ошибка при обработке запроса: {e}")
            
    except Exception as e:
        print(f"[ERROR] Критическая ошибка в обработчике: {e}")
        try:
            bot.send_message(
                chat_id,
                "Произошла критическая ошибка при обработке вашего запроса. Пожалуйста, попробуйте еще раз."
            )
        except:
            pass

def build_literature_messages(content, history=None):
    """Build chat messages for model call with strict literature scope."""
    messages = [{"role": "system", "content": LITERATURE_SYSTEM_PROMPT}]
    if isinstance(history, list):
        for item in history[-10:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).strip().lower()
            text = str(item.get("content", "")).strip()
            if role not in ("user", "assistant") or not text:
                continue
            messages.append({
                "role": role,
                "content": text[:1500]
            })
    messages.append({"role": "user", "content": content})
    return messages


def get_answer(content, history=None):
    """Get model response for Telegram chat and Mini App."""
    client = OpenAI(
        base_url="https://router.huggingface.co/v1",
        api_key=HUGGINGFACE_TOKEN
    )
    completion = client.chat.completions.create(
        model="deepseek-ai/DeepSeek-V3.2-Exp:novita",
        messages=build_literature_messages(content, history=history),
        max_tokens=3500,
        temperature=0.7,
    )
    return completion.choices[0].message.content

if __name__ == "__main__":
    if not acquire_instance_lock():
        print("[ERROR] Another bot instance is already running. Stop it before starting a new one.")
        sys.exit(1)

    mini_app_server = None
    if MINI_APP_ENABLED:
        try:
            mini_app_server = start_mini_app_server()
            if not RUNTIME_MINI_APP_URL and MINI_APP_AUTO_TUNNEL:
                MINI_APP_TUNNEL_PROCESS, tunnel_url = start_cloudflare_tunnel(MINI_APP_PORT)
                if tunnel_url:
                    RUNTIME_MINI_APP_URL = tunnel_url
                    print(f"[LOG] Auto tunnel URL: {RUNTIME_MINI_APP_URL}")
                else:
                    print('[WARNING] Auto tunnel failed. Mini App button may be unavailable.')
        except Exception as e:
            print(f"[ERROR] Failed to start Mini App server: {e}")

    print("=" * 50)
    print("Pushkin AI Bot запущен!")
    print(f"Администратор: ID {ADMIN_ID}")
    print(f"Подключен к Telegram")
    print(f"Рспользуется модель: DeepSeek-V3.2-Exp")
    
    if os.path.exists("main.png"):
        file_size = os.path.getsize("main.png")
        print(f"Рзображение main.png найдено, размер: {file_size/1024/1024:.2f}MB")
    else:
        print(f"Рзображение main.png РЅРµ найдено РІ текущей директории")
        print(f"Текущая директория: {os.getcwd()}")
    
    print("=" * 50)
    print("Ожидаю запросы...")
    print("Административные команды:")
    print(f"  • /admin - панель администратора")
    print(f"  • /reset - сброс и перезапуск")
    print(f"  • /status - статус системы")

    try:
        bot.remove_webhook()
    except Exception as e:
        print(f"[WARNING] Could not remove webhook before polling: {e}")

    try:
        bot.polling(none_stop=True, interval=1, timeout=30)
    except Exception as e:
        error_text = str(e)
        print(f"[CRITICAL ERROR] Bot stopped: {error_text}")

        if 'Error code: 409' in error_text:
            print('[ERROR] Telegram 409 conflict: another bot instance is polling getUpdates.')
            print('[INFO] Keep only one running process/session for this bot token.')
            stop_mini_app_tunnel()
            sys.exit(1)

        print('[INFO] Auto restart in 5 seconds...')
        time.sleep(5)
        python_executable = sys.executable
        script_path = os.path.abspath(__file__)
        stop_mini_app_tunnel()
        subprocess.Popen([python_executable, script_path])
        sys.exit(0)


