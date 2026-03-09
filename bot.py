import threading
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

import random
import logging
from datetime import datetime, timedelta
from enum import Enum, auto

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters, CallbackQueryHandler
)

BOT_TOKEN      = "8105638057:AAF0hHZnRPdJjKi6Ydi6C-BVApA8ltNj5GU"
ADMIN_IDS      = [5423348915]
ADMIN_PASSWORD = "в"

# Файл для хранения каналов
CHANNELS_FILE = "channels.json"

class Step(Enum):
    PRIZE      = auto()
    RULES      = auto()
    DURATION   = auto()
    WINNERS    = auto()
    CONFIRM    = auto()
    ADD_CHANNEL = auto()
    SELECT_CHANNEL = auto()

class AddChannelStep(Enum):
    USERNAME = auto()

REMINDER_TEMPLATE = """⏰ До конца розыгрыша осталось 5 минут!

🎁 {prize}

Последний шанс написать комментарий! 🔥"""

RESULT_TEMPLATE = """🎊 РОЗЫГРЫШ ЗАВЕРШЁН!

🎁 Приз: {prize}
👥 Всего участников: {total}

🏆 ПОБЕДИТЕЛИ:
{winners_text}

Поздравляем! Напишите в личку @{admin_username} для получения приза 🎉"""

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

sessions: dict = {}  # channel_id -> GiveawaySession


# ─── Хранение каналов ───────────────────────────────────────────────────────

def load_channels() -> dict:
    """{ admin_id: [ {channel_id, username, title, discussion_id}, ... ] }"""
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_channels(data: dict):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_admin_channels(admin_id: int) -> list:
    data = load_channels()
    return data.get(str(admin_id), [])

def add_channel_for_admin(admin_id: int, channel_info: dict):
    data = load_channels()
    key = str(admin_id)
    if key not in data:
        data[key] = []
    # Не дублируем
    for ch in data[key]:
        if ch["channel_id"] == channel_info["channel_id"]:
            return False
    data[key].append(channel_info)
    save_channels(data)
    return True

def remove_channel_for_admin(admin_id: int, channel_id):
    data = load_channels()
    key = str(admin_id)
    if key not in data:
        return False
    before = len(data[key])
    data[key] = [ch for ch in data[key] if ch["channel_id"] != channel_id]
    save_channels(data)
    return len(data[key]) < before


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def build_post(prize, rules, winners, duration):
    rules_block = f"\n📌 Условия участия:\n{rules}\n" if rules else ""
    return (
        f"🎁 {prize} 🎁\n"
        f"{rules_block}\n"
        f"🏆 Победителей: {winners}\n"
        f"⏰ Розыгрыш через {duration} мин\n\n"
        f"🥳 Выберу рандомно — удачи всем! 🍀"
    )

def msg_link(discussion_id, msg_id):
    chat_id_str = str(discussion_id).replace("-100", "")
    return f"https://t.me/c/{chat_id_str}/{msg_id}"

def is_admin(uid): return uid in ADMIN_IDS
def mention(info): return f"@{info['username']}" if info.get("username") else info["name"]


# ─── Сессия розыгрыша ─────────────────────────────────────────────────────────

class GiveawaySession:
    def __init__(self, prize, rules, duration_min, winners_count,
                 channel_id, channel_username, discussion_id, discussion_post_id):
        self.prize              = prize
        self.rules              = rules
        self.duration_min       = duration_min
        self.winners_count      = winners_count
        self.channel_id         = channel_id
        self.channel_username   = channel_username
        self.discussion_id      = discussion_id
        self.discussion_post_id = discussion_post_id
        self.start_time         = datetime.now()
        self.end_time           = self.start_time + timedelta(minutes=duration_min)
        self.all_comments: list = []
        self.unique_users: dict = {}

    def register(self, user_id, name, username, msg_id, msg_time):
        if msg_time > self.end_time:
            return
        self.all_comments.append({"uid": user_id, "name": name, "username": username, "msg_id": msg_id})
        self.unique_users[user_id] = name
        log.info(f"  -> комментарий #{len(self.all_comments)} от {name}")

    def pick_winners(self):
        if not self.all_comments:
            return []
        pool, chosen = [], set()
        candidates = self.all_comments[:]
        random.shuffle(candidates)
        for entry in candidates:
            uid = entry["uid"]
            if uid not in chosen and len(pool) < self.winners_count:
                chosen.add(uid)
                pool.append(entry)
        return pool


