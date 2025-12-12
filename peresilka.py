import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # встроенный модуль Python 3.9+

TOKEN = "8367672393:AAEgvlNjMDpo73cPfx7iR8gDqfpIj2Ig688"
CHANNEL_ID = -1002226758013

MSK = ZoneInfo("Europe/Moscow")  # Московское время

bot = Bot(token=TOKEN)
dp = Dispatcher()

# =============================
# FSM состояния
# =============================
class TimeInput(StatesGroup):
    waiting_for_time = State()

# =============================
# CallbackData
# =============================
class MessageCallback(CallbackData, prefix="msg"):
    action: str
    chat_id: int
    message_id: int

# =============================
# SQLite база
# =============================
conn = sqlite3.connect("tasks.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    message_id INTEGER,
    send_time TEXT
)
""")
conn.commit()

# =============================
# Отложенная отправка
# =============================
async def delayed_forward_db(chat_id: int, message_id: int, send_time: datetime, task_id: int):
    now = datetime.now(MSK)
    delay = (send_time - now).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await bot.forward_message(
            chat_id=CHANNEL_ID,
            from_chat_id=chat_id,
            message_id=message_id
        )
    except Exception as e:
        print(f"Ошибка пересылки: {e}")
    finally:
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()

# =============================
# Загрузка задач из базы при старте
# =============================
async def load_tasks_from_db():
    cursor.execute("SELECT id, chat_id, message_id, send_time FROM tasks")
    rows = cursor.fetchall()
    for task_id, chat_id, message_id, send_time_str in rows:
        send_time = datetime.fromisoformat(send_time_str).replace(tzinfo=MSK)
        asyncio.create_task(delayed_forward_db(chat_id, message_id, send_time, task_id))

# =============================
# ОБРАБОТЧИК ВХОДЯЩИХ СООБЩЕНИЙ
# =============================
@dp.message(StateFilter(None))
async def handle_message(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Принять (30 мин)",
                    callback_data=MessageCallback(
                        action="accept_delayed",
                        chat_id=message.chat.id,
                        message_id=message.message_id
                    ).pack()
                ),
                InlineKeyboardButton(
                    text="Отправить сейчас",
                    callback_data=MessageCallback(
                        action="accept_now",
                        chat_id=message.chat.id,
                        message_id=message.message_id
                    ).pack()
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отправить по времени",
                    callback_data=MessageCallback(
                        action="custom_time",
                        chat_id=message.chat.id,
                        message_id=message.message_id
                    ).pack()
                ),
                InlineKeyboardButton(
                    text="Отказ",
                    callback_data=MessageCallback(
                        action="decline",
                        chat_id=message.chat.id,
                        message_id=message.message_id
                    ).pack()
                )
            ]
        ]
    )
    await message.reply("Выберите действие:", reply_markup=keyboard)

# =============================
# ОБРАБОТКА КНОПОК
# =============================
@dp.callback_query(MessageCallback.filter())
async def process_buttons(callback: types.CallbackQuery, callback_data: MessageCallback, state: FSMContext):
    moderator = callback.from_user.username or callback.from_user.full_name
    chat_id = callback_data.chat_id
    message_id = callback_data.message_id

    # Отправить сейчас
    if callback_data.action == "accept_now":
        await bot.forward_message(CHANNEL_ID, chat_id, message_id)
        await callback.message.edit_text(f"✅ Отправлено сразу (модератор @{moderator})")
        return await callback.answer()

    # Отложить на 30 мин
    if callback_data.action == "accept_delayed":
        send_time = datetime.now(MSK) + timedelta(minutes=30)
        cursor.execute("INSERT INTO tasks (chat_id, message_id, send_time) VALUES (?, ?, ?)",
                       (chat_id, message_id, send_time.isoformat()))
        task_id = cursor.lastrowid
        conn.commit()
        asyncio.create_task(delayed_forward_db(chat_id, message_id, send_time, task_id))
        await callback.message.edit_text(f"⏳ Будет отправлено через 30 минут (модератор @{moderator})")
        return await callback.answer()

    # Отправка по времени
    if callback_data.action == "custom_time":
        await state.update_data(chat_id=chat_id, message_id=message_id, moderator=moderator)
        await callback.message.edit_text("Введите время отправки (HH:MM, МСК)")
        await state.set_state(TimeInput.waiting_for_time)
        return await callback.answer()

    # Отклонить
    if callback_data.action == "decline":
        await callback.message.edit_text(f"❌ Отклонено модератором @{moderator}")
        return await callback.answer()

# =============================
# FSM: ввод времени
# =============================
@dp.message(TimeInput.waiting_for_time)
async def input_time(message: types.Message, state: FSMContext):
    try:
        user_time = datetime.strptime(message.text, "%H:%M").time()
    except ValueError:
        return await message.answer("❗ Неверный формат, попробуйте HH:MM")

    now = datetime.now(MSK)
    send_time = datetime.combine(now.date(), user_time).replace(tzinfo=MSK)
    if send_time < now:
        send_time += timedelta(days=1)

    data = await state.get_data()
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    moderator = data["moderator"]

    cursor.execute("INSERT INTO tasks (chat_id, message_id, send_time) VALUES (?, ?, ?)",
                   (chat_id, message_id, send_time.isoformat()))
    task_id = cursor.lastrowid
    conn.commit()

    asyncio.create_task(delayed_forward_db(chat_id, message_id, send_time, task_id))

    await message.answer(f"⏳ Сообщение будет отправлено в {send_time.strftime('%H:%M')} МСК (модератор @{moderator})")
    await state.clear()

# =============================
# START
# =============================
async def main():
    await load_tasks_from_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
