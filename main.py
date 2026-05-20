import asyncio
import sqlite3
import logging
import re
import time
import random
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, ChatMemberUpdated, ChatPermissions,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.enums import ChatMemberStatus, ChatType

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
OWNER_ID = 8675927241

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("replify")

# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════
conn = sqlite3.connect("replify.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
cur = conn.cursor()

cur.executescript("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id      INTEGER PRIMARY KEY,
    title        TEXT DEFAULT '',
    username     TEXT DEFAULT '',
    member_count INTEGER DEFAULT 0,
    welcome      TEXT DEFAULT '',
    farewell     TEXT DEFAULT '',
    rules        TEXT DEFAULT '',
    log_channel  INTEGER DEFAULT 0,
    fl_limit     INTEGER DEFAULT 5,
    fl_action    TEXT DEFAULT 'mute',
    f_links      INTEGER DEFAULT 0,
    f_caps       INTEGER DEFAULT 0,
    antiraid     INTEGER DEFAULT 0,
    locked       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bad_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, word TEXT
);
CREATE TABLE IF NOT EXISTS moderators (
    chat_id INTEGER, user_id INTEGER, rank TEXT DEFAULT 'Модератор',
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, user_id INTEGER, reason TEXT,
    ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stats (
    chat_id INTEGER, user_id INTEGER, name TEXT, username TEXT, msgs INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS economy (
    user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, last_bonus TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item TEXT
);
CREATE TABLE IF NOT EXISTS shop (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, description TEXT
);
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, keyword TEXT, response TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    user1 INTEGER, user2 INTEGER, PRIMARY KEY (user1, user2)
);
CREATE TABLE IF NOT EXISTS family (
    parent INTEGER, child INTEGER, PRIMARY KEY (parent, child)
);
CREATE TABLE IF NOT EXISTS bot_staff (
    user_id INTEGER PRIMARY KEY, rank_id INTEGER DEFAULT 9,
    rank_name TEXT DEFAULT 'Саппорт', name TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS factories (
    user_id      INTEGER PRIMARY KEY,
    level        INTEGER DEFAULT 1,
    workers      INTEGER DEFAULT 5,
    upgrades     INTEGER DEFAULT 0,
    last_collect TEXT DEFAULT ''
);
""")
conn.commit()

cur.execute("SELECT COUNT(*) FROM shop")
if cur.fetchone()[0] == 0:
    cur.executemany("INSERT INTO shop (name,price,description) VALUES (?,?,?)", [
        ("🎭 VIP-статус", 500, "Особый статус в инвентаре"),
        ("💎 Кристалл", 250, "Редкий предмет"),
        ("🍀 Амулет", 150, "Приносит удачу"),
        ("🎲 Кейс удачи", 100, "Случайный приз"),
    ])
    conn.commit()

# ══════════════════════════════════════════════
#  BOT STAFF RANKS
# ══════════════════════════════════════════════
RANKS = {
    1: "👑 Владелец",
    2: "👑 Владелец",
    3: "🔱 ЗВ (зам. владельца)",
    4: "🔱 ЗВ (зам. владельца)",
    5: "⚜️ ПВ (помощник владельца)",
    6: "🏅 ЗПВ (зам. помощника владельца)",
    7: "⭐ Администратор",
    8: "🎯 Куратор",
    9: "🔹 Саппорт (хелпер)",
}

def can_appoint(appointer_rank: int, target_rank: int) -> bool:
    return appointer_rank < target_rank

# ══════════════════════════════════════════════
#  DB HELPERS
# ══════════════════════════════════════════════
def db_exec(sql, params=()):
    cur.execute(sql, params)
    conn.commit()

def db_one(sql, params=()):
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None

def db_all(sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]

def get_chat(chat_id):
    row = db_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    if not row:
        db_exec("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        row = db_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    return row

def set_chat(chat_id, col, val):
    get_chat(chat_id)
    db_exec(f"UPDATE chats SET {col}=? WHERE chat_id=?", (val, chat_id))

def ensure_eco(uid):
    db_exec("INSERT OR IGNORE INTO economy (user_id) VALUES (?)", (uid,))

def get_bot_rank(uid) -> int:
    if uid == OWNER_ID:
        return 1
    row = db_one("SELECT rank_id FROM bot_staff WHERE user_id=?", (uid,))
    return row["rank_id"] if row else 99

def get_bot_rank_name(uid) -> str:
    if uid == OWNER_ID:
        return "👑 Владелец"
    row = db_one("SELECT rank_name FROM bot_staff WHERE user_id=?", (uid,))
    return row["rank_name"] if row else ""

def factory_income(level, workers, upgrades) -> int:
    return (level * 50 + workers * 10 + upgrades * 25)

def factory_upgrade_cost(level) -> int:
    return level * 500

def factory_worker_cost(workers) -> int:
    return workers * 100

# ══════════════════════════════════════════════
#  MISC
# ══════════════════════════════════════════════
def mn(uid, name):
    return f'<a href="tg://user?id={uid}">{name}</a>'

def is_group(m: Message):
    return m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def check_admin(bot, chat_id, user_id) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except:
        return False

async def check_mod(bot, chat_id, user_id) -> bool:
    if await check_admin(bot, chat_id, user_id):
        return True
    return bool(db_one("SELECT 1 FROM moderators WHERE chat_id=? AND user_id=?", (chat_id, user_id)))

async def do_log(bot, chat_id, text):
    ch = get_chat(chat_id).get("log_channel", 0)
    if ch:
        try:
            await bot.send_message(ch, f"📋 {text}", parse_mode="HTML")
        except:
            pass

# ══════════════════════════════════════════════
#  DECORATORS
# ══════════════════════════════════════════════
def group_only(func):
    @wraps(func)
    async def wrapper(message: Message, **kw):
        if not is_group(message):
            return await message.answer("❌ Только для групп.")
        return await func(message, **kw)
    return wrapper

def mod_only(func):
    @wraps(func)
    async def wrapper(message: Message, **kw):
        if not is_group(message):
            return
        if not await check_mod(bot, message.chat.id, message.from_user.id):
            return await message.answer("❌ Нет прав.")
        return await func(message, **kw)
    return wrapper

def admin_only(func):
    @wraps(func)
    async def wrapper(message: Message, **kw):
        if not is_group(message):
            return
        if not await check_admin(bot, message.chat.id, message.from_user.id):
            return await message.answer("❌ Только для администраторов.")
        return await func(message, **kw)
    return wrapper

def owner_only(func):
    @wraps(func)
    async def wrapper(message: Message, **kw):
        if message.from_user.id != OWNER_ID:
            return await message.answer("❌ Только для владельца бота.")
        return await func(message, **kw)
    return wrapper

def need_reply(func):
    @wraps(func)
    async def wrapper(message: Message, **kw):
        if not message.reply_to_message:
            return await message.answer("⚠️ Ответь на сообщение пользователя.")
        return await func(message, **kw)
    return wrapper

# ══════════════════════════════════════════════
#  MULTI-PREFIX PARSER
# ══════════════════════════════════════════════
PREFIXES = ('/', '.', '!', '-', '+')

def parse_cmd(text: str):
    """Возвращает (команда, аргументы) или None"""
    if not text:
        return None
    text = text.strip()
    # с префиксом
    for p in PREFIXES:
        if text.startswith(p):
            parts = text[len(p):].split()
            if parts:
                cmd = parts[0].split('@')[0].lower()
                args = parts[1:]
                return cmd, args
    # без префикса — только если первое слово = известная команда
    parts = text.split()
    cmd = parts[0].lower()
    return cmd, parts[1:]

# известные команды (заполняется ниже)
KNOWN_CMDS: set = set()

# ══════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
         InlineKeyboardButton(text="💰 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="🏭 Завод", callback_data="factory"),
         InlineKeyboardButton(text="🛒 Магазин", callback_data="shop")],
        [InlineKeyboardButton(text="🎲 Кейс", callback_data="case"),
         InlineKeyboardButton(text="🎁 Бонус", callback_data="bonus")],
        [InlineKeyboardButton(text="📊 Топ актива", callback_data="top"),
         InlineKeyboardButton(text="📋 Команды", callback_data="help")],
    ])

def kb_factory(uid):
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏭 Купить завод (500 💎)", callback_data="factory_buy")]
        ])
    income = factory_income(f["level"], f["workers"], f["upgrades"])
    up_cost = factory_upgrade_cost(f["level"])
    w_cost = factory_worker_cost(f["workers"])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⬆️ Прокачать (ур. {f['level']}→{f['level']+1}) за {up_cost} 💎", callback_data="factory_upgrade")],
        [InlineKeyboardButton(text=f"👷 Нанять рабочего за {w_cost} 💎", callback_data="factory_worker")],
        [InlineKeyboardButton(text=f"💰 Собрать доход (~{income} 💎/ч)", callback_data="factory_collect")],
        [InlineKeyboardButton(text="📈 Продать завод", callback_data="factory_sell")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")],
    ])

def kb_shop():
    rows = db_all("SELECT id,name,price FROM shop", ())
    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(
            text=f"{r['name']} — {r['price']} 💎",
            callback_data=f"buy_{r['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
    ])

# ══════════════════════════════════════════════
#  FLOOD TRACKER
# ══════════════════════════════════════════════
flood: dict = defaultdict(list)

# ══════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# ══════════════════════════════════════════════
#  BOT JOIN
# ══════════════════════════════════════════════
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_bot_join(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    get_chat(chat.id)
    set_chat(chat.id, "title", chat.title or "")
    set_chat(chat.id, "username", chat.username or "")
    try:
        await bot.promote_chat_member(
            chat_id=chat.id, user_id=OWNER_ID,
            can_manage_chat=True, can_delete_messages=True,
            can_manage_video_chats=True, can_restrict_members=True,
            can_promote_members=True, can_change_info=True,
            can_invite_users=True, can_pin_messages=True, is_anonymous=False,
        )
        await bot.set_chat_administrator_custom_title(chat.id, OWNER_ID, "Владелец")
    except Exception as e:
        log.warning(f"Не выдал права в {chat.id}: {e}")

# ══════════════════════════════════════════════
#  WELCOME / FAREWELL
# ══════════════════════════════════════════════
@router.chat_member()
async def on_member_update(event: ChatMemberUpdated):
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    user = event.new_chat_member.user
    cid = event.chat.id
    chat = get_chat(cid)

    joined = (old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new == ChatMemberStatus.MEMBER)
    left = (old == ChatMemberStatus.MEMBER and new in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED))

    if joined:
        if chat.get("antiraid"):
            try:
                await bot.ban_chat_member(cid, user.id)
                await bot.unban_chat_member(cid, user.id)
            except:
                pass
            return
        welcome = chat.get("welcome", "")
        if welcome:
            text = welcome.replace("{user}", mn(user.id, user.full_name)).replace("{chat}", event.chat.title or "")
            try:
                await bot.send_message(cid, text)
            except:
                pass
    elif left:
        farewell = chat.get("farewell", "")
        if farewell:
            text = farewell.replace("{user}", mn(user.id, user.full_name)).replace("{chat}", event.chat.title or "")
            try:
                await bot.send_message(cid, text)
            except:
                pass

# ══════════════════════════════════════════════
#  GROUP MESSAGE HANDLER
# ══════════════════════════════════════════════
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_group_message(message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    cid = message.chat.id
    text = message.text or message.caption or ""

    # stats
    db_exec("""
        INSERT INTO stats (chat_id,user_id,name,username,msgs) VALUES (?,?,?,?,1)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            msgs=msgs+1, name=excluded.name, username=excluded.username
    """, (cid, uid, message.from_user.full_name, message.from_user.username or ""))

    # multi-prefix commands
    parsed = parse_cmd(text)
    if parsed:
        cmd, args = parsed
        if text.startswith('/') or any(text.startswith(p) for p in PREFIXES[1:]) or cmd in KNOWN_CMDS:
            handled = await handle_alias_cmd(message, cmd, args)
            if handled:
                return

    if text.startswith('/'):
        return
    if await check_mod(bot, cid, uid):
        await check_triggers(message, cid, text)
        return

    chat = get_chat(cid)

    if chat.get("antiraid"):
        try:
            await message.delete()
        except:
            pass
        return

    # bad words
    bad = db_all("SELECT word FROM bad_words WHERE chat_id=?", (cid,))
    for row in bad:
        if row["word"].lower() in text.lower():
            try:
                await message.delete()
                await message.answer(f"🚫 {mn(uid, message.from_user.full_name)}, сообщение удалено.")
            except:
                pass
            return

    # link filter
    if chat.get("f_links") and re.search(r"(https?://|t\.me/|www\.)", text, re.I):
        try:
            await message.delete()
            await message.answer(f"🔗 {mn(uid, message.from_user.full_name)}, ссылки запрещены.")
        except:
            pass
        return

    # caps filter
    if chat.get("f_caps") and len(text) > 10:
        letters = [c for c in text if c.isalpha()]
        if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
            try:
                await message.delete()
                await message.answer(f"🔠 {mn(uid, message.from_user.full_name)}, не пиши заглавными.")
            except:
                pass
            return

    # antiflood
    limit = chat.get("fl_limit") or 5
    now = time.time()
    key = (cid, uid)
    flood[key] = [t for t in flood[key] if now - t < 5]
    flood[key].append(now)
    if len(flood[key]) >= limit:
        flood[key] = []
        action = chat.get("fl_action") or "mute"
        name = mn(uid, message.from_user.full_name)
        try:
            await message.delete()
        except:
            pass
        try:
            if action == "ban":
                await bot.ban_chat_member(cid, uid)
                await message.answer(f"🚫 {name} забанен за флуд.")
            elif action == "kick":
                await bot.ban_chat_member(cid, uid)
                await bot.unban_chat_member(cid, uid)
                await message.answer(f"👢 {name} выкинут за флуд.")
            else:
                until = datetime.now() + timedelta(minutes=5)
                await bot.restrict_chat_member(cid, uid,
                    permissions=ChatPermissions(can_send_messages=False), until_date=until)
                await message.answer(f"🔇 {name} замучен за флуд на 5 минут.")
        except:
            pass
        return

    await check_triggers(message, cid, text)

async def check_triggers(message: Message, cid: int, text: str):
    if not text:
        return
    rows = db_all("SELECT keyword,response FROM triggers WHERE chat_id=?", (cid,))
    for row in rows:
        if row["keyword"].lower() in text.lower():
            try:
                await message.answer(row["response"])
            except:
                pass
            break

async def handle_alias_cmd(message: Message, cmd: str, args: list) -> bool:
    """Обрабатывает команды с альтернативными префиксами"""
    handlers = {
        "старт": do_start, "start": do_start,
        "помощь": do_help, "help": do_help, "команды": do_help,
        "профиль": do_profile, "profile": do_profile, "кто": do_profile,
        "баланс": do_balance, "balance": do_balance,
        "бонус": do_bonus, "bonus": do_bonus,
        "топ": do_top, "top": do_top,
        "чат": do_chatinfo, "chatinfo": do_chatinfo,
        "стата": do_stat, "stat": do_stat,
        "магазин": do_shop_cmd, "shop": do_shop_cmd,
        "инвентарь": do_inventory, "инв": do_inventory,
        "кейс": do_case, "case": do_case,
        "правила": do_rules, "rules": do_rules,
        "триггеры": do_triggers, "triggers": do_triggers,
        "пинг": do_ping, "ping": do_ping,
        "ид": do_id, "id": do_id,
        "завод": do_factory, "factory": do_factory,
        "топзавод": do_factory_top,
        "семья": do_family, "family": do_family,
        "стафф": do_staff, "staff": do_staff,
        "должности": do_bot_staff,
        "чаты": do_chats,
    }
    if cmd in handlers:
        await handlers[cmd](message, args)
        return True
    return False

# ══════════════════════════════════════════════
#  CALLABLE HANDLERS (for alias system)
# ══════════════════════════════════════════════
async def do_start(message: Message, args=None):
    uid = message.from_user.id
    rank_name = get_bot_rank_name(uid)
    rank_line = f"\n🎖 Твоя должность: <b>{rank_name}</b>" if rank_name else ""
    text = (
        f"👋 Привет, {mn(uid, message.from_user.full_name)}!\n\n"
        f"🤖 <b>Replify | Чат-менеджер</b>{rank_line}\n\n"
        f"Я помогу управлять твоим чатом:\n"
        f"🛡 Модерация и автозащита\n"
        f"💰 Экономика и игры\n"
        f"🏭 Заводы и бизнес\n"
        f"🎭 RP-команды и развлечения\n\n"
        f"Используй кнопки ниже или /помощь"
    )
    await message.answer(text, reply_markup=kb_main())

async def do_help(message: Message, args=None):
    await message.answer(
        "📋 <b>Replify | Команды</b>\n\n"
        "<b>👥 Информация</b>\n"
        "/админы /стафф /чат /профиль /топ /стата /пинг /ид /чаты\n\n"
        "<b>🔨 Модерация</b>\n"
        "/бан /разбан /мут [мин] /размут /кик\n"
        "/варн /варны /снятьварны\n"
        "/очистить [N] /заморозить /разморозить /пин /анпин\n\n"
        "<b>🤖 Автомод</b>\n"
        "/фильтрссылок вкл|выкл\n"
        "/фильтркапс вкл|выкл\n"
        "/антифлуд [лимит] [бан|мут|кик]\n"
        "/антирейд вкл|выкл\n"
        "/запретитьслово /разрешитьслово /запрещённые\n\n"
        "<b>⭐ Ранги в чате</b>\n"
        "/назначить [ранг] /снятьмодера /ранг [звание]\n\n"
        "<b>📣 Приветствие</b>\n"
        "/приветствие [текст] /прощание [текст] /правила [текст]\n"
        "Переменные: {user} {chat}\n\n"
        "<b>🎯 Триггеры</b>\n"
        "/триггер [слово] [ответ] /удалитьтриггер /триггеры\n\n"
        "<b>💰 Экономика</b>\n"
        "/баланс /бонус /перевести [сумма]\n"
        "/магазин /купить [id] /инвентарь /кейс\n\n"
        "<b>🏭 Завод</b>\n"
        "/завод /купитьзавод /прокачать /рабочие\n"
        "/собрать /продатьзавод /топзавод\n\n"
        "<b>🎭 RP-команды</b>\n"
        "/обнять /поцеловать /ударить /погладить /укусить\n"
        "/подмигнуть /пнуть /обидеть /приобнять /потрепать\n"
        "/зарыть /швырнуть /лизнуть /укачать /потискать\n\n"
        "<b>💑 Социальные</b>\n"
        "/жениться /развестись /семья /усыновить\n\n"
        "<i>Команды можно писать с / . ! - + или без префикса</i>",
        reply_markup=kb_main()
    )

async def do_ping(message: Message, args=None):
    await message.answer("🟢 Replify работает!")

async def do_id(message: Message, args=None):
    t = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    await message.answer(f"🆔 <b>{t.full_name}</b>: <code>{t.id}</code>")

async def do_profile(message: Message, args=None):
    if not is_group(message):
        uid = message.from_user.id
        eco = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))
        rank = get_bot_rank_name(uid)
        text = (f"👤 <b>Профиль</b> {mn(uid, message.from_user.full_name)}\n\n"
                f"🆔 ID: <code>{uid}</code>\n")
        if rank:
            text += f"🎖 Должность: <b>{rank}</b>\n"
        text += f"💰 Баланс: <b>{eco['balance'] if eco else 0} 💎</b>"
        return await message.answer(text, reply_markup=kb_back())
    t = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    member = await bot.get_chat_member(message.chat.id, t.id)
    sm = {
        ChatMemberStatus.CREATOR: "👑 Создатель",
        ChatMemberStatus.ADMINISTRATOR: "⭐ Администратор",
        ChatMemberStatus.MEMBER: "👤 Участник",
        ChatMemberStatus.RESTRICTED: "🔇 Ограничен",
        ChatMemberStatus.LEFT: "🚪 Покинул",
        ChatMemberStatus.BANNED: "🚫 Забанен",
    }.get(member.status, "👤")
    warns = db_one("SELECT COUNT(*) as c FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    st = db_one("SELECT msgs FROM stats WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    eco = db_one("SELECT balance FROM economy WHERE user_id=?", (t.id,))
    mod = db_one("SELECT rank FROM moderators WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    bot_rank = get_bot_rank_name(t.id)
    text = (f"👤 <b>Профиль</b> {mn(t.id, t.full_name)}\n\n"
            f"🆔 ID: <code>{t.id}</code>\n"
            f"📌 Статус: {sm}\n")
    if mod:
        text += f"🎖 Ранг в чате: <b>{mod['rank']}</b>\n"
    if bot_rank:
        text += f"🏅 Должность в боте: <b>{bot_rank}</b>\n"
    text += (f"💬 Сообщений: <b>{st['msgs'] if st else 0}</b>\n"
             f"⚠️ Варнов: <b>{warns['c'] if warns else 0}/3</b>\n"
             f"💰 Баланс: <b>{eco['balance'] if eco else 0} 💎</b>\n")
    if t.username:
        text += f"🔗 @{t.username}"
    await message.answer(text)

async def do_balance(message: Message, args=None):
    uid = message.from_user.id
    ensure_eco(uid)
    row = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))
    await message.answer(f"💰 Твой баланс: <b>{row['balance']} 💎</b>", reply_markup=kb_back())

async def do_bonus(message: Message, args=None):
    uid = message.from_user.id
    ensure_eco(uid)
    row = db_one("SELECT balance,last_bonus FROM economy WHERE user_id=?", (uid,))
    last_bonus = row["last_bonus"] or ""
    if last_bonus:
        try:
            last_dt = datetime.fromisoformat(last_bonus)
            diff = datetime.now() - last_dt
            if diff < timedelta(hours=24):
                rem = timedelta(hours=24) - diff
                h = int(rem.total_seconds()) // 3600
                m = (int(rem.total_seconds()) % 3600) // 60
                return await message.answer(f"⏳ Следующий бонус через <b>{h}ч {m}мин</b>.")
        except:
            pass
    amount = random.randint(50, 200)
    db_exec("UPDATE economy SET balance=balance+?, last_bonus=? WHERE user_id=?",
            (amount, datetime.now().isoformat(), uid))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"🎁 Получено <b>{amount} 💎</b>!\n💰 Баланс: <b>{new_bal} 💎</b>")

async def do_top(message: Message, args=None):
    if not is_group(message):
        return await message.answer("❌ Только в группах.")
    rows = db_all("SELECT user_id,name,msgs FROM stats WHERE chat_id=? ORDER BY msgs DESC LIMIT 10",
                  (message.chat.id,))
    if not rows:
        return await message.answer("📊 Статистики пока нет.")
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else str(i+1)+'.'} {mn(r['user_id'], r['name'])} — <b>{r['msgs']}</b>"
             for i, r in enumerate(rows)]
    await message.answer("📊 <b>Топ актива:</b>\n\n" + "\n".join(lines))

async def do_stat(message: Message, args=None):
    if not is_group(message):
        return await message.answer("❌ Только в группах.")
    t = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    row = db_one("SELECT msgs FROM stats WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    await message.answer(f"📊 {mn(t.id, t.full_name)} — <b>{row['msgs'] if row else 0}</b> сообщений")

async def do_chatinfo(message: Message, args=None):
    if not is_group(message):
        return await message.answer("❌ Только в группах.")
    chat = await bot.get_chat(message.chat.id)
    admins = await bot.get_chat_administrators(message.chat.id)
    ac = sum(1 for a in admins if not a.user.is_bot)
    text = (f"💬 <b>{chat.title}</b>\n\n"
            f"🆔 ID: <code>{chat.id}</code>\n"
            f"👥 Участников: <b>{chat.member_count}</b>\n"
            f"👑 Админов: <b>{ac}</b>\n"
            f"🔗 Тип: <b>{'Супергруппа' if message.chat.type == ChatType.SUPERGROUP else 'Группа'}</b>\n")
    if chat.username:
        text += f"📎 @{chat.username}\n"
    if chat.description:
        text += f"\n📄 <i>{chat.description[:200]}</i>"
    await message.answer(text)

async def do_shop_cmd(message: Message, args=None):
    rows = db_all("SELECT id,name,price,description FROM shop", ())
    if not rows:
        return await message.answer("🛒 Магазин пуст.")
    lines = [f"<b>{r['id']}.</b> {r['name']} — <b>{r['price']} 💎</b>\n<i>{r['description']}</i>" for r in rows]
    await message.answer("🛒 <b>Магазин:</b>\n\n" + "\n\n".join(lines), reply_markup=kb_shop())

async def do_inventory(message: Message, args=None):
    uid = message.from_user.id
    rows = db_all("SELECT item,COUNT(*) as cnt FROM inventory WHERE user_id=? GROUP BY item", (uid,))
    if not rows:
        return await message.answer("🎒 Инвентарь пуст.", reply_markup=kb_back())
    await message.answer("🎒 <b>Инвентарь:</b>\n" + "\n".join(f"• {r['item']} x{r['cnt']}" for r in rows),
                         reply_markup=kb_back())

async def do_case(message: Message, args=None):
    uid = message.from_user.id
    ensure_eco(uid)
    cost = 50
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await message.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.")
    prizes = [
        ("💎 Кристалл", 5), ("🍀 Амулет", 10), ("🎭 VIP-статус", 2),
        ("💰 200 монет", 15), ("💰 100 монет", 28), ("💰 50 монет", 40),
    ]
    total = sum(w for _, w in prizes)
    roll = random.uniform(0, total)
    cum = 0; prize = prizes[-1][0]
    for name, weight in prizes:
        cum += weight
        if roll <= cum:
            prize = name; break
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    if "монет" in prize:
        coins = int(prize.split()[1])
        db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (coins, uid))
    else:
        db_exec("INSERT INTO inventory (user_id,item) VALUES (?,?)", (uid, prize))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"🎲 Выпало: <b>{prize}</b>!\n💰 Баланс: <b>{new_bal} 💎</b>")

async def do_rules(message: Message, args=None):
    if not is_group(message):
        return
    if args and await check_admin(bot, message.chat.id, message.from_user.id):
        set_chat(message.chat.id, "rules", " ".join(args))
        return await message.answer("✅ Правила установлены.")
    rules = get_chat(message.chat.id).get("rules", "")
    await message.answer(f"📜 <b>Правила:</b>\n\n{rules}" if rules else "📜 Правила не установлены.")

async def do_triggers(message: Message, args=None):
    if not is_group(message):
        return
    rows = db_all("SELECT keyword,response FROM triggers WHERE chat_id=?", (message.chat.id,))
    if not rows:
        return await message.answer("📋 Триггеров нет.")
    lines = [f"• <b>{r['keyword']}</b> → {r['response'][:50]}" for r in rows]
    await message.answer("🎯 <b>Триггеры:</b>\n\n" + "\n".join(lines))

async def do_staff(message: Message, args=None):
    if not is_group(message):
        return
    admins = await bot.get_chat_administrators(message.chat.id)
    lines = []
    for a in admins:
        if a.user.is_bot:
            continue
        title = getattr(a, "custom_title", None)
        role = title or ("👑 Создатель" if a.status == ChatMemberStatus.CREATOR else "⭐ Администратор")
        lines.append(f"• {mn(a.user.id, a.user.full_name)} — <i>{role}</i>")
    mods = db_all("SELECT user_id,rank FROM moderators WHERE chat_id=?", (message.chat.id,))
    for r in mods:
        lines.append(f"• <code>{r['user_id']}</code> — <i>{r['rank']}</i>")
    await message.answer("🛡 <b>Стафф чата:</b>\n\n" + "\n".join(lines))

async def do_bot_staff(message: Message, args=None):
    rows = db_all("SELECT user_id,rank_id,rank_name,name FROM bot_staff ORDER BY rank_id", ())
    lines = [f"• {r['rank_name']} — {mn(r['user_id'], r['name'] or str(r['user_id']))}" for r in rows]
    # Добавить владельца
    owner_line = f"• 👑 Владелец — {mn(OWNER_ID, 'Владелец бота')}"
    await message.answer(f"👑 <b>Должности в боте:</b>\n\n{owner_line}\n" + "\n".join(lines) if lines
                         else f"👑 <b>Должности в боте:</b>\n\n{owner_line}\n\n<i>Стафф не назначен</i>")

async def do_chats(message: Message, args=None):
    """Список чатов где есть бот"""
    rows = db_all("SELECT * FROM chats ORDER BY member_count DESC", ())
    if not rows:
        return await message.answer("📋 Бот не добавлен ни в один чат.")

    # топ по активности
    top_stats = db_all("""
        SELECT chat_id, SUM(msgs) as total FROM stats
        GROUP BY chat_id ORDER BY total DESC LIMIT 3
    """)
    top_ids = [r["chat_id"] for r in top_stats]

    lines = []
    for i, r in enumerate(rows, 1):
        link = f"@{r['username']}" if r.get("username") else f"<code>{r['chat_id']}</code>"
        trophy = " 🏆" if r["chat_id"] in top_ids else ""
        lines.append(
            f"<b>{i}.</b> {r['title'] or 'Без названия'}{trophy}\n"
            f"   👥 {r['member_count']} участников | 🆔 {link}"
        )

    owner_link = mn(OWNER_ID, "Владелец")
    text = (f"💬 <b>Чаты где есть Replify</b> ({len(rows)}):\n\n" +
            "\n\n".join(lines) +
            f"\n\n👑 Владелец: {owner_link}" +
            f"\n\n🏆 Топ-3 самых активных чатов получат подарки!")
    await message.answer(text)

# ══════════════════════════════════════════════
#  FACTORY GAME
# ══════════════════════════════════════════════
async def do_factory(message: Message, args=None):
    uid = message.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await message.answer(
            "🏭 <b>Завод</b>\n\nУ тебя нет завода!\nКупи его за <b>500 💎</b>.",
            reply_markup=kb_factory(uid))
    income = factory_income(f["level"], f["workers"], f["upgrades"])
    up_cost = factory_upgrade_cost(f["level"])
    w_cost = factory_worker_cost(f["workers"])
    sell_price = int((f["level"] * 400 + f["workers"] * 80 + f["upgrades"] * 200) * 0.7)

    last = f["last_collect"] or ""
    can_collect = True
    collect_info = "Готово к сбору!"
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            diff = datetime.now() - last_dt
            if diff < timedelta(hours=1):
                rem = timedelta(hours=1) - diff
                m = int(rem.total_seconds()) // 60
                s = int(rem.total_seconds()) % 60
                can_collect = False
                collect_info = f"Через {m}мин {s}сек"
        except:
            pass

    text = (f"🏭 <b>Твой завод</b>\n\n"
            f"📊 Уровень: <b>{f['level']}</b>\n"
            f"👷 Рабочих: <b>{f['workers']}</b>\n"
            f"⚙️ Улучшений: <b>{f['upgrades']}</b>\n"
            f"💰 Доход: <b>{income} 💎/час</b>\n"
            f"⏰ Сбор: <b>{collect_info}</b>\n\n"
            f"⬆️ Прокачка: {up_cost} 💎\n"
            f"👷 Рабочий: {w_cost} 💎\n"
            f"📈 Продажа: ~{sell_price} 💎")
    await message.answer(text, reply_markup=kb_factory(uid))

async def do_factory_top(message: Message, args=None):
    rows = db_all("""
        SELECT f.user_id, f.level, f.workers, f.upgrades,
               COALESCE(e.balance,0) as balance
        FROM factories f LEFT JOIN economy e ON f.user_id=e.user_id
        ORDER BY f.level DESC, f.workers DESC LIMIT 10
    """)
    if not rows:
        return await message.answer("🏭 Никто ещё не купил завод.")
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        income = factory_income(r["level"], r["workers"], r["upgrades"])
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <code>{r['user_id']}</code> — ур.<b>{r['level']}</b> | {income} 💎/ч")
    await message.answer("🏭 <b>Топ заводов:</b>\n\n" + "\n".join(lines))

# ══════════════════════════════════════════════
#  BOT STAFF COMMANDS
# ══════════════════════════════════════════════
async def do_family(message: Message, args=None):
    uid = message.from_user.id
    rel = db_one("SELECT user1,user2 FROM relationships WHERE user1=? OR user2=?", (uid, uid))
    children = db_all("SELECT child FROM family WHERE parent=?", (uid,))
    text = f"👨‍👩‍👧 <b>Семья</b> {mn(uid, message.from_user.full_name)}\n\n"
    if rel:
        pid = rel["user2"] if rel["user1"] == uid else rel["user1"]
        text += f"💑 Партнёр: <code>{pid}</code>\n"
    else:
        text += "💑 Партнёра нет\n"
    if children:
        text += f"👶 Детей: {len(children)}\n" + "".join(f"  • <code>{r['child']}</code>\n" for r in children)
    await message.answer(text)

# ══════════════════════════════════════════════
#  COMMAND HANDLERS (aiogram)
# ══════════════════════════════════════════════
@router.message(Command(commands=["start", "старт"]))
async def cmd_start(message: Message):
    await do_start(message)

@router.message(Command(commands=["help", "помощь", "команды"]))
async def cmd_help(message: Message):
    await do_help(message)

@router.message(Command(commands=["ping", "пинг"]))
async def cmd_ping(message: Message):
    await do_ping(message)

@router.message(Command(commands=["id", "ид"]))
async def cmd_id(message: Message):
    await do_id(message)

@router.message(Command(commands=["профиль", "profile", "кто"]))
async def cmd_profile(message: Message):
    await do_profile(message)

@router.message(Command(commands=["баланс", "balance"]))
async def cmd_balance(message: Message):
    await do_balance(message)

@router.message(Command(commands=["бонус", "bonus", "daily"]))
async def cmd_bonus(message: Message):
    await do_bonus(message)

@router.message(Command(commands=["топ", "top"]))
async def cmd_top(message: Message):
    await do_top(message)

@router.message(Command(commands=["стата", "stat"]))
async def cmd_stat(message: Message):
    await do_stat(message)

@router.message(Command(commands=["чат", "chatinfo"]))
async def cmd_chatinfo(message: Message):
    await do_chatinfo(message)

@router.message(Command(commands=["магазин", "shop"]))
async def cmd_shop(message: Message):
    await do_shop_cmd(message)

@router.message(Command(commands=["инвентарь", "инв", "inventory"]))
async def cmd_inventory(message: Message):
    await do_inventory(message)

@router.message(Command(commands=["кейс", "case"]))
async def cmd_case(message: Message):
    await do_case(message)

@router.message(Command(commands=["правила", "rules"]))
async def cmd_rules(message: Message):
    args = message.text.split(maxsplit=1)
    await do_rules(message, args[1:] if len(args) > 1 else [])

@router.message(Command(commands=["триггеры", "triggers"]))
async def cmd_triggers(message: Message):
    await do_triggers(message)

@router.message(Command(commands=["стафф", "staff"]))
async def cmd_staff(message: Message):
    await do_staff(message)

@router.message(Command(commands=["должности", "боттим"]))
async def cmd_bot_staff(message: Message):
    await do_bot_staff(message)

@router.message(Command(commands=["чаты", "mychats_pub"]))
async def cmd_chats(message: Message):
    await do_chats(message)

@router.message(Command(commands=["завод", "factory"]))
async def cmd_factory(message: Message):
    await do_factory(message)

@router.message(Command(commands=["топзавод", "factorytop"]))
async def cmd_factory_top(message: Message):
    await do_factory_top(message)

@router.message(Command(commands=["семья", "family"]))
async def cmd_family(message: Message):
    await do_family(message)

@router.message(Command(commands=["админы", "admins"]))
@group_only
async def cmd_admins(message: Message):
    admins = await bot.get_chat_administrators(message.chat.id)
    lines = []
    for a in admins:
        if a.user.is_bot:
            continue
        title = getattr(a, "custom_title", None)
        role = title or ("👑 Создатель" if a.status == ChatMemberStatus.CREATOR else "⭐ Администратор")
        lines.append(f"• {mn(a.user.id, a.user.full_name)} — <i>{role}</i>")
    await message.answer(f"👑 <b>Администраторы</b> ({len(lines)}):\n\n" + "\n".join(lines))

# ── Moderation ────────────────────────────────
@router.message(Command(commands=["бан", "ban"]))
@mod_only
@need_reply
async def cmd_ban(message: Message):
    t = message.reply_to_message.from_user
    reason = " ".join(message.text.split()[1:]) or "Без причины"
    try:
        await bot.ban_chat_member(message.chat.id, t.id)
        await message.answer(f"🚫 {mn(t.id, t.full_name)} забанен.\n📌 {reason}")
        await do_log(bot, message.chat.id, f"БАН {t.full_name} ({t.id}) | {reason}")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["разбан", "unban"]))
@mod_only
@need_reply
async def cmd_unban(message: Message):
    t = message.reply_to_message.from_user
    try:
        await bot.unban_chat_member(message.chat.id, t.id)
        await message.answer(f"✅ {mn(t.id, t.full_name)} разбанен.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["мут", "mute"]))
@mod_only
@need_reply
async def cmd_mute(message: Message):
    t = message.reply_to_message.from_user
    args = message.text.split()
    until, dur = None, "навсегда"
    if len(args) > 1 and args[1].isdigit():
        until = datetime.now() + timedelta(minutes=int(args[1]))
        dur = f"на {args[1]} мин."
    try:
        await bot.restrict_chat_member(message.chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=False), until_date=until)
        await message.answer(f"🔇 {mn(t.id, t.full_name)} замучен {dur}.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["размут", "unmute"]))
@mod_only
@need_reply
async def cmd_unmute(message: Message):
    t = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(message.chat.id, t.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        await message.answer(f"🔊 {mn(t.id, t.full_name)} размучен.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["кик", "kick"]))
@mod_only
@need_reply
async def cmd_kick(message: Message):
    t = message.reply_to_message.from_user
    try:
        await bot.ban_chat_member(message.chat.id, t.id)
        await bot.unban_chat_member(message.chat.id, t.id)
        await message.answer(f"👢 {mn(t.id, t.full_name)} выкинут.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["варн", "warn"]))
@mod_only
@need_reply
async def cmd_warn(message: Message):
    t = message.reply_to_message.from_user
    reason = " ".join(message.text.split()[1:]) or "Без причины"
    db_exec("INSERT INTO warns (chat_id,user_id,reason) VALUES (?,?,?)", (message.chat.id, t.id, reason))
    cnt = db_one("SELECT COUNT(*) as c FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))["c"]
    await message.answer(f"⚠️ {mn(t.id, t.full_name)} — варн!\n📌 {reason}\n📊 {cnt}/3")
    if cnt >= 3:
        try:
            await bot.ban_chat_member(message.chat.id, t.id)
            db_exec("DELETE FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
            await message.answer(f"🚫 {mn(t.id, t.full_name)} забанен за 3 варна.")
        except:
            pass

@router.message(Command(commands=["варны", "warns"]))
@group_only
async def cmd_warns(message: Message):
    t = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    rows = db_all("SELECT reason,ts FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    if not rows:
        return await message.answer(f"✅ У {mn(t.id, t.full_name)} нет варнов.")
    lines = [f"{i+1}. {r['reason']} — <i>{r['ts']}</i>" for i, r in enumerate(rows)]
    await message.answer(f"⚠️ Варны {mn(t.id, t.full_name)} ({len(rows)}/3):\n\n" + "\n".join(lines))

@router.message(Command(commands=["снятьварны", "clearwarns"]))
@mod_only
@need_reply
async def cmd_clearwarns(message: Message):
    t = message.reply_to_message.from_user
    db_exec("DELETE FROM warns WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    await message.answer(f"✅ Варны {mn(t.id, t.full_name)} сняты.")

@router.message(Command(commands=["очистить", "purge"]))
@mod_only
async def cmd_purge(message: Message):
    args = message.text.split()
    try:
        count = min(int(args[1]), 100) if len(args) > 1 else 10
    except:
        count = 10
    deleted = 0
    for i in range(count + 1):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
            deleted += 1
        except:
            pass
    m = await message.answer(f"🗑 Удалено {deleted} сообщений.")
    await asyncio.sleep(3)
    try:
        await m.delete()
    except:
        pass

@router.message(Command(commands=["заморозить", "lock"]))
@admin_only
async def cmd_lock(message: Message):
    try:
        await bot.set_chat_permissions(message.chat.id, ChatPermissions(can_send_messages=False))
        set_chat(message.chat.id, "locked", 1)
        await message.answer("🔒 Чат заморожен.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["разморозить", "unlock"]))
@admin_only
async def cmd_unlock(message: Message):
    try:
        await bot.set_chat_permissions(message.chat.id,
            ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        set_chat(message.chat.id, "locked", 0)
        await message.answer("🔓 Чат открыт.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["пин", "pin"]))
@mod_only
@need_reply
async def cmd_pin(message: Message):
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.answer("📌 Закреплено.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["анпин", "unpin"]))
@mod_only
async def cmd_unpin(message: Message):
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.answer("📌 Откреплено.")
    except Exception as e:
        await message.answer(f"❌ {e}")

# ── Automod settings ──────────────────────────
@router.message(Command("фильтрссылок"))
@admin_only
async def cmd_flinks(message: Message):
    args = message.text.split()
    val = 1 if len(args) > 1 and args[1] == "вкл" else 0
    set_chat(message.chat.id, "f_links", val)
    await message.answer(f"🔗 Фильтр ссылок: {'✅ включён' if val else '❌ выключен'}")

@router.message(Command("фильтркапс"))
@admin_only
async def cmd_fcaps(message: Message):
    args = message.text.split()
    val = 1 if len(args) > 1 and args[1] == "вкл" else 0
    set_chat(message.chat.id, "f_caps", val)
    await message.answer(f"🔠 Антикапс: {'✅ включён' if val else '❌ выключен'}")

@router.message(Command("антифлуд"))
@admin_only
async def cmd_antiflood(message: Message):
    args = message.text.split()
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
    action_map = {"бан": "ban", "мут": "mute", "кик": "kick"}
    action_ru = args[2] if len(args) > 2 else "мут"
    set_chat(message.chat.id, "fl_limit", limit)
    set_chat(message.chat.id, "fl_action", action_map.get(action_ru, "mute"))
    await message.answer(f"🌊 Антифлуд: {limit} сообщ/5сек → {action_ru}")

@router.message(Command("антирейд"))
@admin_only
async def cmd_antiraid(message: Message):
    args = message.text.split()
    val = 1 if len(args) > 1 and args[1] == "вкл" else 0
    set_chat(message.chat.id, "antiraid", val)
    await message.answer(f"🛡 Антирейд: {'✅ включён' if val else '❌ выключен'}")

@router.message(Command("запретитьслово"))
@admin_only
async def cmd_addword(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /запретитьслово [слово]")
    db_exec("INSERT INTO bad_words (chat_id,word) VALUES (?,?)", (message.chat.id, args[1].lower().strip()))
    await message.answer(f"✅ «{args[1]}» запрещено.")

@router.message(Command("разрешитьслово"))
@admin_only
async def cmd_delword(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /разрешитьслово [слово]")
    db_exec("DELETE FROM bad_words WHERE chat_id=? AND word=?", (message.chat.id, args[1].lower().strip()))
    await message.answer(f"✅ «{args[1]}» разрешено.")

@router.message(Command(commands=["запрещённые", "запрещенные"]))
@group_only
async def cmd_badwords(message: Message):
    rows = db_all("SELECT word FROM bad_words WHERE chat_id=?", (message.chat.id,))
    if not rows:
        return await message.answer("📋 Запрещённых слов нет.")
    await message.answer("🚫 <b>Запрещённые слова:</b>\n" + "\n".join(f"• {r['word']}" for r in rows))

# ── Ranks in chat ──────────────────────────────
@router.message(Command(commands=["назначить", "addmod"]))
@admin_only
@need_reply
async def cmd_addmod(message: Message):
    t = message.reply_to_message.from_user
    args = message.text.split(maxsplit=1)
    rank = args[1] if len(args) > 1 else "Модератор"
    db_exec("INSERT OR REPLACE INTO moderators (chat_id,user_id,rank) VALUES (?,?,?)",
            (message.chat.id, t.id, rank))
    await message.answer(f"✅ {mn(t.id, t.full_name)} — {rank}")

@router.message(Command(commands=["снятьмодера", "removemod"]))
@admin_only
@need_reply
async def cmd_removemod(message: Message):
    t = message.reply_to_message.from_user
    db_exec("DELETE FROM moderators WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    await message.answer(f"✅ {mn(t.id, t.full_name)} снят.")

@router.message(Command("ранг"))
@admin_only
@need_reply
async def cmd_rank(message: Message):
    t = message.reply_to_message.from_user
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /ранг [звание]")
    db_exec("UPDATE moderators SET rank=? WHERE chat_id=? AND user_id=?", (args[1], message.chat.id, t.id))
    await message.answer(f"🎖 Ранг {mn(t.id, t.full_name)}: «{args[1]}»")

# ── Welcome/farewell ──────────────────────────
@router.message(Command(commands=["приветствие", "welcome"]))
@admin_only
async def cmd_welcome(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /приветствие [текст]\nПеременные: {user} {chat}")
    set_chat(message.chat.id, "welcome", args[1])
    await message.answer("✅ Приветствие установлено.")

@router.message(Command(commands=["прощание", "farewell"]))
@admin_only
async def cmd_farewell(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /прощание [текст]")
    set_chat(message.chat.id, "farewell", args[1])
    await message.answer("✅ Прощание установлено.")

# ── Triggers ──────────────────────────────────
@router.message(Command(commands=["триггер", "trigger"]))
@admin_only
async def cmd_addtrigger(message: Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        return await message.answer("⚠️ /триггер [слово] [ответ]")
    db_exec("INSERT INTO triggers (chat_id,keyword,response) VALUES (?,?,?)",
            (message.chat.id, args[1].lower(), args[2]))
    await message.answer(f"✅ Триггер «{args[1]}» добавлен.")

@router.message(Command("удалитьтриггер"))
@admin_only
async def cmd_deltrigger(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /удалитьтриггер [слово]")
    db_exec("DELETE FROM triggers WHERE chat_id=? AND keyword=?", (message.chat.id, args[1].lower()))
    await message.answer(f"✅ Триггер «{args[1]}» удалён.")

# ── Economy commands ───────────────────────────
@router.message(Command(commands=["перевести", "transfer"]))
async def cmd_transfer(message: Message):
    if not message.reply_to_message:
        return await message.answer("⚠️ Ответь на сообщение получателя.")
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("⚠️ /перевести [сумма]")
    amount = int(args[1])
    if amount <= 0:
        return await message.answer("❌ Сумма должна быть больше 0.")
    sender = message.from_user.id
    receiver = message.reply_to_message.from_user.id
    if sender == receiver:
        return await message.answer("❌ Нельзя переводить себе.")
    ensure_eco(sender); ensure_eco(receiver)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (sender,))["balance"]
    if bal < amount:
        return await message.answer(f"❌ Недостаточно средств ({bal} 💎).")
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (amount, sender))
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (amount, receiver))
    t = message.reply_to_message.from_user
    await message.answer(f"✅ {mn(sender, message.from_user.full_name)} → {mn(t.id, t.full_name)}: <b>{amount} 💎</b>")

@router.message(Command(commands=["купить", "buy"]))
async def cmd_buy(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("⚠️ /купить [id товара]")
    item = db_one("SELECT id,name,price FROM shop WHERE id=?", (int(args[1]),))
    if not item:
        return await message.answer("❌ Товар не найден.")
    uid = message.from_user.id
    ensure_eco(uid)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < item["price"]:
        return await message.answer(f"❌ Нужно {item['price']} 💎, у тебя {bal} 💎.")
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (item["price"], uid))
    db_exec("INSERT INTO inventory (user_id,item) VALUES (?,?)", (uid, item["name"]))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"✅ Куплено: {item['name']}\n💰 Баланс: <b>{new_bal} 💎</b>")

# ── Factory commands ───────────────────────────
@router.message(Command(commands=["купитьзавод", "buyfactory"]))
async def cmd_buy_factory(message: Message):
    uid = message.from_user.id
    if db_one("SELECT 1 FROM factories WHERE user_id=?", (uid,)):
        return await message.answer("❌ У тебя уже есть завод! /завод")
    ensure_eco(uid)
    cost = 500
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await message.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.")
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    db_exec("INSERT INTO factories (user_id) VALUES (?)", (uid,))
    await message.answer("🏭 Завод куплен! Используй /завод для управления.", reply_markup=kb_factory(uid))

@router.message(Command(commands=["прокачать", "upgradefactory"]))
async def cmd_upgrade_factory(message: Message):
    uid = message.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await message.answer("❌ У тебя нет завода. /купитьзавод")
    cost = factory_upgrade_cost(f["level"])
    ensure_eco(uid)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await message.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.")
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    db_exec("UPDATE factories SET level=level+1, upgrades=upgrades+1 WHERE user_id=?", (uid,))
    f2 = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    await message.answer(f"⬆️ Завод прокачан до уровня <b>{f2['level']}</b>!\n"
                         f"💰 Новый доход: <b>{factory_income(f2['level'], f2['workers'], f2['upgrades'])} 💎/ч</b>",
                         reply_markup=kb_factory(uid))

@router.message(Command(commands=["рабочие", "addworker"]))
async def cmd_add_worker(message: Message):
    uid = message.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await message.answer("❌ У тебя нет завода. /купитьзавод")
    cost = factory_worker_cost(f["workers"])
    ensure_eco(uid)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await message.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.")
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    db_exec("UPDATE factories SET workers=workers+1 WHERE user_id=?", (uid,))
    f2 = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    await message.answer(f"👷 Нанят рабочий! Всего: <b>{f2['workers']}</b>\n"
                         f"💰 Новый доход: <b>{factory_income(f2['level'], f2['workers'], f2['upgrades'])} 💎/ч</b>")

@router.message(Command(commands=["собрать", "collect"]))
async def cmd_collect(message: Message):
    uid = message.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await message.answer("❌ У тебя нет завода. /купитьзавод")
    last = f["last_collect"] or ""
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            diff = datetime.now() - last_dt
            if diff < timedelta(hours=1):
                rem = timedelta(hours=1) - diff
                m = int(rem.total_seconds()) // 60
                s = int(rem.total_seconds()) % 60
                return await message.answer(f"⏳ Доход будет через <b>{m}мин {s}сек</b>.")
        except:
            pass
    income = factory_income(f["level"], f["workers"], f["upgrades"])
    ensure_eco(uid)
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (income, uid))
    db_exec("UPDATE factories SET last_collect=? WHERE user_id=?", (datetime.now().isoformat(), uid))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"💰 Собрано <b>{income} 💎</b> с завода!\n💼 Баланс: <b>{new_bal} 💎</b>")

@router.message(Command(commands=["продатьзавод", "sellfactory"]))
async def cmd_sell_factory(message: Message):
    uid = message.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await message.answer("❌ У тебя нет завода.")
    sell_price = int((f["level"] * 400 + f["workers"] * 80 + f["upgrades"] * 200) * 0.7)
    ensure_eco(uid)
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (sell_price, uid))
    db_exec("DELETE FROM factories WHERE user_id=?", (uid,))
    await message.answer(f"📈 Завод продан за <b>{sell_price} 💎</b>!")

# ── Relationships ──────────────────────────────
@router.message(Command(commands=["жениться", "marry"]))
@need_reply
async def cmd_marry(message: Message):
    u1, u2 = message.from_user.id, message.reply_to_message.from_user.id
    if u1 == u2:
        return await message.answer("❌ Нельзя жениться на себе.")
    if db_one("SELECT 1 FROM relationships WHERE (user1=? AND user2=?) OR (user1=? AND user2=?)", (u1,u2,u2,u1)):
        return await message.answer("❌ Вы уже вместе.")
    if db_one("SELECT 1 FROM relationships WHERE user1=? OR user2=? OR user1=? OR user2=?", (u1,u1,u2,u2)):
        return await message.answer("❌ Один из вас уже в отношениях.")
    db_exec("INSERT INTO relationships (user1,user2) VALUES (?,?)", (min(u1,u2), max(u1,u2)))
    t = message.reply_to_message.from_user
    await message.answer(f"💑 {mn(u1, message.from_user.full_name)} и {mn(u2, t.full_name)} теперь вместе! 💕")

@router.message(Command(commands=["развестись", "divorce"]))
async def cmd_divorce(message: Message):
    uid = message.from_user.id
    if not db_one("SELECT 1 FROM relationships WHERE user1=? OR user2=?", (uid, uid)):
        return await message.answer("❌ Ты не в отношениях.")
    db_exec("DELETE FROM relationships WHERE user1=? OR user2=?", (uid, uid))
    await message.answer(f"💔 {mn(uid, message.from_user.full_name)} вышел(а) из отношений.")

@router.message(Command(commands=["усыновить", "adopt"]))
@need_reply
async def cmd_adopt(message: Message):
    parent, child = message.from_user.id, message.reply_to_message.from_user.id
    if parent == child:
        return await message.answer("❌ Нельзя усыновить себя.")
    db_exec("INSERT OR IGNORE INTO family (parent,child) VALUES (?,?)", (parent, child))
    t = message.reply_to_message.from_user
    await message.answer(f"👶 {mn(parent, message.from_user.full_name)} усыновил(а) {mn(child, t.full_name)}!")

# ══════════════════════════════════════════════
#  RP COMMANDS
# ══════════════════════════════════════════════
RP_ACTIONS = {
    "обнять":     ("🤗", "обнял(а)"),
    "поцеловать": ("😘", "поцеловал(а)"),
    "ударить":    ("👊", "ударил(а)"),
    "погладить":  ("🥰", "погладил(а)"),
    "укусить":    ("😬", "укусил(а)"),
    "подмигнуть": ("😉", "подмигнул(а)"),
    "пнуть":      ("🦵", "пнул(а)"),
    "обидеть":    ("😢", "обидел(а)"),
    "приобнять":  ("🫂", "крепко обнял(а)"),
    "потрепать":  ("😄", "потрепал(а) по голове"),
    "зарыть":     ("⚰️", "закопал(а)"),
    "швырнуть":   ("🌪", "швырнул(а)"),
    "лизнуть":    ("👅", "лизнул(а)"),
    "укачать":    ("🌙", "укачал(а)"),
    "потискать":  ("🐻", "потискал(а)"),
    "шлёпнуть":   ("👋", "шлёпнул(а)"),
    "укутать":    ("🧣", "укутал(а)"),
    "ущипнуть":   ("🤏", "ущипнул(а)"),
    "похлопать":  ("👏", "похлопал(а) по плечу"),
    "потыкать":   ("👆", "потыкал(а)"),
    "поднять":    ("💪", "поднял(а) на руки"),
    "потанцевать":("💃", "потанцевал(а) с"),
    "укрыть":     ("🛌", "укрыл(а) одеялом"),
    "накормить":  ("🍰", "накормил(а)"),
}

def make_rp(emoji, action):
    async def handler(message: Message):
        if not message.reply_to_message:
            return await message.answer("⚠️ Ответь на сообщение участника.")
        s, t = message.from_user, message.reply_to_message.from_user
        await message.answer(f"{emoji} {mn(s.id, s.full_name)} {action} {mn(t.id, t.full_name)}")
    return handler

for _cmd, (_em, _act) in RP_ACTIONS.items():
    router.message(Command(_cmd))(make_rp(_em, _act))

# ══════════════════════════════════════════════
#  BOT STAFF MANAGEMENT
# ══════════════════════════════════════════════
@router.message(Command(commands=["назначитьдолжность", "setrank"]))
async def cmd_set_bot_rank(message: Message):
    uid = message.from_user.id
    my_rank = get_bot_rank(uid)
    if my_rank > 6:
        return await message.answer("❌ Недостаточно прав.")
    if not message.reply_to_message:
        return await message.answer(
            "⚠️ Ответь на сообщение и укажи номер должности:\n\n" +
            "\n".join(f"{k}. {v}" for k, v in RANKS.items()) +
            "\n\nПример: /назначитьдолжность 7"
        )
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("⚠️ Укажи номер должности (1-9).")
    target_rank = int(args[1])
    if target_rank < 1 or target_rank > 9:
        return await message.answer("❌ Должность должна быть от 1 до 9.")
    if not can_appoint(my_rank, target_rank):
        return await message.answer("❌ Нельзя назначить должность выше или равную своей.")
    t = message.reply_to_message.from_user
    if t.id == OWNER_ID:
        return await message.answer("❌ Нельзя изменить должность владельца.")
    rank_name = RANKS[target_rank]
    db_exec("INSERT OR REPLACE INTO bot_staff (user_id,rank_id,rank_name,name) VALUES (?,?,?,?)",
            (t.id, target_rank, rank_name, t.full_name))
    await message.answer(f"✅ {mn(t.id, t.full_name)} назначен(а) на должность <b>{rank_name}</b>")

@router.message(Command(commands=["снятьдолжность", "removerank"]))
async def cmd_remove_bot_rank(message: Message):
    uid = message.from_user.id
    my_rank = get_bot_rank(uid)
    if my_rank > 6:
        return await message.answer("❌ Недостаточно прав.")
    if not message.reply_to_message:
        return await message.answer("⚠️ Ответь на сообщение участника.")
    t = message.reply_to_message.from_user
    if t.id == OWNER_ID:
        return await message.answer("❌ Нельзя снять владельца.")
    their_rank = get_bot_rank(t.id)
    if not can_appoint(my_rank, their_rank):
        return await message.answer("❌ Нельзя снять человека с должностью выше или равной твоей.")
    db_exec("DELETE FROM bot_staff WHERE user_id=?", (t.id,))
    await message.answer(f"✅ Должность {mn(t.id, t.full_name)} снята.")

# ══════════════════════════════════════════════
#  OWNER COMMANDS
# ══════════════════════════════════════════════
@router.message(Command(commands=["владелецпомощь", "ownerhelp"]))
@owner_only
async def cmd_ownerhelp(message: Message):
    await message.answer(
        "👑 <b>Команды владельца бота</b>\n\n"
        "<b>👥 Стафф бота</b>\n"
        "/назначитьдолжность [1-9] — назначить должность (ответ на сообщение)\n"
        "/снятьдолжность — снять должность\n"
        "/должности — список стаффа\n\n"
        "<b>📋 Чаты</b>\n"
        "/мойчаты — приватный список чатов\n"
        "/чаты — публичный список чатов\n"
        "/статбота — статистика бота\n"
        "/рассылка [текст] — рассылка во все чаты\n"
        "/лог [chat_id] [ch_id] — настроить лог\n\n"
        "<b>💰 Экономика</b>\n"
        "/выдатьбаланс [user_id] [сумма]\n"
        "/забратьбаланс [user_id] [сумма]\n"
        "/добавитьтовар [цена] [название] [описание]\n"
        "/удалитьтовар [id]\n\n"
        "<b>🚫 Глобальная модерация</b>\n"
        "/глобальныйбан (ответ)\n"
        "/глобальныйразбан (ответ)\n\n"
        "<b>⚙️ Прочее</b>\n"
        "/сбросбд ПОДТВЕРЖДАЮ"
    )

@router.message(Command(commands=["мойчаты", "mychats"]))
@owner_only
async def cmd_mychats(message: Message):
    rows = db_all("SELECT * FROM chats ORDER BY member_count DESC", ())
    if not rows:
        return await message.answer("📋 Нет чатов.")
    lines = []
    for r in rows:
        link = f"@{r['username']}" if r.get("username") else f"<code>{r['chat_id']}</code>"
        lines.append(f"• {r['title'] or '—'} | {link} | 👥{r['member_count']}")
    await message.answer("📋 <b>Все чаты бота:</b>\n" + "\n".join(lines))

@router.message(Command(commands=["статбота", "botstats"]))
@owner_only
async def cmd_botstats(message: Message):
    chats = db_one("SELECT COUNT(*) as c FROM chats", ())["c"]
    users = db_one("SELECT COUNT(DISTINCT user_id) as c FROM stats", ())["c"]
    warns = db_one("SELECT COUNT(*) as c FROM warns", ())["c"]
    eco   = db_one("SELECT COUNT(*) as c FROM economy", ())["c"]
    staff = db_one("SELECT COUNT(*) as c FROM bot_staff", ())["c"]
    fabs  = db_one("SELECT COUNT(*) as c FROM factories", ())["c"]
    await message.answer(
        f"📊 <b>Статистика Replify</b>\n\n"
        f"💬 Чатов: <b>{chats}</b>\n"
        f"👥 Юзеров: <b>{users}</b>\n"
        f"⚠️ Варнов: <b>{warns}</b>\n"
        f"💰 В экономике: <b>{eco}</b>\n"
        f"🏭 Заводов: <b>{fabs}</b>\n"
        f"🛡 Стафф бота: <b>{staff}</b>"
    )

@router.message(Command(commands=["рассылка", "broadcast"]))
@owner_only
async def cmd_broadcast(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /рассылка [текст]")
    rows = db_all("SELECT chat_id FROM chats", ())
    ok = fail = 0
    for r in rows:
        try:
            await bot.send_message(r["chat_id"], f"📢 <b>Объявление:</b>\n\n{args[1]}")
            ok += 1
        except:
            fail += 1
    await message.answer(f"✅ Отправлено: {ok}\n❌ Ошибок: {fail}")

@router.message(Command(commands=["лог", "setlog"]))
@owner_only
async def cmd_setlog(message: Message):
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("⚠️ /лог [chat_id] [channel_id]")
    try:
        cid, ch = int(args[1]), int(args[2])
        get_chat(cid)
        set_chat(cid, "log_channel", ch)
        await message.answer(f"✅ Лог <code>{cid}</code> → <code>{ch}</code>")
    except:
        await message.answer("❌ Неверные ID.")

@router.message(Command(commands=["выдатьбаланс", "givemoney"]))
@owner_only
async def cmd_givemoney(message: Message):
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("⚠️ /выдатьбаланс [user_id] [сумма]")
    try:
        uid, amount = int(args[1]), int(args[2])
    except:
        return await message.answer("❌ Неверные параметры.")
    ensure_eco(uid)
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (amount, uid))
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"✅ Выдано <b>{amount} 💎</b> → <code>{uid}</code>\nБаланс: <b>{bal} 💎</b>")

@router.message(Command(commands=["забратьбаланс", "takemoney"]))
@owner_only
async def cmd_takemoney(message: Message):
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("⚠️ /забратьбаланс [user_id] [сумма]")
    try:
        uid, amount = int(args[1]), int(args[2])
    except:
        return await message.answer("❌ Неверные параметры.")
    ensure_eco(uid)
    db_exec("UPDATE economy SET balance=MAX(0,balance-?) WHERE user_id=?", (amount, uid))
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"✅ Снято <b>{amount} 💎</b> у <code>{uid}</code>\nОстаток: <b>{bal} 💎</b>")

@router.message(Command(commands=["добавитьтовар", "additem"]))
@owner_only
async def cmd_additem(message: Message):
    args = message.text.split(maxsplit=3)
    if len(args) < 4:
        return await message.answer("⚠️ /добавитьтовар [цена] [название] [описание]")
    try:
        price = int(args[1])
    except:
        return await message.answer("❌ Цена должна быть числом.")
    db_exec("INSERT INTO shop (name,price,description) VALUES (?,?,?)", (args[2], price, args[3]))
    await message.answer(f"✅ «{args[2]}» за {price} 💎 добавлен.")

@router.message(Command(commands=["удалитьтовар", "delitem"]))
@owner_only
async def cmd_delitem(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("⚠️ /удалитьтовар [id]")
    db_exec("DELETE FROM shop WHERE id=?", (int(args[1]),))
    await message.answer(f"✅ Товар #{args[1]} удалён.")

@router.message(Command(commands=["глобальныйбан", "gban"]))
@owner_only
@need_reply
async def cmd_gban(message: Message):
    t = message.reply_to_message.from_user
    rows = db_all("SELECT chat_id FROM chats", ())
    ok = fail = 0
    for r in rows:
        try:
            await bot.ban_chat_member(r["chat_id"], t.id)
            ok += 1
        except:
            fail += 1
    await message.answer(f"🚫 Глобальный бан {mn(t.id, t.full_name)}\n✅ {ok} чатов | ❌ {fail} ошибок")

@router.message(Command(commands=["глобальныйразбан", "gunban"]))
@owner_only
@need_reply
async def cmd_gunban(message: Message):
    t = message.reply_to_message.from_user
    rows = db_all("SELECT chat_id FROM chats", ())
    ok = fail = 0
    for r in rows:
        try:
            await bot.unban_chat_member(r["chat_id"], t.id)
            ok += 1
        except:
            fail += 1
    await message.answer(f"✅ Глобальный разбан {mn(t.id, t.full_name)}\n{ok} чатов")

@router.message(Command(commands=["сбросбд", "resetdb"]))
@owner_only
async def cmd_resetdb(message: Message):
    args = message.text.split()
    if len(args) < 2 or args[1] != "ПОДТВЕРЖДАЮ":
        return await message.answer("⚠️ Это сбросит всю БД!\nНапиши: /сбросбд ПОДТВЕРЖДАЮ")
    cur.executescript("""
        DELETE FROM warns; DELETE FROM stats; DELETE FROM economy;
        DELETE FROM inventory; DELETE FROM triggers; DELETE FROM bad_words;
        DELETE FROM relationships; DELETE FROM family; DELETE FROM moderators;
        DELETE FROM bot_staff; DELETE FROM factories;
    """)
    conn.commit()
    await message.answer("✅ База данных очищена.")

# ══════════════════════════════════════════════
#  CALLBACK BUTTONS
# ══════════════════════════════════════════════
@router.callback_query(F.data == "main_menu")
async def cb_main(call: CallbackQuery):
    uid = call.from_user.id
    rank_name = get_bot_rank_name(uid)
    rank_line = f"\n🎖 Должность: <b>{rank_name}</b>" if rank_name else ""
    await call.message.edit_text(
        f"👋 <b>Replify | Чат-менеджер</b>{rank_line}\n\nВыбери раздел:",
        reply_markup=kb_main()
    )

@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    uid = call.from_user.id
    ensure_eco(uid)
    eco = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))
    rank = get_bot_rank_name(uid)
    f = db_one("SELECT level FROM factories WHERE user_id=?", (uid,))
    text = (f"👤 <b>Профиль</b> {mn(uid, call.from_user.full_name)}\n\n"
            f"🆔 ID: <code>{uid}</code>\n")
    if rank:
        text += f"🎖 Должность: <b>{rank}</b>\n"
    text += f"💰 Баланс: <b>{eco['balance'] if eco else 0} 💎</b>\n"
    if f:
        text += f"🏭 Завод: ур. <b>{f['level']}</b>\n"
    if call.from_user.username:
        text += f"🔗 @{call.from_user.username}"
    await call.message.edit_text(text, reply_markup=kb_back())

@router.callback_query(F.data == "balance")
async def cb_balance(call: CallbackQuery):
    uid = call.from_user.id
    ensure_eco(uid)
    row = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))
    await call.message.edit_text(f"💰 Твой баланс: <b>{row['balance']} 💎</b>", reply_markup=kb_back())

@router.callback_query(F.data == "bonus")
async def cb_bonus(call: CallbackQuery):
    uid = call.from_user.id
    ensure_eco(uid)
    row = db_one("SELECT balance,last_bonus FROM economy WHERE user_id=?", (uid,))
    last_bonus = row["last_bonus"] or ""
    if last_bonus:
        try:
            last_dt = datetime.fromisoformat(last_bonus)
            diff = datetime.now() - last_dt
            if diff < timedelta(hours=24):
                rem = timedelta(hours=24) - diff
                h = int(rem.total_seconds()) // 3600
                m = (int(rem.total_seconds()) % 3600) // 60
                return await call.answer(f"⏳ Следующий бонус через {h}ч {m}мин", show_alert=True)
        except:
            pass
    amount = random.randint(50, 200)
    db_exec("UPDATE economy SET balance=balance+?, last_bonus=? WHERE user_id=?",
            (amount, datetime.now().isoformat(), uid))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await call.answer(f"🎁 Получено {amount} 💎! Баланс: {new_bal} 💎", show_alert=True)

@router.callback_query(F.data == "factory")
async def cb_factory(call: CallbackQuery):
    uid = call.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await call.message.edit_text(
            "🏭 <b>Завод</b>\n\nУ тебя нет завода!\nКупи за <b>500 💎</b>.",
            reply_markup=kb_factory(uid))
    income = factory_income(f["level"], f["workers"], f["upgrades"])
    sell_price = int((f["level"] * 400 + f["workers"] * 80 + f["upgrades"] * 200) * 0.7)
    last = f["last_collect"] or ""
    collect_info = "✅ Готово к сбору!"
    if last:
        try:
            diff = datetime.now() - datetime.fromisoformat(last)
            if diff < timedelta(hours=1):
                rem = timedelta(hours=1) - diff
                m = int(rem.total_seconds()) // 60
                collect_info = f"⏳ Через {m} мин"
        except:
            pass
    await call.message.edit_text(
        f"🏭 <b>Твой завод</b>\n\n"
        f"📊 Уровень: <b>{f['level']}</b>\n"
        f"👷 Рабочих: <b>{f['workers']}</b>\n"
        f"💰 Доход: <b>{income} 💎/ч</b>\n"
        f"⏰ Сбор: {collect_info}\n"
        f"📈 Продажа: ~{sell_price} 💎",
        reply_markup=kb_factory(uid)
    )

@router.callback_query(F.data == "factory_buy")
async def cb_factory_buy(call: CallbackQuery):
    uid = call.from_user.id
    if db_one("SELECT 1 FROM factories WHERE user_id=?", (uid,)):
        return await call.answer("❌ У тебя уже есть завод!", show_alert=True)
    ensure_eco(uid)
    cost = 500
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await call.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.", show_alert=True)
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    db_exec("INSERT INTO factories (user_id) VALUES (?)", (uid,))
    await call.answer("🏭 Завод куплен!", show_alert=True)
    await cb_factory(call)

@router.callback_query(F.data == "factory_upgrade")
async def cb_factory_upgrade(call: CallbackQuery):
    uid = call.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await call.answer("❌ Нет завода.", show_alert=True)
    cost = factory_upgrade_cost(f["level"])
    ensure_eco(uid)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await call.answer(f"❌ Нужно {cost} 💎, у тебя {bal}.", show_alert=True)
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    db_exec("UPDATE factories SET level=level+1, upgrades=upgrades+1 WHERE user_id=?", (uid,))
    await call.answer("⬆️ Завод прокачан!", show_alert=True)
    await cb_factory(call)

@router.callback_query(F.data == "factory_worker")
async def cb_factory_worker(call: CallbackQuery):
    uid = call.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await call.answer("❌ Нет завода.", show_alert=True)
    cost = factory_worker_cost(f["workers"])
    ensure_eco(uid)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await call.answer(f"❌ Нужно {cost} 💎, у тебя {bal}.", show_alert=True)
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    db_exec("UPDATE factories SET workers=workers+1 WHERE user_id=?", (uid,))
    await call.answer("👷 Рабочий нанят!", show_alert=True)
    await cb_factory(call)

@router.callback_query(F.data == "factory_collect")
async def cb_factory_collect(call: CallbackQuery):
    uid = call.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await call.answer("❌ Нет завода.", show_alert=True)
    last = f["last_collect"] or ""
    if last:
        try:
            diff = datetime.now() - datetime.fromisoformat(last)
            if diff < timedelta(hours=1):
                rem = timedelta(hours=1) - diff
                m = int(rem.total_seconds()) // 60
                return await call.answer(f"⏳ Ещё {m} мин.", show_alert=True)
        except:
            pass
    income = factory_income(f["level"], f["workers"], f["upgrades"])
    ensure_eco(uid)
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (income, uid))
    db_exec("UPDATE factories SET last_collect=? WHERE user_id=?", (datetime.now().isoformat(), uid))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await call.answer(f"💰 Собрано {income} 💎! Баланс: {new_bal} 💎", show_alert=True)
    await cb_factory(call)

@router.callback_query(F.data == "factory_sell")
async def cb_factory_sell(call: CallbackQuery):
    uid = call.from_user.id
    f = db_one("SELECT * FROM factories WHERE user_id=?", (uid,))
    if not f:
        return await call.answer("❌ Нет завода.", show_alert=True)
    sell_price = int((f["level"] * 400 + f["workers"] * 80 + f["upgrades"] * 200) * 0.7)
    ensure_eco(uid)
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (sell_price, uid))
    db_exec("DELETE FROM factories WHERE user_id=?", (uid,))
    await call.answer(f"📈 Завод продан за {sell_price} 💎!", show_alert=True)
    await call.message.edit_text(
        "🏭 <b>Завод</b>\n\nУ тебя нет завода!\nКупи за <b>500 💎</b>.",
        reply_markup=kb_factory(uid)
    )

@router.callback_query(F.data == "shop")
async def cb_shop(call: CallbackQuery):
    rows = db_all("SELECT id,name,price,description FROM shop", ())
    if not rows:
        return await call.message.edit_text("🛒 Магазин пуст.", reply_markup=kb_back())
    lines = [f"<b>{r['id']}.</b> {r['name']} — <b>{r['price']} 💎</b>\n<i>{r['description']}</i>" for r in rows]
    await call.message.edit_text("🛒 <b>Магазин:</b>\n\n" + "\n\n".join(lines), reply_markup=kb_shop())

@router.callback_query(F.data.startswith("buy_"))
async def cb_buy(call: CallbackQuery):
    item_id = int(call.data.split("_")[1])
    item = db_one("SELECT id,name,price FROM shop WHERE id=?", (item_id,))
    if not item:
        return await call.answer("❌ Товар не найден.", show_alert=True)
    uid = call.from_user.id
    ensure_eco(uid)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < item["price"]:
        return await call.answer(f"❌ Нужно {item['price']} 💎, у тебя {bal} 💎.", show_alert=True)
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (item["price"], uid))
    db_exec("INSERT INTO inventory (user_id,item) VALUES (?,?)", (uid, item["name"]))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await call.answer(f"✅ Куплено: {item['name']}! Баланс: {new_bal} 💎", show_alert=True)

@router.callback_query(F.data == "case")
async def cb_case(call: CallbackQuery):
    uid = call.from_user.id
    ensure_eco(uid)
    cost = 50
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await call.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.", show_alert=True)
    prizes = [
        ("💎 Кристалл", 5), ("🍀 Амулет", 10), ("🎭 VIP-статус", 2),
        ("💰 200 монет", 15), ("💰 100 монет", 28), ("💰 50 монет", 40),
    ]
    total = sum(w for _, w in prizes)
    roll = random.uniform(0, total)
    cum = 0; prize = prizes[-1][0]
    for name, weight in prizes:
        cum += weight
        if roll <= cum:
            prize = name; break
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    if "монет" in prize:
        coins = int(prize.split()[1])
        db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (coins, uid))
    else:
        db_exec("INSERT INTO inventory (user_id,item) VALUES (?,?)", (uid, prize))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await call.answer(f"🎲 Выпало: {prize}! Баланс: {new_bal} 💎", show_alert=True)

@router.callback_query(F.data == "top")
async def cb_top(call: CallbackQuery):
    rows = db_all("""
        SELECT user_id, name, SUM(msgs) as total
        FROM stats GROUP BY user_id ORDER BY total DESC LIMIT 10
    """)
    if not rows:
        return await call.message.edit_text("📊 Статистики пока нет.", reply_markup=kb_back())
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i<3 else str(i+1)+'.'} {mn(r['user_id'], r['name'])} — <b>{r['total']}</b>"
             for i, r in enumerate(rows)]
    await call.message.edit_text("📊 <b>Топ актива (все чаты):</b>\n\n" + "\n".join(lines), reply_markup=kb_back())

@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await call.message.edit_text(
        "📋 <b>Команды</b>\n\n"
        "Можно писать с / . ! - + или без префикса\n\n"
        "старт • помощь • профиль • баланс\n"
        "бонус • топ • стата • чат • магазин\n"
        "инвентарь • кейс • завод • топзавод\n"
        "жениться • семья • должности • чаты",
        reply_markup=kb_back()
    )

# ══════════════════════════════════════════════
#  UPDATE MEMBER COUNT
# ══════════════════════════════════════════════
async def update_member_counts():
    """Обновляет кол-во участников каждые 10 минут"""
    while True:
        await asyncio.sleep(600)
        rows = db_all("SELECT chat_id FROM chats", ())
        for r in rows:
            try:
                count = await bot.get_chat_member_count(r["chat_id"])
                db_exec("UPDATE chats SET member_count=? WHERE chat_id=?", (count, r["chat_id"]))
            except:
                pass

# ══════════════════════════════════════════════
#  LAUNCH
# ══════════════════════════════════════════════
async def main():
    dp.include_router(router)
    log.info("🚀 Replify запущен")
    asyncio.create_task(update_member_counts())
    await dp.start_polling(
        bot,
        allowed_updates=["message", "chat_member", "my_chat_member", "callback_query"]
    )

if __name__ == "__main__":
    asyncio.run(main())