# ─── /start ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text(
            "👋 Привет! Я бот для розыгрышей.\n\n"
            "/admin [пароль] — стать администратором"
        )
        return

    channels = get_admin_channels(user.id)

    if not channels:
        await update.message.reply_text(
            "👋 Привет! Я бот для розыгрышей.\n\n"
            "📢 У тебя пока нет добавленных каналов.\n\n"
            "Добавь свой канал командой /addchannel\n\n"
            "После добавления канала ты сможешь создавать розыгрыши командой /giveaway"
        )
    else:
        ch_list = "\n".join([f"• {ch.get('title', ch['username'])}" for ch in channels])
        await update.message.reply_text(
            f"👋 Привет!\n\n"
            f"📢 Твои каналы:\n{ch_list}\n\n"
            f"📋 Команды:\n"
            f"/giveaway — создать розыгрыш\n"
            f"/addchannel — добавить канал\n"
            f"/channels — список каналов\n"
            f"/stop — остановить розыгрыш\n"
            f"/admins — список администраторов"
        )


# ─── /addchannel ─────────────────────────────────────────────────────────────

async def cmd_addchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только для администраторов.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📢 Добавление канала\n\n"
        "Шаг 1: Введи @username канала (например @mychannel):\n\n"
        "❗️ Убедись что бот уже добавлен в канал как администратор!",
        reply_markup=ReplyKeyboardRemove()
    )
    return AddChannelStep.USERNAME


