import html
import io
import os
import sys
import threading
import time
from typing import Any, Dict, Optional
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv(".env")
load_dotenv("API.env")

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

import db
import pdf_export
import plan_service

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    print("[CRITICAL] TELEGRAM_BOT_TOKEN is not configured in .env or API.env!", file=sys.stderr)
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

# Pre-validate token to prevent infinite 401 retry loops during polling
try:
    bot_info = bot.get_me()
    print(f"[*] Telegram Bot authenticated as @{bot_info.username} (ID: {bot_info.id})", flush=True)
except ApiTelegramException as e:
    print(f"[CRITICAL] Invalid TELEGRAM_BOT_TOKEN (Unauthorized: {e})", file=sys.stderr, flush=True)
    sys.exit(1)
except Exception as e:
    print(f"[WARNING] Could not verify Telegram bot on startup (will attempt polling): {e}", flush=True)

# Хранилище сессий пользователей (chat_id -> dict)
user_sessions: Dict[int, Dict[str, Any]] = {}
# Кэш последнего сгенерированного плана для PDF и Feedback (chat_id -> dict)
user_last_plan: Dict[int, Dict[str, Any]] = {}
# Блокировка от повторных параллельных генераций одним пользователем (thread-safe)
user_generating_locks: Dict[int, bool] = {}
user_generating_locks_mutex = threading.Lock()


# --- Вспомогательные функции форматирования ---

def escape_html(text: Any) -> str:
    """Безопасное экранирование HTML для Telegram."""
    if text is None:
        return ""
    return html.escape(str(text))


def format_plan_html(plan_data: Dict[str, Any]) -> str:
    """Преобразование структуры плана в красивый HTML для Telegram."""
    if not plan_data:
        return "❌ <i>План пуст или не сформирован.</i>"

    summary = escape_html(plan_data.get("summary") or "Индивидуальный план тренировок")
    res = f"🏀 <b>{summary}</b>\n\n"

    safety = plan_data.get("safety_notes")
    if safety:
        res += "⚠️ <b>Техника безопасности:</b>\n"
        notes = safety if isinstance(safety, list) else [safety]
        for note in notes:
            res += f"• <i>{escape_html(note)}</i>\n"
        res += "\n"

    schedule = plan_data.get("schedule", [])
    for day in schedule:
        day_title = escape_html(day.get("day", "Тренировочный день"))
        day_focus = escape_html(day.get("focus", ""))
        res += f"📅 <b>{day_title} — {day_focus}</b>\n"

        for ex in day.get("exercises", []):
            name = escape_html(ex.get("name", "Упражнение"))
            sets = escape_html(ex.get("sets", "3"))
            reps = escape_html(ex.get("reps", "10"))
            notes = ex.get("notes", "")

            res += f"  ▫️ <b>{name}</b>: {sets} подх. × {reps}"
            if notes:
                res += f" <i>({escape_html(notes)})</i>"
            res += "\n"
        res += "\n"

    return res


def send_long_html_message(chat_id: int, text: str, reply_markup: Optional[types.InlineKeyboardMarkup] = None):
    """Безопасная отправка длинных сообщений (сплит по 3900 символов)."""
    max_len = 3900
    if len(text) <= max_len:
        bot.send_message(chat_id, text, reply_markup=reply_markup)
        return

    parts = []
    while len(text) > max_len:
        split_idx = text.rfind("\n\n", 0, max_len)
        if split_idx == -1:
            split_idx = text.rfind("\n", 0, max_len)
        if split_idx == -1:
            split_idx = max_len
        parts.append(text[:split_idx])
        text = text[split_idx:].strip()
    if text:
        parts.append(text)

    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        bot.send_message(chat_id, part, reply_markup=markup)


