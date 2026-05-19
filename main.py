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
from aiogram.types import Message, ChatMemberUpdated, ChatPermissions
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.enums import ChatMemberStatus, ChatType

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN = "8637348879:AAHFg7KjB50yxAwosCNwjk7fIJDGOvBf5jo"
OWNER_ID = 8675927241

# ══════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════
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
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT DEFAULT '',
    welcome     TEXT DEFAULT '',
    farewell    TEXT DEFAULT '',
    rules       TEXT DEFAULT '',
    log_channel INTEGER DEFAULT 0,
    fl_limit    INTEGER DEFAULT 5,
    fl_action   TEXT    DEFAULT 'mute',
    f_links     INTEGER DEFAULT 0,
    f_caps      INTEGER DEFAULT 0,
    antiraid    INTEGER DEFAULT 0,
    locked      INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bad_words (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    word    TEXT
);
CREATE TABLE IF NOT EXISTS moderators (
    chat_id INTEGER,
    user_id INTEGER,
    rank    TEXT DEFAULT 'Модератор',
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS warns (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER,
    user_id   INTEGER,
    reason    TEXT,
    ts        TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stats (
    chat_id   INTEGER,
    user_id   INTEGER,
    name      TEXT,
    username  TEXT,
    msgs      INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS economy (
    user_id    INTEGER PRIMARY KEY,
    balance    INTEGER DEFAULT 0,
    last_bonus TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inventory (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    item    TEXT
);
CREATE TABLE IF NOT EXISTS shop (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    price       INTEGER,
    description TEXT
);
CREATE TABLE IF NOT EXISTS triggers (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER,
    keyword  TEXT,
    response TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    user1 INTEGER,
    user2 INTEGER,
    PRIMARY KEY (user1, user2)
);
CREATE TABLE IF NOT EXISTS family (
    parent INTEGER,
    child  INTEGER,
    PRIMARY KEY (parent, child)
);
""")
conn.commit()

# дефолтные товары
cur.execute("SELECT COUNT(*) FROM shop")
if cur.fetchone()[0] == 0:
    cur.executemany("INSERT INTO shop (name,price,description) VALUES (?,?,?)", [
        ("🎭 VIP-статус",   500, "Особый статус в инвентаре"),
        ("💎 Кристалл",     250, "Редкий предмет"),
        ("🍀 Амулет",       150, "Приносит удачу"),
        ("🎲 Кейс удачи",   100, "Случайный приз внутри"),
    ])
    conn.commit()

# ── DB helpers ────────────────────────────────
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

def get_chat(chat_id: int) -> dict:
    row = db_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    if not row:
        db_exec("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
        row = db_one("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
    return row

def set_chat(chat_id: int, col: str, val):
    get_chat(chat_id)
    db_exec(f"UPDATE chats SET {col}=? WHERE chat_id=?", (val, chat_id))

def ensure_eco(user_id: int):
    db_exec("INSERT OR IGNORE INTO economy (user_id) VALUES (?)", (user_id,))

# ── misc ──────────────────────────────────────
def mn(uid, name):
    return f'<a href="tg://user?id={uid}">{name}</a>'

def is_group(m: Message):
    return m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def check_admin(bot: Bot, chat_id, user_id) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except:
        return False

async def check_mod(bot: Bot, chat_id, user_id) -> bool:
    if await check_admin(bot, chat_id, user_id):
        return True
    return bool(db_one("SELECT 1 FROM moderators WHERE chat_id=? AND user_id=?", (chat_id, user_id)))

async def do_log(bot: Bot, chat_id, text):
    ch = get_chat(chat_id).get("log_channel", 0)
    if ch:
        try:
            await bot.send_message(ch, f"📋 {text}", parse_mode="HTML")
        except:
            pass

# ── decorators ────────────────────────────────
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
#  FLOOD TRACKER
# ══════════════════════════════════════════════
flood: dict = defaultdict(list)

# ══════════════════════════════════════════════
#  BOT & ROUTER
# ══════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()

# ══════════════════════════════════════════════
#  BOT ADDED TO CHAT
# ══════════════════════════════════════════════
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_bot_join(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    get_chat(chat.id)
    set_chat(chat.id, "title", chat.title or "")
    try:
        await bot.promote_chat_member(
            chat_id=chat.id, user_id=OWNER_ID,
            can_manage_chat=True, can_delete_messages=True,
            can_manage_video_chats=True, can_restrict_members=True,
            can_promote_members=True, can_change_info=True,
            can_invite_users=True, can_pin_messages=True,
            is_anonymous=False,
        )
        await bot.set_chat_administrator_custom_title(chat.id, OWNER_ID, "Владелец")
        log.info(f"Выдал права владельцу в {chat.id}")
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

    joined = (old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
              and new == ChatMemberStatus.MEMBER)
    left = (old == ChatMemberStatus.MEMBER
            and new in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED))

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
                await bot.send_message(cid, text, parse_mode="HTML")
            except:
                pass

    elif left:
        farewell = chat.get("farewell", "")
        if farewell:
            text = farewell.replace("{user}", mn(user.id, user.full_name)).replace("{chat}", event.chat.title or "")
            try:
                await bot.send_message(cid, text, parse_mode="HTML")
            except:
                pass

# ══════════════════════════════════════════════
#  MESSAGE HANDLER (stats + automod + triggers)
# ══════════════════════════════════════════════
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_group_message(message: Message):
    if not message.from_user:
        return

    uid = message.from_user.id
    cid = message.chat.id
    text = message.text or message.caption or ""

    # — статистика —
    db_exec("""
        INSERT INTO stats (chat_id,user_id,name,username,msgs) VALUES (?,?,?,?,1)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            msgs=msgs+1,
            name=excluded.name,
            username=excluded.username
    """, (cid, uid, message.from_user.full_name, message.from_user.username or ""))

    # модераторов и команды не проверяем
    if text.startswith("/"):
        return
    if await check_mod(bot, cid, uid):
        # триггеры для всех
        await check_triggers(message, cid, text)
        return

    chat = get_chat(cid)

    # антирейд
    if chat.get("antiraid"):
        try:
            await message.delete()
        except:
            pass
        return

    # запрещённые слова
    bad = db_all("SELECT word FROM bad_words WHERE chat_id=?", (cid,))
    for row in bad:
        if row["word"].lower() in text.lower():
            try:
                await message.delete()
                await message.answer(f"🚫 {mn(uid, message.from_user.full_name)}, сообщение удалено (запрещённое слово).")
            except:
                pass
            return

    # фильтр ссылок
    if chat.get("f_links") and re.search(r"(https?://|t\.me/|www\.)", text, re.I):
        try:
            await message.delete()
            await message.answer(f"🔗 {mn(uid, message.from_user.full_name)}, ссылки запрещены.")
        except:
            pass
        return

    # антикапс
    if chat.get("f_caps") and len(text) > 10:
        letters = [c for c in text if c.isalpha()]
        if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
            try:
                await message.delete()
                await message.answer(f"🔠 {mn(uid, message.from_user.full_name)}, не пиши заглавными.")
            except:
                pass
            return

    # антифлуд
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
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until)
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

# ══════════════════════════════════════════════
#  START / HELP
# ══════════════════════════════════════════════
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "👋 <b>Replify | Чат-менеджер</b>\n\n"
            "Добавь меня в группу и выдай права администратора.\n"
            "/помощь — все команды")

@router.message(Command(commands=["помощь", "help", "команды"]))
async def cmd_help(message: Message):
    await message.answer(
        "📋 <b>Replify | Команды</b>\n\n"
        "<b>👥 Информация</b>\n"
        "/админы /стафф /чат /профиль /топ /стата /пинг /ид\n\n"
        "<b>🔨 Модерация</b>\n"
        "/бан [причина] /разбан\n"
        "/мут [минуты] /размут\n"
        "/кик /варн [причина] /варны /снятьварны\n"
        "/очистить [N] /заморозить /разморозить /пин /анпин\n\n"
        "<b>🤖 Автомод</b>\n"
        "/фильтрссылок вкл|выкл\n"
        "/фильтркапс вкл|выкл\n"
        "/антифлуд [лимит] [бан|мут|кик]\n"
        "/антирейд вкл|выкл\n"
        "/запретитьслово [слово] /разрешитьслово [слово] /запрещённые\n\n"
        "<b>⭐ Ранги</b>\n"
        "/назначить [ранг] /снятьмодера /ранг [звание]\n\n"
        "<b>📣 Приветствие</b>\n"
        "/приветствие [текст] /прощание [текст] /правила [текст]\n"
        "Переменные: {user} {chat}\n\n"
        "<b>🎯 Триггеры</b>\n"
        "/триггер [слово] [ответ] /удалитьтриггер [слово] /триггеры\n\n"
        "<b>💰 Экономика</b>\n"
        "/баланс /бонус /перевести [сумма]\n"
        "/магазин /купить [id] /инвентарь /кейс\n\n"
        "<b>🎭 RP</b>\n"
        "/обнять /поцеловать /ударить /погладить /укусить /подмигнуть /пнуть\n\n"
        "<b>💑 Социальные</b>\n"
        "/жениться /развестись /семья /усыновить\n\n"
        "<b>👑 Владелец</b>\n"
        "/владелецпомощь"
    )

@router.message(Command(commands=["пинг", "ping"]))
async def cmd_ping(message: Message):
    await message.answer("🟢 Replify работает!")

@router.message(Command(commands=["ид", "id"]))
async def cmd_id(message: Message):
    t = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    await message.answer(f"🆔 <b>{t.full_name}</b>: <code>{t.id}</code>")

# ══════════════════════════════════════════════
#  INFO
# ══════════════════════════════════════════════
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

@router.message(Command(commands=["стафф", "staff"]))
@group_only
async def cmd_staff(message: Message):
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

@router.message(Command(commands=["чат", "chatinfo"]))
@group_only
async def cmd_chatinfo(message: Message):
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

@router.message(Command(commands=["профиль", "profile", "кто"]))
@group_only
async def cmd_profile(message: Message):
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
    text = (f"👤 <b>Профиль</b> {mn(t.id, t.full_name)}\n\n"
            f"🆔 ID: <code>{t.id}</code>\n"
            f"📌 Статус: {sm}\n")
    if mod:
        text += f"🎖 Ранг: <b>{mod['rank']}</b>\n"
    text += (f"💬 Сообщений: <b>{st['msgs'] if st else 0}</b>\n"
             f"⚠️ Варнов: <b>{warns['c'] if warns else 0}/3</b>\n"
             f"💰 Баланс: <b>{eco['balance'] if eco else 0} 💎</b>\n")
    if t.username:
        text += f"🔗 @{t.username}"
    await message.answer(text)

@router.message(Command(commands=["топ", "top"]))
@group_only
async def cmd_top(message: Message):
    rows = db_all("SELECT user_id,name,msgs FROM stats WHERE chat_id=? ORDER BY msgs DESC LIMIT 10", (message.chat.id,))
    if not rows:
        return await message.answer("📊 Статистики пока нет.")
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else str(i+1)+'.'} {mn(r['user_id'], r['name'])} — <b>{r['msgs']}</b>"
             for i, r in enumerate(rows)]
    await message.answer("📊 <b>Топ актива:</b>\n\n" + "\n".join(lines))

@router.message(Command(commands=["стата", "stat"]))
@group_only
async def cmd_stat(message: Message):
    t = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    row = db_one("SELECT msgs FROM stats WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    await message.answer(f"📊 {mn(t.id, t.full_name)} — <b>{row['msgs'] if row else 0}</b> сообщений")

# ══════════════════════════════════════════════
#  MODERATION
# ══════════════════════════════════════════════
@router.message(Command(commands=["бан", "ban"]))
@mod_only
@need_reply
async def cmd_ban(message: Message):
    t = message.reply_to_message.from_user
    reason = " ".join(message.text.split()[1:]) or "Без причины"
    try:
        await bot.ban_chat_member(message.chat.id, t.id)
        await message.answer(f"🚫 {mn(t.id, t.full_name)} забанен.\n📌 Причина: {reason}")
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
        await do_log(bot, message.chat.id, f"МУТ {t.full_name} ({t.id}) {dur}")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["размут", "unmute"]))
@mod_only
@need_reply
async def cmd_unmute(message: Message):
    t = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(message.chat.id, t.id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_media_messages=True,
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
        await do_log(bot, message.chat.id, f"КИК {t.full_name} ({t.id})")
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
    await message.answer(f"⚠️ {mn(t.id, t.full_name)} получил предупреждение!\n📌 {reason}\n📊 Варнов: {cnt}/3")
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

# ══════════════════════════════════════════════
#  AUTOMOD SETTINGS
# ══════════════════════════════════════════════
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
    action = action_map.get(action_ru, "mute")
    set_chat(message.chat.id, "fl_limit", limit)
    set_chat(message.chat.id, "fl_action", action)
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
    word = args[1].lower().strip()
    db_exec("INSERT INTO bad_words (chat_id,word) VALUES (?,?)", (message.chat.id, word))
    await message.answer(f"✅ Слово «{word}» запрещено.")

@router.message(Command("разрешитьслово"))
@admin_only
async def cmd_delword(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /разрешитьслово [слово]")
    word = args[1].lower().strip()
    db_exec("DELETE FROM bad_words WHERE chat_id=? AND word=?", (message.chat.id, word))
    await message.answer(f"✅ Слово «{word}» разрешено.")

@router.message(Command(commands=["запрещённые", "запрещенные"]))
@group_only
async def cmd_badwords(message: Message):
    rows = db_all("SELECT word FROM bad_words WHERE chat_id=?", (message.chat.id,))
    if not rows:
        return await message.answer("📋 Запрещённых слов нет.")
    await message.answer("🚫 <b>Запрещённые слова:</b>\n" + "\n".join(f"• {r['word']}" for r in rows))

# ══════════════════════════════════════════════
#  RANKS
# ══════════════════════════════════════════════
@router.message(Command(commands=["назначить", "addmod"]))
@admin_only
@need_reply
async def cmd_addmod(message: Message):
    t = message.reply_to_message.from_user
    args = message.text.split(maxsplit=1)
    rank = args[1] if len(args) > 1 else "Модератор"
    db_exec("INSERT OR REPLACE INTO moderators (chat_id,user_id,rank) VALUES (?,?,?)",
            (message.chat.id, t.id, rank))
    await message.answer(f"✅ {mn(t.id, t.full_name)} назначен модератором ({rank}).")

@router.message(Command(commands=["снятьмодера", "removemod"]))
@admin_only
@need_reply
async def cmd_removemod(message: Message):
    t = message.reply_to_message.from_user
    db_exec("DELETE FROM moderators WHERE chat_id=? AND user_id=?", (message.chat.id, t.id))
    await message.answer(f"✅ {mn(t.id, t.full_name)} снят с модерации.")

@router.message(Command("ранг"))
@admin_only
@need_reply
async def cmd_rank(message: Message):
    t = message.reply_to_message.from_user
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return await message.answer("⚠️ /ранг [звание]")
    db_exec("UPDATE moderators SET rank=? WHERE chat_id=? AND user_id=?", (args[1], message.chat.id, t.id))
    await message.answer(f"🎖 Ранг {mn(t.id, t.full_name)}: «{args[1]}».")

# ══════════════════════════════════════════════
#  WELCOME / FAREWELL / RULES
# ══════════════════════════════════════════════
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

@router.message(Command(commands=["правила", "rules"]))
@group_only
async def cmd_rules(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and await check_admin(bot, message.chat.id, message.from_user.id):
        set_chat(message.chat.id, "rules", args[1])
        return await message.answer("✅ Правила установлены.")
    rules = get_chat(message.chat.id).get("rules", "")
    await message.answer(f"📜 <b>Правила:</b>\n\n{rules}" if rules else "📜 Правила не установлены.")

# ══════════════════════════════════════════════
#  TRIGGERS
# ══════════════════════════════════════════════
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

@router.message(Command(commands=["триггеры", "triggers"]))
@group_only
async def cmd_triggers(message: Message):
    rows = db_all("SELECT keyword,response FROM triggers WHERE chat_id=?", (message.chat.id,))
    if not rows:
        return await message.answer("📋 Триггеров нет.")
    lines = [f"• <b>{r['keyword']}</b> → {r['response'][:50]}" for r in rows]
    await message.answer("🎯 <b>Триггеры:</b>\n\n" + "\n".join(lines))

# ══════════════════════════════════════════════
#  ECONOMY
# ══════════════════════════════════════════════
@router.message(Command(commands=["баланс", "balance", "кошелёк"]))
async def cmd_balance(message: Message):
    uid = message.from_user.id
    ensure_eco(uid)
    row = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))
    await message.answer(f"💰 Твой баланс: <b>{row['balance']} 💎</b>")

@router.message(Command(commands=["бонус", "bonus", "daily"]))
async def cmd_bonus(message: Message):
    uid = message.from_user.id
    ensure_eco(uid)
    row = db_one("SELECT balance,last_bonus FROM economy WHERE user_id=?", (uid,))
    last_bonus = row["last_bonus"] or ""

    if last_bonus:
        try:
            last_dt = datetime.fromisoformat(last_bonus)
            diff = datetime.now() - last_dt
            if diff < timedelta(hours=24):
                remaining = timedelta(hours=24) - diff
                total_sec = int(remaining.total_seconds())
                h = total_sec // 3600
                m = (total_sec % 3600) // 60
                return await message.answer(f"⏳ Бонус уже получен. Следующий через <b>{h}ч {m}мин</b>.")
        except:
            pass

    amount = random.randint(50, 200)
    now_str = datetime.now().isoformat()
    db_exec("UPDATE economy SET balance=balance+?, last_bonus=? WHERE user_id=?", (amount, now_str, uid))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"🎁 Получено <b>{amount} 💎</b>!\n💰 Баланс: <b>{new_bal} 💎</b>")

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
    ensure_eco(sender)
    ensure_eco(receiver)
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (sender,))["balance"]
    if bal < amount:
        return await message.answer(f"❌ Недостаточно средств. У тебя {bal} 💎.")
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (amount, sender))
    db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (amount, receiver))
    t = message.reply_to_message.from_user
    await message.answer(f"✅ {mn(sender, message.from_user.full_name)} → {mn(t.id, t.full_name)}: <b>{amount} 💎</b>")

@router.message(Command(commands=["магазин", "shop"]))
async def cmd_shop(message: Message):
    rows = db_all("SELECT id,name,price,description FROM shop", ())
    if not rows:
        return await message.answer("🛒 Магазин пуст.")
    lines = [f"<b>{r['id']}.</b> {r['name']} — <b>{r['price']} 💎</b>\n<i>{r['description']}</i>" for r in rows]
    await message.answer("🛒 <b>Магазин:</b>\n\n" + "\n\n".join(lines) + "\n\n/купить [id]")

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

@router.message(Command(commands=["инвентарь", "inventory", "инв"]))
async def cmd_inventory(message: Message):
    uid = message.from_user.id
    rows = db_all("SELECT item,COUNT(*) as cnt FROM inventory WHERE user_id=? GROUP BY item", (uid,))
    if not rows:
        return await message.answer("🎒 Инвентарь пуст.")
    await message.answer("🎒 <b>Инвентарь:</b>\n" + "\n".join(f"• {r['item']} x{r['cnt']}" for r in rows))

@router.message(Command(commands=["кейс", "case"]))
async def cmd_case(message: Message):
    uid = message.from_user.id
    ensure_eco(uid)
    cost = 50
    bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    if bal < cost:
        return await message.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎.")
    prizes = [
        ("💎 Кристалл",   5),
        ("🍀 Амулет",     10),
        ("🎭 VIP-статус",  2),
        ("💰 200 монет",  15),
        ("💰 100 монет",  28),
        ("💰 50 монет",   40),
    ]
    total = sum(w for _, w in prizes)
    roll = random.uniform(0, total)
    cum = 0
    prize = prizes[-1][0]
    for name, weight in prizes:
        cum += weight
        if roll <= cum:
            prize = name
            break
    db_exec("UPDATE economy SET balance=balance-? WHERE user_id=?", (cost, uid))
    if "монет" in prize:
        coins = int(prize.split()[1])
        db_exec("UPDATE economy SET balance=balance+? WHERE user_id=?", (coins, uid))
    else:
        db_exec("INSERT INTO inventory (user_id,item) VALUES (?,?)", (uid, prize))
    new_bal = db_one("SELECT balance FROM economy WHERE user_id=?", (uid,))["balance"]
    await message.answer(f"🎲 Выпало: <b>{prize}</b>!\n💰 Баланс: <b>{new_bal} 💎</b>")

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
#  RELATIONSHIPS
# ══════════════════════════════════════════════
@router.message(Command(commands=["жениться", "marry"]))
@need_reply
async def cmd_marry(message: Message):
    u1, u2 = message.from_user.id, message.reply_to_message.from_user.id
    if u1 == u2:
        return await message.answer("❌ Нельзя жениться на себе.")
    existing = db_one("SELECT 1 FROM relationships WHERE (user1=? AND user2=?) OR (user1=? AND user2=?)",
                      (u1, u2, u2, u1))
    if existing:
        return await message.answer("❌ Вы уже вместе.")
    in_rel = db_one("SELECT 1 FROM relationships WHERE user1=? OR user2=? OR user1=? OR user2=?",
                    (u1, u1, u2, u2))
    if in_rel:
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

@router.message(Command(commands=["семья", "family"]))
async def cmd_family(message: Message):
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
#  OWNER COMMANDS
# ══════════════════════════════════════════════
@router.message(Command(commands=["владелецпомощь", "ownerhelp"]))
@owner_only
async def cmd_ownerhelp(message: Message):
    await message.answer(
        "👑 <b>Команды владельца</b>\n\n"
        "/мойчаты — все чаты бота\n"
        "/статбота — статистика\n"
        "/рассылка [текст] — рассылка во все чаты\n"
        "/лог [chat_id] [ch_id] — настроить лог\n\n"
        "<b>💰 Экономика</b>\n"
        "/выдатьбаланс [user_id] [сумма]\n"
        "/забратьбаланс [user_id] [сумма]\n"
        "/добавитьтовар [цена] [название] [описание]\n"
        "/удалитьтовар [id]\n\n"
        "<b>🚫 Глобальная модерация</b>\n"
        "/глобальныйбан — бан во всех чатах (ответ)\n"
        "/глобальныйразбан — разбан во всех чатах (ответ)\n\n"
        "<b>⚙️ Прочее</b>\n"
        "/сбросбд ПОДТВЕРЖДАЮ — очистить БД"
    )

@router.message(Command(commands=["мойчаты", "mychats"]))
@owner_only
async def cmd_mychats(message: Message):
    rows = db_all("SELECT chat_id,title FROM chats", ())
    if not rows:
        return await message.answer("📋 Нет чатов.")
    lines = [f"• <code>{r['chat_id']}</code> — {r['title'] or '—'}" for r in rows]
    await message.answer("📋 <b>Чаты бота:</b>\n" + "\n".join(lines))

@router.message(Command(commands=["статбота", "botstats"]))
@owner_only
async def cmd_botstats(message: Message):
    chats = db_one("SELECT COUNT(*) as c FROM chats", ())["c"]
    users = db_one("SELECT COUNT(DISTINCT user_id) as c FROM stats", ())["c"]
    warns = db_one("SELECT COUNT(*) as c FROM warns", ())["c"]
    eco   = db_one("SELECT COUNT(*) as c FROM economy", ())["c"]
    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"💬 Чатов: <b>{chats}</b>\n"
        f"👥 Уникальных юзеров: <b>{users}</b>\n"
        f"⚠️ Активных варнов: <b>{warns}</b>\n"
        f"💰 Юзеров в экономике: <b>{eco}</b>"
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
        await message.answer(f"✅ Лог чата <code>{cid}</code> → <code>{ch}</code>")
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
    await message.answer(f"✅ Выдано <b>{amount} 💎</b> юзеру <code>{uid}</code>\nБаланс: <b>{bal} 💎</b>")

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
    await message.answer(f"✅ Товар «{args[2]}» добавлен за {price} 💎.")

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
    await message.answer(f"🚫 Глобальный бан {mn(t.id, t.full_name)}\n✅ {ok} чатов\n❌ Ошибок: {fail}")

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
    """)
    conn.commit()
    await message.answer("✅ База данных очищена.")

# ══════════════════════════════════════════════
#  LAUNCH
# ══════════════════════════════════════════════
async def main():
    dp.include_router(router)
    log.info("🚀 Replify запущен")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "chat_member", "my_chat_member"]
    )

if __name__ == "__main__":
    asyncio.run(main())