async def addchannel_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.startswith("@"):
        text = "@" + text

    await update.message.reply_text(f"⏳ Проверяю канал {text}...")

    try:
        chat = await ctx.bot.get_chat(text)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не могу найти канал {text}\n\n"
            f"Причина: {e}\n\n"
            f"Убедись что:\n"
            f"• Username правильный\n"
            f"• Бот добавлен в канал как администратор\n\n"
            f"Попробуй снова или /cancel"
        )
        return AddChannelStep.USERNAME

    # Проверяем что это канал
    if chat.type != "channel":
        await update.message.reply_text(
            f"❌ {text} — это не канал. Нужно добавить именно канал.\n\n"
            f"Попробуй снова или /cancel"
        )
        return AddChannelStep.USERNAME

    # Ищем linked группу (обсуждение)
    discussion_id = None
    if chat.linked_chat_id:
        discussion_id = chat.linked_chat_id
        log.info(f"Найдена группа обсуждений: {discussion_id}")

    channel_info = {
        "channel_id": text,  # @username
        "username": text.lstrip("@"),
        "title": chat.title or text,
        "discussion_id": discussion_id
    }

    admin_id = update.effective_user.id
    added = add_channel_for_admin(admin_id, channel_info)

    if not added:
        await update.message.reply_text(
            f"⚠️ Канал {text} уже добавлен!\n\n"
            f"/channels — посмотреть все каналы",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    if discussion_id:
        # Проверяем что бот есть в группе обсуждений
        try:
            await ctx.bot.get_chat(discussion_id)
            status_text = f"✅ Группа обсуждений найдена и подключена"
        except:
            status_text = (
                f"⚠️ Группа обсуждений найдена (ID: {discussion_id}), "
                f"но бот не добавлен туда!\n"
                f"Добавь бота в группу обсуждений как администратора."
            )
    else:
        status_text = (
            f"⚠️ Группа обсуждений не найдена.\n"
            f"Подключи группу обсуждений к каналу в настройках канала, "
            f"затем добавь бота туда как администратора."
        )

    await update.message.reply_text(
        f"✅ Канал {chat.title} добавлен!\n\n"
        f"{status_text}\n\n"
        f"Теперь можешь создавать розыгрыши командой /giveaway",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ─── /channels ───────────────────────────────────────────────────────────────

async def cmd_channels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только для администраторов.")
        return

    channels = get_admin_channels(update.effective_user.id)
    if not channels:
        await update.message.reply_text(
            "У тебя нет добавленных каналов.\n\n"
            "/addchannel — добавить канал"
        )
        return

    lines = []
    for i, ch in enumerate(channels, 1):
        disc = f"✅ обсуждения подключены" if ch.get("discussion_id") else "❌ обсуждения не подключены"
        active = " 🔴 ИДЁТ РОЗЫГРЫШ" if ch["channel_id"] in sessions else ""
        lines.append(f"{i}. {ch.get('title', ch['username'])} (@{ch['username']})\n   {disc}{active}")

    await update.message.reply_text(
        "📢 Твои каналы:\n\n" + "\n\n".join(lines) + "\n\n"
        "/addchannel — добавить ещё канал"
    )


# ─── /giveaway ───────────────────────────────────────────────────────────────

async def cmd_giveaway(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только для администраторов.")
        return ConversationHandler.END

    channels = get_admin_channels(update.effective_user.id)

    if not channels:
        await update.message.reply_text(
            "❌ У тебя нет добавленных каналов!\n\n"
            "Сначала добавь канал командой /addchannel"
        )
        return ConversationHandler.END

    # Фильтруем каналы без активного розыгрыша
    available = [ch for ch in channels if ch["channel_id"] not in sessions]
    if not available:
        await update.message.reply_text(
            "❌ Во всех твоих каналах уже идут розыгрыши!\n\n"
            "/stop — остановить розыгрыш"
        )
        return ConversationHandler.END

    ctx.user_data.clear()

    # Если канал один — сразу выбираем его
    if len(available) == 1:
        ctx.user_data["channel"] = available[0]
        await update.message.reply_text(
            f"📢 Канал: {available[0].get('title', available[0]['username'])}\n\n"
            f"Шаг 1 из 4 — Что разыгрываем?\n\nНапиши название приза:",
            reply_markup=ReplyKeyboardRemove()
        )
        return Step.PRIZE

    # Если каналов несколько — спрашиваем
    keyboard = [[InlineKeyboardButton(
        ch.get("title", ch["username"]),
        callback_data=f"sel_ch:{i}"
    )] for i, ch in enumerate(available)]

    ctx.user_data["available_channels"] = available
    await update.message.reply_text(
        "📢 В какой канал публикуем розыгрыш?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return Step.SELECT_CHANNEL


async def callback_select_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":")[1])
    available = ctx.user_data.get("available_channels", [])
    if idx >= len(available):
        await query.edit_message_text("❌ Ошибка выбора канала.")
        return ConversationHandler.END

    ctx.user_data["channel"] = available[idx]
    ch = available[idx]

    await query.edit_message_text(
        f"📢 Канал: {ch.get('title', ch['username'])}\n\n"
        f"Шаг 1 из 4 — Что разыгрываем?\n\nНапиши название приза:"
    )
    return Step.PRIZE


# ─── Шаги создания розыгрыша ─────────────────────────────────────────────────

async def step_prize(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["prize"] = update.message.text.strip()
    await update.message.reply_text(
        f"Приз: {ctx.user_data['prize']}\n\n"
        f"Шаг 2 из 4 — Условия участия\n\nНапиши условия или выбери вариант:",
        reply_markup=ReplyKeyboardMarkup(
            [["Написать комментарий + реакция, однотипные нельзя"],
             ["Написать комментарий под постом"],
             ["Нету"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return Step.RULES


async def step_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["rules"] = "" if text == "Нету" else text
    await update.message.reply_text(
        "Шаг 3 из 4 — Время розыгрыша\n\nСколько минут?",
        reply_markup=ReplyKeyboardMarkup(
            [["15", "30", "60"], ["120", "1440"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return Step.DURATION


async def step_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        duration = int(update.message.text.strip())
        if not (1 <= duration <= 10080): raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число от 1 до 10080:")
        return Step.DURATION
    ctx.user_data["duration"] = duration
    await update.message.reply_text(
        f"Время: {duration} мин\n\nШаг 4 из 4 — Сколько победителей?",
        reply_markup=ReplyKeyboardMarkup(
            [["1", "2", "3"], ["5", "10"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return Step.WINNERS


async def step_winners(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        winners = int(update.message.text.strip())
        if not (1 <= winners <= 100): raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число от 1 до 100:")
        return Step.WINNERS
    ctx.user_data["winners"] = winners
    d = ctx.user_data
    ch = d["channel"]
    preview = build_post(d["prize"], d["rules"], d["winners"], d["duration"])
    await update.message.reply_text(
        f"📢 Канал: {ch.get('title', ch['username'])}\n\n"
        f"Предпросмотр поста:\n\n{preview}\n\n"
        f"-----------------\nПубликуем в канал?",
        reply_markup=ReplyKeyboardMarkup(
            [["Опубликовать", "Отмена"]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )
    return Step.CONFIRM


async def step_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "Отмена" in update.message.text:
        await update.message.reply_text("Создание отменено.", reply_markup=ReplyKeyboardRemove())
        ctx.user_data.clear()
        return ConversationHandler.END

    d, user = ctx.user_data, update.effective_user
    ch = d["channel"]
    post_text = build_post(d["prize"], d["rules"], d["winners"], d["duration"])

    # Проверяем группу обсуждений
    discussion_id = ch.get("discussion_id")
    if not discussion_id:
        await update.message.reply_text(
            f"❌ У канала {ch.get('title', ch['username'])} не подключена группа обсуждений!\n\n"
            f"Подключи группу обсуждений к каналу и добавь бота туда,\n"
            f"затем пересоздай канал через /addchannel",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    try:
        channel_msg = await ctx.bot.send_message(chat_id=ch["channel_id"], text=post_text)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не могу написать в канал:\n{e}",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

    # discussion_post_id = channel_post_id (Telegram создаёт тред с тем же ID)
    discussion_post_id = channel_msg.message_id
    log.info(f"channel_post_id={channel_msg.message_id}, discussion_post_id={discussion_post_id}")

    session = GiveawaySession(
        prize=d["prize"], rules=d["rules"],
        duration_min=d["duration"], winners_count=d["winners"],
        channel_id=ch["channel_id"],
        channel_username=ch["username"],
        discussion_id=discussion_id,
        discussion_post_id=discussion_post_id
    )
    sessions[ch["channel_id"]] = session

    # Приветственное сообщение сразу в тред поста
    try:
        await ctx.bot.send_message(
            chat_id=discussion_id,
            message_thread_id=discussion_post_id,
            text=(
                f"🎁 Розыгрыш начался!\n\n"
                f"💬 Пиши комментарий прямо здесь чтобы участвовать\n"
                f"⏰ Время: {d['duration']} мин\n"
                f"🏆 Победителей: {d['winners']}\n\n"
                f"⚡️ Больше шансов у тех кто напишет больше комментариев!"
            )
        )
    except Exception as e:
        log.error(f"Welcome comment error: {e}")

    await update.message.reply_text(
        f"✅ Пост опубликован!\n\n"
        f"📢 Канал: {ch.get('title', ch['username'])}\n"
        f"🎁 Приз: {d['prize']}\n"
        f"⏰ Время: {d['duration']} мин\n"
        f"🏆 Победителей: {d['winners']}\n\n"
        f"Результаты пришлю автоматически.",
        reply_markup=ReplyKeyboardRemove()
    )

    channel_key = ch["channel_id"]
    duration_sec = d["duration"] * 60
    if duration_sec > 300:
        ctx.job_queue.run_once(job_reminder, when=duration_sec - 300,
            data={"channel_key": channel_key},
            name=f"reminder_{channel_key}")
    ctx.job_queue.run_once(job_finish, when=duration_sec,
        data={"channel_key": channel_key, "admin_id": user.id,
              "admin_username": user.username or user.first_name},
        name=f"finish_{channel_key}")

    ctx.user_data.clear()
    return ConversationHandler.END


# ─── /stop ───────────────────────────────────────────────────────────────────

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Только для администраторов.")
        return

    # Найдём активные розыгрыши этого админа
    channels = get_admin_channels(update.effective_user.id)
    active = [ch for ch in channels if ch["channel_id"] in sessions]

    if not active:
        await update.message.reply_text("Активных розыгрышей нет.")
        return

    if len(active) == 1:
        ch = active[0]
        sessions.pop(ch["channel_id"])
        for name in [f"reminder_{ch['channel_id']}", f"finish_{ch['channel_id']}"]:
            for j in ctx.job_queue.get_jobs_by_name(name): j.schedule_removal()
        await update.message.reply_text(f"✅ Розыгрыш в {ch.get('title', ch['username'])} остановлен.")
        return

    # Несколько активных — спрашиваем
    keyboard = [[InlineKeyboardButton(
        ch.get("title", ch["username"]),
        callback_data=f"stop_ch:{ch['channel_id']}"
    )] for ch in active]
    await update.message.reply_text(
        "Какой розыгрыш остановить?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def callback_stop_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    channel_id = query.data.split(":", 1)[1]
    if channel_id not in sessions:
        await query.edit_message_text("❌ Розыгрыш уже завершён.")
        return

    sessions.pop(channel_id)
    for name in [f"reminder_{channel_id}", f"finish_{channel_id}"]:
        for j in ctx.job_queue.get_jobs_by_name(name): j.schedule_removal()
    await query.edit_message_text(f"✅ Розыгрыш остановлен.")


# ─── Обработка комментариев ───────────────────────────────────────────────────

async def handle_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return
    if msg.forward_origin is not None: return

    # Определяем к какой сессии относится комментарий
    session = None
    for ch_id, sess in sessions.items():
        if msg.chat.id == sess.discussion_id:
            # Проверяем что сообщение в нужном треде
            if msg.message_thread_id == sess.discussion_post_id:
                session = sess
                break

    if not session: return

    if msg.sender_chat is not None:
        uid      = msg.sender_chat.id
        name     = msg.sender_chat.title or "Аноним"
        username = msg.sender_chat.username or ""
    else:
        user = msg.from_user
        if not user or user.is_bot: return
        uid      = user.id
        name     = user.full_name
        username = user.username or ""

    now = datetime.now()
    session.register(uid, name, username, msg.message_id, now)
    log.info(f"Comment ACCEPTED: {name}, thread={msg.message_thread_id}")


# ─── Jobs ─────────────────────────────────────────────────────────────────────

async def job_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    channel_key = ctx.job.data["channel_key"]
    session = sessions.get(channel_key)
    if not session: return
    try:
        await ctx.bot.send_message(
            chat_id=session.discussion_id,
            message_thread_id=session.discussion_post_id,
            text=REMINDER_TEMPLATE.format(prize=session.prize)
        )
    except Exception as e:
        log.error(f"Reminder: {e}")


async def job_finish(ctx: ContextTypes.DEFAULT_TYPE):
    data = ctx.job.data
    channel_key = data["channel_key"]
    session = sessions.pop(channel_key, None)
    if not session: return

    winners = session.pick_winners()

    if winners:
        lines = []
        for i, w in enumerate(winners, 1):
            tag  = mention(w)
            link = msg_link(session.discussion_id, w["msg_id"])
            lines.append(f"{i}. {tag}\n   Сообщение: {link}")
        winners_text = "\n\n".join(lines)
    else:
        winners_text = "Никто не участвовал"

    result = RESULT_TEMPLATE.format(
        prize=session.prize,
        total=len(session.unique_users),
        winners_text=winners_text,
        admin_username=data["admin_username"]
    )

    log.info(f"Отправляю итоги в thread={session.discussion_post_id}")
    try:
        await ctx.bot.send_message(
            chat_id=session.discussion_id,
            message_thread_id=session.discussion_post_id,
            text=result,
            disable_web_page_preview=True
        )
        log.info("Итоги отправлены!")
    except Exception as e:
        log.error(f"Finish error: {e}")

    try:
        await ctx.bot.send_message(
            chat_id=data["admin_id"],
            text=f"Розыгрыш завершён!\n\n{result}",
            disable_web_page_preview=True
        )
    except Exception as e:
        log.error(f"Admin DM error: {e}")

    log.info(f"Done. Winners: {[w['name'] for w in winners]}")


# ─── Команды администратора ───────────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user: return
    args = ctx.args
    if not args:
        await update.message.reply_text("Использование: /admin [пароль]")
        return
    if args[0] != ADMIN_PASSWORD:
        await update.message.reply_text("❌ Неверный пароль.")
        return
    if user.id in ADMIN_IDS:
        await update.message.reply_text("✅ Ты уже администратор.")
        return
    ADMIN_IDS.append(user.id)
    log.info(f"Новый админ: {user.full_name} ({user.id})")
    await update.message.reply_text(
        f"✅ Ты теперь администратор!\n\n"
        f"Следующий шаг — добавь свой канал:\n"
        f"/addchannel"
    )


async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Только для администраторов.")
        return
    lines = [f"{i+1}. {uid}" + (" [root]" if i == 0 else "") for i, uid in enumerate(ADMIN_IDS)]
    await update.message.reply_text("Администраторы:\n" + "\n".join(lines))


async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_IDS[0]:
        await update.message.reply_text("❌ Только главный администратор может удалять.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Использование: /removeadmin [user_id]")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Введи числовой user_id.")
        return
    if uid == ADMIN_IDS[0]:
        await update.message.reply_text("❌ Нельзя удалить главного администратора.")
        return
    if uid in ADMIN_IDS:
        ADMIN_IDS.remove(uid)
        await update.message.reply_text(f"✅ Админ {uid} удалён.")
    else:
        await update.message.reply_text("❌ Такого админа нет.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ─── Health server ────────────────────────────────────────────────────────────

def run_health_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args): pass
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("", port), Handler).serve_forever()


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для добавления канала
    add_channel_conv = ConversationHandler(
        entry_points=[CommandHandler("addchannel", cmd_addchannel)],
        states={
            AddChannelStep.USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addchannel_username)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True, per_chat=True
    )

    # ConversationHandler для создания розыгрыша
    giveaway_conv = ConversationHandler(
        entry_points=[CommandHandler("giveaway", cmd_giveaway)],
        states={
            Step.SELECT_CHANNEL: [CallbackQueryHandler(callback_select_channel, pattern=r"^sel_ch:")],
            Step.PRIZE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_prize)],
            Step.RULES:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_rules)],
            Step.DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_duration)],
            Step.WINNERS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_winners)],
            Step.CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, step_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True, per_chat=True
    )

    app.add_handler(add_channel_conv)
    app.add_handler(giveaway_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CallbackQueryHandler(callback_stop_channel, pattern=r"^stop_ch:"))

    # Обработчик комментариев — слушаем ВСЕ группы где есть активные сессии
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
        handle_comment
    ))

    threading.Thread(target=run_health_server, daemon=True).start()
    log.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()