def get_plan_action_keyboard() -> types.InlineKeyboardMarkup:
    """Инлайн-кнопки действий под готовым планом."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_pdf = types.InlineKeyboardButton("📥 Скачать PDF", callback_data="action_download_pdf")
    btn_adapt = types.InlineKeyboardButton("🔄 Адаптировать (Feedback)", callback_data="action_adapt_plan")
    btn_new = types.InlineKeyboardButton("🆕 Новый план", callback_data="action_new_plan")
    markup.add(btn_pdf, btn_adapt)
    markup.add(btn_new)
    return markup


# --- Клавиатуры визарда ---

def get_positions_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🏀 PG (Разыгрывающий)", callback_data="pos_PG"),
        types.InlineKeyboardButton("⚡ SG (Атакующий)", callback_data="pos_SG"),
        types.InlineKeyboardButton("🎯 SF (Легкий форвард)", callback_data="pos_SF"),
        types.InlineKeyboardButton("💪 PF (Мощный форвард)", callback_data="pos_PF"),
        types.InlineKeyboardButton("🛡️ C (Центровой)", callback_data="pos_C"),
        types.InlineKeyboardButton("🌟 Универсал", callback_data="pos_Universal"),
    )
    return markup


def get_goals_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🚀 Вертикальный прыжок и данки", callback_data="goal_jump"),
        types.InlineKeyboardButton("🎯 Стабильность броска и техника", callback_data="goal_shooting"),
        types.InlineKeyboardButton("⚡ Скорость первого шага и дриблинг", callback_data="goal_speed_dribble"),
        types.InlineKeyboardButton("🛡️ Защитная стойка и выносливость", callback_data="goal_defense_cardio"),
        types.InlineKeyboardButton("🏋️ Атлетизм и силовая подготовка", callback_data="goal_strength"),
    )
    return markup


def get_days_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=5)
    buttons = [
        types.InlineKeyboardButton(f"{d} дн.", callback_data=f"days_{d}")
        for d in range(2, 7)
    ]
    markup.add(*buttons)
    return markup


# --- Хэндлеры команд ---

@bot.message_handler(commands=["start", "help"])
def handle_start(message: types.Message):
    chat_id = message.chat.id
    user_sessions.pop(chat_id, None)

    welcome_text = (
        "👋 <b>Привет! Я AI-тренер Basketball Coach</b> 🏀\n\n"
        "Я составляю персонализированные тренировочные программы уровня Pro:\n"
        "• <i>Плиометрика и взрывной прыжок</i>\n"
        "• <i>Баскетбольные навыки и бросковая техника</i>\n"
        "• <i>Силовая подготовка с учётом биомеханики и травм</i>\n\n"
        "Нажмите кнопку ниже или отправьте <b>/plan</b>, чтобы начать!"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🚀 Составить план тренировок", callback_data="action_new_plan"))
    bot.send_message(chat_id, welcome_text, reply_markup=markup)


@bot.message_handler(commands=["plan"])
def handle_plan_command(message: types.Message):
    start_wizard(message.chat.id)


def start_wizard(chat_id: int):
    user_sessions[chat_id] = {}
    msg = bot.send_message(
        chat_id,
        "📏 <b>Шаг 1 из 6:</b> Введите ваш <b>рост в сантиметрах</b> (например, <code>188</code>):"
    )
    bot.register_next_step_handler(msg, process_height_step)


# --- Пошаговый сбор данных (FSM) ---

def process_height_step(message: types.Message):
    chat_id = message.chat.id
    text = (message.text or "").strip()

    if not text.isdigit() or not (120 <= int(text) <= 240):
        msg = bot.send_message(
            chat_id,
            "⚠️ Пожалуйста, введите реальный рост числом от 120 до 240 см (например: <code>185</code>):"
        )
        bot.register_next_step_handler(msg, process_height_step)
        return

    user_sessions.setdefault(chat_id, {})["height"] = int(text)
    msg = bot.send_message(
        chat_id,
        "⚖️ <b>Шаг 2 из 6:</b> Введите ваш <b>вес в кг</b> (например, <code>82.5</code>):"
    )
    bot.register_next_step_handler(msg, process_weight_step)


def process_weight_step(message: types.Message):
    chat_id = message.chat.id
    text = (message.text or "").strip().replace(",", ".")

    try:
        weight = float(text)
        if not (35.0 <= weight <= 200.0):
            raise ValueError
    except ValueError:
        msg = bot.send_message(
            chat_id,
            "⚠️ Введите корректный вес числом от 35 до 200 кг (например: <code>78</code>):"
        )
        bot.register_next_step_handler(msg, process_weight_step)
        return

    user_sessions[chat_id]["weight"] = weight
    bot.send_message(
        chat_id,
        "🏀 <b>Шаг 3 из 6:</b> Выберите вашу <b>позицию на площадке</b>:",
        reply_markup=get_positions_keyboard()
    )


# --- Обработка Callback Query (Инлайн-кнопки) ---

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    data = call.data

    # 1. Действия над планом
    if data == "action_new_plan":
        bot.answer_callback_query(call.id)
        start_wizard(chat_id)
        return

    elif data == "action_download_pdf":
        bot.answer_callback_query(call.id, "Генерирую PDF...")
        export_and_send_pdf(chat_id)
        return

    elif data == "action_adapt_plan":
        bot.answer_callback_query(call.id)
        prompt_feedback_adaptation(chat_id)
        return

    # 2. Выбор позиции
    if data.startswith("pos_"):
        bot.answer_callback_query(call.id)
        pos_map = {
            "pos_PG": "PG (Разыгрывающий)",
            "pos_SG": "SG (Атакующий защитник)",
            "pos_SF": "SF (Легкий форвард)",
            "pos_PF": "PF (Мощный форвард)",
            "pos_C": "C (Центровой)",
            "pos_Universal": "Универсальный игрок"
        }
        selected_pos = pos_map.get(data, "PG")
        user_sessions.setdefault(chat_id, {})["position"] = selected_pos

        try:
            bot.edit_message_text(
                f"✅ Позиция выбрана: <b>{selected_pos}</b>\n\n"
                "🎯 <b>Шаг 4 из 6:</b> Выберите <b>главную цель тренировок</b>:",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=get_goals_keyboard()
            )
        except ApiTelegramException:
            bot.send_message(chat_id, "🎯 <b>Шаг 4 из 6:</b> Выберите <b>главную цель тренировок</b>:", reply_markup=get_goals_keyboard())
        return

    # 3. Выбор цели
    if data.startswith("goal_"):
        bot.answer_callback_query(call.id)
        goals_map = {
            "goal_jump": "Вертикальный прыжок и взрывная сила",
            "goal_shooting": "Стабильность и техника броска",
            "goal_speed_dribble": "Скорость первого шага и дриблинг",
            "goal_defense_cardio": "Защитная стойка и выносливость",
            "goal_strength": "Атлетизм и общая силовая подготовка"
        }
        selected_goal = goals_map.get(data, "Комплексное развитие")
        user_sessions.setdefault(chat_id, {})["goal"] = selected_goal

        try:
            bot.edit_message_text(
                f"✅ Цель выбрана: <b>{selected_goal}</b>\n\n"
                "📅 <b>Шаг 5 из 6:</b> Сколько <b>дней в неделю</b> готовы тренироваться?",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=get_days_keyboard()
            )
        except ApiTelegramException:
            bot.send_message(chat_id, "📅 <b>Шаг 5 из 6:</b> Сколько <b>дней в неделю</b> готовы тренироваться?", reply_markup=get_days_keyboard())
        return

    # 4. Выбор количества тренировочных дней
    if data.startswith("days_"):
        bot.answer_callback_query(call.id)
        days = int(data.split("_")[1])
        user_sessions.setdefault(chat_id, {})["days_per_week"] = days

        try:
            bot.edit_message_text(
                f"✅ Выбрано: <b>{days} дня(ей) в неделю</b>",
                chat_id=chat_id,
                message_id=call.message.message_id
            )
        except ApiTelegramException:
            pass

        msg = bot.send_message(
            chat_id,
            "🏥 <b>Шаг 6 из 6:</b> Укажите <b>травмы или ограничения</b> (например: <i>болят колени при приземлении, травма плеча</i>).\n\n"
            "Если ограничений нет, отправьте <code>Нет</code>:"
        )
        bot.register_next_step_handler(msg, process_injuries_step)
        return


def process_injuries_step(message: types.Message):
    chat_id = message.chat.id
    injuries_text = (message.text or "Нет").strip()[:400]

    session = user_sessions.get(chat_id)
    if not session or "height" not in session or "position" not in session:
        bot.send_message(chat_id, "⚠️ Данные сессии устарели. Нажмите /plan для начала.")
        return

    session["injuries"] = injuries_text if injuries_text.lower() != "нет" else "None"
    session["model"] = "auto"

    # Запуск фонового пайплайна генерации
    run_plan_generation_flow(chat_id, session)


# --- Фоновая генерация и живое обновление прогресса ---

def run_plan_generation_flow(chat_id: int, payload: Dict[str, Any], is_adaptation: bool = False):
    with user_generating_locks_mutex:
        if user_generating_locks.get(chat_id):
            bot.send_message(chat_id, "⏳ План уже генерируется. Пожалуйста, подождите...")
            return
        user_generating_locks[chat_id] = True

    status_prefix = "🔄 <b>Адаптация программы под ваш фидбек</b>\n\n" if is_adaptation else "⚡ <b>Генерация индивидуального плана</b>\n\n"
    try:
        status_msg = bot.send_message(
            chat_id,
            f"{status_prefix}🔍 [██░░░░░░░░] 20% — Анализ параметров и поиск по базе знаний..."
        )
    except Exception as e:
        with user_generating_locks_mutex:
            user_generating_locks[chat_id] = False
        print(f"[TelegramBot Error] Failed to send status message: {e}")
        return

    def worker():
        try:
            created = plan_service.create_task(payload, username=f"tg_{chat_id}")
            task_id = created["task_id"]
            poll_token = created["poll_token"]

            max_attempts = 60  # 60 × 2.5с = 150 секунд ожидания
            progress_stages = [
                (4, "🔍 [███░░░░░░░] 30% — Поиск методик в спортивной литературе (RAG)..."),
                (9, "🤖 [█████░░░░░] 55% — AI проектирует упражнения, сеты и повторения..."),
                (18, "🧠 [███████░░░] 75% — Оптимизация периодизации и нагрузки..."),
                (28, "📋 [█████████░] 90% — Проверка техники безопасности и сборка..."),
            ]

            stage_idx = 0
            for attempt in range(1, max_attempts + 1):
                time.sleep(2.5)

                # Периодическое обновление статуса в чате
                if stage_idx < len(progress_stages) and attempt >= progress_stages[stage_idx][0]:
                    text_stage = progress_stages[stage_idx][1]
                    try:
                        bot.edit_message_text(
                            f"{status_prefix}{text_stage}",
                            chat_id=chat_id,
                            message_id=status_msg.message_id
                        )
                    except ApiTelegramException:
                        pass
                    stage_idx += 1

                # Отправка typing действия
                if attempt % 2 == 0:
                    try:
                        bot.send_chat_action(chat_id, "typing")
                    except Exception:
                        pass

                task = plan_service.get_task(task_id, poll_token=poll_token)
                if not task:
                    continue

                status = task.get("status")
                if status == "success":
                    # Удаляем статусное сообщение и выдаем готовый план
                    try:
                        bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass

                    # Сохраняем в кэш для PDF и адаптации
                    user_last_plan[chat_id] = {
                        "payload": payload,
                        "apiResult": task
                    }

                    formatted_plan = format_plan_html(task.get("data", {}))
                    send_long_html_message(chat_id, formatted_plan, reply_markup=get_plan_action_keyboard())
                    return

                elif status == "error":
                    try:
                        bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
                    bot.send_message(chat_id, "❌ Произошла ошибка при генерации плана. Попробуйте снова через /plan.")
                    return

            # Если время ожидания вышло
            try:
                bot.delete_message(chat_id, status_msg.message_id)
            except Exception:
                pass
            bot.send_message(
                chat_id,
                "⏱ <b>Время ожидания ответа AI превысило 2.5 минуты.</b>\n"
                "Серверы сейчас перегружены. Попробуйте нажать /plan еще раз."
            )

        except Exception as e:
            print(f"[TelegramBot Error] {e}")
            try:
                bot.send_message(chat_id, "❌ Непредвиденная ошибка при создании плана. Пожалуйста, попробуйте позже.")
            except Exception:
                pass
        finally:
            with user_generating_locks_mutex:
                user_generating_locks[chat_id] = False

    threading.Thread(target=worker, daemon=True).start()


# --- Генерация PDF ---

def export_and_send_pdf(chat_id: int):
    cached = user_last_plan.get(chat_id)
    # Если в RAM нет плана (например, после рестарта бота), восстанавливаем из SQLite
    if not cached or not cached.get("apiResult", {}).get("data"):
        try:
            with db.get_db_connection() as conn:
                row = conn.execute(
                    "SELECT result FROM plan_tasks WHERE owner = ? AND status = 'success' ORDER BY created_at DESC LIMIT 1",
                    (f"tg_{chat_id}",)
                ).fetchone()
                if row and row["result"]:
                    import json
                    task_res = json.loads(row["result"])
                    cached = {"payload": {}, "apiResult": task_res}
                    user_last_plan[chat_id] = cached
        except Exception as e:
            print(f"[TelegramBot DB Fallback Error] {e}")

    if not cached or not cached.get("apiResult", {}).get("data"):
        bot.send_message(chat_id, "⚠️ Нет готового плана для скачивания. Сгенерируйте новый через /plan.")
        return

    try:
        bot.send_chat_action(chat_id, "upload_document")
        pdf_bytes = pdf_export.generate_plan_pdf(cached.get("payload", {}), cached["apiResult"])
        
        pdf_stream = io.BytesIO(pdf_bytes)
        pdf_stream.name = f"Basketball_Training_Plan_{chat_id}.pdf"

        bot.send_document(
            chat_id,
            document=pdf_stream,
            caption="🏀 <b>Ваша персональная программа тренировок в PDF</b>\n<i>Hoop Pro AI Coach</i>"
        )
    except Exception as e:
        print(f"[PDF Export Error] {e}")
        bot.send_message(chat_id, "❌ Не удалось сформировать PDF. Попробуйте позже.")


# --- Адаптация (Feedback Loop) ---

def prompt_feedback_adaptation(chat_id: int):
    cached = user_last_plan.get(chat_id)
    if not cached:
        bot.send_message(chat_id, "⚠️ Предыдущий план не найден. Создайте новый через /plan.")
        return

    msg = bot.send_message(
        chat_id,
        "📝 <b>Адаптация программы под ваш фидбек</b>\n\n"
        "Напишите, как прошли ваши тренировки:\n"
        "• <i>Какие упражнения показались слишком легкими или тяжелыми?</i>\n"
        "• <i>Были ли боли в коленях, связках или суставах?</i>\n"
        "• <i>Хотите ли скорректировать объем или нагрузку?</i>\n\n"
        "<i>Отправьте ваш отзыв сообщением:</i>"
    )
    bot.register_next_step_handler(msg, process_feedback_step)


def process_feedback_step(message: types.Message):
    chat_id = message.chat.id
    raw_text = (message.text or "").strip()

    # Если пользователь ввел команду вместо отзыва
    if raw_text.startswith("/"):
        if raw_text in ("/start", "/help"):
            handle_start(message)
        elif raw_text == "/plan":
            handle_plan_command(message)
        return

    feedback_text = raw_text[:500]
    cached = user_last_plan.get(chat_id)
    if not cached or not feedback_text:
        bot.send_message(chat_id, "⚠️ Не удалось получить отзыв. Нажмите /plan для новой программы.")
        return

    updated_payload = dict(cached["payload"])
    updated_payload["feedback"] = feedback_text

    bot.send_message(chat_id, f"👌 Принято: <i>«{escape_html(feedback_text)}»</i>\nПередаю данные тренеру AI для перерасчета нагрузки...")
    run_plan_generation_flow(chat_id, updated_payload, is_adaptation=True)


# --- Запуск бота ---

if __name__ == "__main__":
    print("[*] Starting Basketball Coach Telegram Bot...", flush=True)
    bot.infinity_polling(timeout=20, long_polling_timeout=20)
