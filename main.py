import asyncio
import sqlite3
import logging
import re
import time
import random
from datetime import datetime, timedelta
from collections import defaultdict
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
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("replify")

# ══════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════
conn = sqlite3.connect("replify.db", check_same_thread=False)
db = conn.cursor()

db.executescript("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    welcome TEXT,
    farewell TEXT,
    rules TEXT,
    log_channel INTEGER,
    antiflood_limit INTEGER DEFAULT 5,
    antiflood_action TEXT DEFAULT 'mute',
    filter_links INTEGER DEFAULT 0,
    filter_caps INTEGER DEFAULT 1,
    filter_spam INTEGER DEFAULT 1,
    antiraid INTEGER DEFAULT 0,
    chat_locked INTEGER DEFAULT 0,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    reason TEXT,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS stats (
    chat_id INTEGER,
    user_id INTEGER,
    username TEXT,
    full_name TEXT,
    messages INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS economy (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    last_bonus TIMESTAMP
);
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    item TEXT
);
CREATE TABLE IF NOT EXISTS shop (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    price INTEGER,
    description TEXT
);
CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    keyword TEXT,
    response TEXT
);
CREATE TABLE IF NOT EXISTS mute_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    word TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    user1 INTEGER,
    user2 INTEGER,
    PRIMARY KEY (user1, user2)
);
CREATE TABLE IF NOT EXISTS family (
    parent INTEGER,
    child INTEGER,
    PRIMARY KEY (parent, child)
);
CREATE TABLE IF NOT EXISTS moderators (
    chat_id INTEGER,
    user_id INTEGER,
    rank_title TEXT DEFAULT 'Модератор',
    PRIMARY KEY (chat_id, user_id)
);
""")
conn.commit()

db.execute("SELECT COUNT(*) FROM shop")
if db.fetchone()[0] == 0:
    db.executemany("INSERT INTO shop (name, price, description) VALUES (?,?,?)", [
        ("🎭 VIP-роль", 500, "Особый статус в инвентаре"),
        ("🎲 Кейс удачи", 100, "Случайный предмет внутри"),
        ("💎 Кристалл", 250, "Редкий предмет"),
        ("🍀 Амулет", 150, "Приносит удачу"),
    ])
    conn.commit()

# ── DB helpers ────────────────────────────────
def q(sql, *args, fetch=None):
    db.execute(sql, args)
    conn.commit()
    if fetch == "one": return db.fetchone()
    if fetch == "all": return db.fetchall()

def get_chat(chat_id):
    row = q("SELECT * FROM chats WHERE chat_id=?", chat_id, fetch="one")
    if not row:
        q("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", chat_id)
        row = q("SELECT * FROM chats WHERE chat_id=?", chat_id, fetch="one")
    return row

CHAT_COLS = ["chat_id","title","welcome","farewell","rules","log_channel",
             "antiflood_limit","antiflood_action","filter_links","filter_caps",
             "filter_spam","antiraid","chat_locked","added_at"]

def chat_col(chat_id, col):
    row = get_chat(chat_id)
    return row[CHAT_COLS.index(col)] if row else None

def set_chat(chat_id, col, val):
    get_chat(chat_id)
    q(f"UPDATE chats SET {col}=? WHERE chat_id=?", val, chat_id)

def ensure_economy(uid):
    q("INSERT OR IGNORE INTO economy (user_id, balance) VALUES (?,0)", uid)

# ── misc helpers ──────────────────────────────
async def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{name}</a>'

def is_group(m: Message):
    return m.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def is_admin(bot, chat_id, user_id):
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except:
        return False

async def is_mod(bot, chat_id, user_id):
    if await is_admin(bot, chat_id, user_id):
        return True
    return bool(q("SELECT 1 FROM moderators WHERE chat_id=? AND user_id=?", chat_id, user_id, fetch="one"))

async def log_action(bot, chat_id, text):
    ch = chat_col(chat_id, "log_channel")
    if ch:
        try:
            await bot.send_message(ch, f"📋 <b>Лог</b> | {text}", parse_mode="HTML")
        except:
            pass

# ══════════════════════════════════════════════
#  FLOOD TRACKER
# ══════════════════════════════════════════════
flood_tracker = defaultdict(list)

# ══════════════════════════════════════════════
#  BOT & ROUTER
# ══════════════════════════════════════════════
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# ══════════════════════════════════════════════
#  AUTO-GRANT ON BOT JOIN
# ══════════════════════════════════════════════
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated):
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
            can_invite_users=True, can_pin_messages=True, is_anonymous=False,
        )
        await bot.set_chat_administrator_custom_title(chat.id, OWNER_ID, "Владелец")
        log.info(f"Выдал права владельцу в чате {chat.id}")
    except Exception as e:
        log.warning(f"Не выдал права в {chat.id}: {e}")

# ══════════════════════════════════════════════
#  WELCOME / FAREWELL
# ══════════════════════════════════════════════
@router.chat_member()
async def on_member_change(event: ChatMemberUpdated):
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    user = event.new_chat_member.user
    cid = event.chat.id

    joined = old in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new == ChatMemberStatus.MEMBER
    left = new in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and old == ChatMemberStatus.MEMBER

    if joined:
        if chat_col(cid, "antiraid"):
            try:
                await bot.ban_chat_member(cid, user.id)
                await bot.unban_chat_member(cid, user.id)
            except:
                pass
            return
        welcome = chat_col(cid, "welcome")
        if welcome:
            text = welcome.replace("{user}", f'<a href="tg://user?id={user.id}">{user.full_name}</a>').replace("{chat}", event.chat.title or "")
            try:
                await bot.send_message(cid, text, parse_mode="HTML")
            except:
                pass

    elif left:
        farewell = chat_col(cid, "farewell")
        if farewell:
            text = farewell.replace("{user}", f'<a href="tg://user?id={user.id}">{user.full_name}</a>').replace("{chat}", event.chat.title or "")
            try:
                await bot.send_message(cid, text, parse_mode="HTML")
            except:
                pass

# ══════════════════════════════════════════════
#  AUTO-MODERATION
# ══════════════════════════════════════════════
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text.func(lambda t: not t or not t.startswith("/")))
async def automod(message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    cid = message.chat.id
    text = message.text or message.caption or ""

    # stats
    q("""INSERT INTO stats (chat_id,user_id,username,full_name,messages) VALUES (?,?,?,?,1)
         ON CONFLICT(chat_id,user_id) DO UPDATE SET messages=messages+1,
         username=excluded.username, full_name=excluded.full_name""",
      cid, uid, message.from_user.username or "", message.from_user.full_name)

    if await is_mod(bot, cid, uid):
        # check triggers even for mods
        triggers = q("SELECT keyword, response FROM triggers WHERE chat_id=?", cid, fetch="all")
        if triggers and text:
            for (kw, resp) in triggers:
                if kw.lower() in text.lower():
                    try:
                        await message.answer(resp, parse_mode="HTML")
                    except:
                        pass
                    break
        return

    if chat_col(cid, "antiraid"):
        try:
            await message.delete()
        except:
            pass
        return

    # bad words
    bad_words = q("SELECT word FROM mute_words WHERE chat_id=?", cid, fetch="all") or []
    for (w,) in bad_words:
        if w.lower() in text.lower():
            try:
                await message.delete()
                await message.answer(
                    f"🔇 {await mention(uid, message.from_user.full_name)}, сообщение удалено.",
                    parse_mode="HTML")
            except:
                pass
            return

    # link filter
    if chat_col(cid, "filter_links") and re.search(r"(https?://|t\.me/|www\.)", text, re.I):
        try:
            await message.delete()
            await message.answer(f"🔗 {await mention(uid, message.from_user.full_name)}, ссылки запрещены.", parse_mode="HTML")
        except:
            pass
        return

    # caps filter
    if chat_col(cid, "filter_caps") and len(text) > 10:
        if sum(1 for c in text if c.isupper()) / max(len(text), 1) > 0.7:
            try:
                await message.delete()
                await message.answer(f"🔠 {await mention(uid, message.from_user.full_name)}, не пиши заглавными.", parse_mode="HTML")
            except:
                pass
            return

    # antiflood
    limit = chat_col(cid, "antiflood_limit") or 5
    now = time.time()
    key = (cid, uid)
    flood_tracker[key] = [t for t in flood_tracker[key] if now - t < 5]
    flood_tracker[key].append(now)
    if len(flood_tracker[key]) >= limit:
        action = chat_col(cid, "antiflood_action") or "mute"
        flood_tracker[key] = []
        try:
            await message.delete()
            name = await mention(uid, message.from_user.full_name)
            if action == "ban":
                await bot.ban_chat_member(cid, uid)
                await message.answer(f"🚫 {name} забанен за флуд.", parse_mode="HTML")
            elif action == "kick":
                await bot.ban_chat_member(cid, uid)
                await bot.unban_chat_member(cid, uid)
                await message.answer(f"👢 {name} выкинут за флуд.", parse_mode="HTML")
            else:
                until = datetime.now() + timedelta(minutes=5)
                await bot.restrict_chat_member(cid, uid,
                    permissions=ChatPermissions(can_send_messages=False), until_date=until)
                await message.answer(f"🔇 {name} замучен за флуд на 5 минут.", parse_mode="HTML")
        except:
            pass
        return

    # triggers
    triggers = q("SELECT keyword, response FROM triggers WHERE chat_id=?", cid, fetch="all") or []
    for (kw, resp) in triggers:
        if kw.lower() in text.lower():
            try:
                await message.answer(resp, parse_mode="HTML")
            except:
                pass
            break

# ══════════════════════════════════════════════
#  BASIC
# ══════════════════════════════════════════════
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "👋 <b>Replify | Чат-менеджер</b>\n\n"
            "Добавь меня в группу и выдай права администратора.\n"
            "/помощь — все команды",
            parse_mode="HTML")

@router.message(Command(commands=["помощь","help","команды"]))
async def cmd_help(message: Message):
    await message.answer(
        "📋 <b>Replify | Команды</b>\n\n"
        "<b>👥 Информация</b>\n"
        "/админы /стафф /чат /профиль /топ /стата /пинг /ид\n\n"
        "<b>🔨 Модерация</b>\n"
        "/бан /разбан /мут [мин] /размут /кик\n"
        "/варн /варны /снятьварны\n"
        "/очистить [N] /заморозить /разморозить\n"
        "/пин /анпин\n\n"
        "<b>🤖 Автомод</b>\n"
        "/фильтрссылок вкл|выкл\n"
        "/фильтркапс вкл|выкл\n"
        "/антифлуд [лимит] [бан|мут|кик]\n"
        "/антирейд вкл|выкл\n"
        "/запретитьслово /разрешитьслово /запрещённые\n\n"
        "<b>⭐ Ранги</b>\n"
        "/назначить [звание] /снятьмодера /ранг [звание]\n\n"
        "<b>📣 Приветствие</b>\n"
        "/приветствие [текст] /прощание [текст] /правила [текст]\n"
        "Переменные: {user} {chat}\n\n"
        "<b>🎯 Триггеры</b>\n"
        "/триггер [слово] [ответ] /удалитьтриггер /триггеры\n\n"
        "<b>💰 Экономика</b>\n"
        "/баланс /бонус /перевести [сумма] /магазин /купить [id] /инвентарь /кейс\n\n"
        "<b>🎭 RP</b>\n"
        "/обнять /поцеловать /ударить /погладить /укусить /подмигнуть /пнуть\n\n"
        "<b>💑 Социальные</b>\n"
        "/жениться /развестись /семья /усыновить\n\n"
        "<b>⚙️ Владелец (ЛС)</b>\n"
        "/лог [chat_id] [channel_id] /мойчаты",
        parse_mode="HTML")

@router.message(Command(commands=["пинг","ping"]))
async def cmd_ping(message: Message):
    await message.answer("🟢 Replify работает!")

@router.message(Command(commands=["ид","id","myid"]))
async def cmd_id(message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    await message.answer(f"🆔 <b>{target.full_name}</b>: <code>{target.id}</code>", parse_mode="HTML")

# ══════════════════════════════════════════════
#  ADMINS / STAFF
# ══════════════════════════════════════════════
@router.message(Command(commands=["админы","admins"]))
async def cmd_admins(message: Message):
    if not is_group(message): return
    admins = await bot.get_chat_administrators(message.chat.id)
    lines = []
    for a in admins:
        if a.user.is_bot: continue
        title = getattr(a, "custom_title", None)
        role = title or ("👑 Создатель" if a.status == ChatMemberStatus.CREATOR else "⭐ Администратор")
        lines.append(f"• {await mention(a.user.id, a.user.full_name)} — <i>{role}</i>")
    await message.answer(f"👑 <b>Администраторы</b> ({len(lines)}):\n\n" + "\n".join(lines), parse_mode="HTML")

@router.message(Command(commands=["стафф","staff"]))
async def cmd_staff(message: Message):
    if not is_group(message): return
    admins = await bot.get_chat_administrators(message.chat.id)
    lines = []
    for a in admins:
        if a.user.is_bot: continue
        title = getattr(a, "custom_title", None)
        role = title or ("👑 Создатель" if a.status == ChatMemberStatus.CREATOR else "⭐ Администратор")
        lines.append(f"• {await mention(a.user.id, a.user.full_name)} — <i>{role}</i>")
    mods = q("SELECT user_id, rank_title FROM moderators WHERE chat_id=?", message.chat.id, fetch="all") or []
    for (uid, rank) in mods:
        lines.append(f"• <code>{uid}</code> — <i>{rank}</i>")
    await message.answer("🛡 <b>Стафф чата</b>:\n\n" + "\n".join(lines), parse_mode="HTML")

# ══════════════════════════════════════════════
#  CHAT INFO / PROFILE / STATS
# ══════════════════════════════════════════════
@router.message(Command(commands=["чат","chatinfo"]))
async def cmd_chatinfo(message: Message):
    if not is_group(message): return
    chat = await bot.get_chat(message.chat.id)
    admins = await bot.get_chat_administrators(message.chat.id)
    admin_count = sum(1 for a in admins if not a.user.is_bot)
    text = (f"💬 <b>{chat.title}</b>\n\n"
            f"🆔 ID: <code>{chat.id}</code>\n"
            f"👥 Участников: <b>{chat.member_count}</b>\n"
            f"👑 Админов: <b>{admin_count}</b>\n"
            f"🔗 Тип: <b>{'Супергруппа' if message.chat.type == ChatType.SUPERGROUP else 'Группа'}</b>\n")
    if chat.username:
        text += f"📎 @{chat.username}\n"
    if chat.description:
        text += f"\n📄 <i>{chat.description[:200]}</i>"
    await message.answer(text, parse_mode="HTML")

@router.message(Command(commands=["профиль","profile","кто"]))
async def cmd_profile(message: Message):
    if not is_group(message): return
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    member = await bot.get_chat_member(message.chat.id, target.id)
    status_map = {
        ChatMemberStatus.CREATOR: "👑 Создатель",
        ChatMemberStatus.ADMINISTRATOR: "⭐ Администратор",
        ChatMemberStatus.MEMBER: "👤 Участник",
        ChatMemberStatus.RESTRICTED: "🔇 Ограничен",
        ChatMemberStatus.LEFT: "🚪 Покинул",
        ChatMemberStatus.BANNED: "🚫 Забанен",
    }
    warns = q("SELECT COUNT(*) FROM warns WHERE chat_id=? AND user_id=?", message.chat.id, target.id, fetch="one")[0]
    stats = q("SELECT messages FROM stats WHERE chat_id=? AND user_id=?", message.chat.id, target.id, fetch="one")
    bal = q("SELECT balance FROM economy WHERE user_id=?", target.id, fetch="one")
    mod = q("SELECT rank_title FROM moderators WHERE chat_id=? AND user_id=?", message.chat.id, target.id, fetch="one")
    text = (f"👤 <b>Профиль</b> {await mention(target.id, target.full_name)}\n\n"
            f"🆔 ID: <code>{target.id}</code>\n"
            f"📌 Статус: {status_map.get(member.status, member.status)}\n")
    if mod: text += f"🎖 Ранг: <b>{mod[0]}</b>\n"
    text += (f"💬 Сообщений: <b>{stats[0] if stats else 0}</b>\n"
             f"⚠️ Варнов: <b>{warns}/3</b>\n"
             f"💰 Баланс: <b>{bal[0] if bal else 0} 💎</b>\n")
    if target.username: text += f"🔗 @{target.username}"
    await message.answer(text, parse_mode="HTML")

@router.message(Command(commands=["топ","top"]))
async def cmd_top(message: Message):
    if not is_group(message): return
    rows = q("SELECT user_id,full_name,messages FROM stats WHERE chat_id=? ORDER BY messages DESC LIMIT 10",
             message.chat.id, fetch="all")
    if not rows:
        await message.answer("📊 Статистики пока нет."); return
    medals = ["🥇","🥈","🥉"]
    lines = [f"{medals[i] if i<3 else str(i+1)+'.'} {await mention(uid, name)} — <b>{msgs}</b>"
             for i, (uid, name, msgs) in enumerate(rows)]
    await message.answer("📊 <b>Топ актива:</b>\n\n" + "\n".join(lines), parse_mode="HTML")

@router.message(Command(commands=["стата","stat"]))
async def cmd_stat(message: Message):
    if not is_group(message): return
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    row = q("SELECT messages FROM stats WHERE chat_id=? AND user_id=?", message.chat.id, target.id, fetch="one")
    await message.answer(
        f"📊 {await mention(target.id, target.full_name)} — <b>{row[0] if row else 0}</b> сообщений",
        parse_mode="HTML")

# ══════════════════════════════════════════════
#  MODERATION
# ══════════════════════════════════════════════
@router.message(Command(commands=["бан","ban"]))
async def cmd_ban(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    reason = " ".join(message.text.split()[1:]) or "Без причины"
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.answer(f"🚫 {await mention(target.id, target.full_name)} забанен.\n📌 {reason}", parse_mode="HTML")
        await log_action(bot, message.chat.id, f"БАН {target.full_name} ({target.id}) | {reason}")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["разбан","unban"]))
async def cmd_unban(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    try:
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.answer(f"✅ {await mention(target.id, target.full_name)} разбанен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["мут","mute"]))
async def cmd_mute(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    args = message.text.split()
    until, dur = None, "навсегда"
    if len(args) > 1 and args[1].isdigit():
        until = datetime.now() + timedelta(minutes=int(args[1]))
        dur = f"на {args[1]} мин."
    try:
        await bot.restrict_chat_member(message.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False), until_date=until)
        await message.answer(f"🔇 {await mention(target.id, target.full_name)} замучен {dur}.", parse_mode="HTML")
        await log_action(bot, message.chat.id, f"МУТ {target.full_name} ({target.id}) {dur}")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["размут","unmute"]))
async def cmd_unmute(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(message.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        await message.answer(f"🔊 {await mention(target.id, target.full_name)} размучен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["кик","kick"]))
async def cmd_kick(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
        await message.answer(f"👢 {await mention(target.id, target.full_name)} выкинут.", parse_mode="HTML")
        await log_action(bot, message.chat.id, f"КИК {target.full_name} ({target.id})")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["варн","warn"]))
async def cmd_warn(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    reason = " ".join(message.text.split()[1:]) or "Без причины"
    q("INSERT INTO warns (chat_id,user_id,reason) VALUES (?,?,?)", message.chat.id, target.id, reason)
    count = q("SELECT COUNT(*) FROM warns WHERE chat_id=? AND user_id=?", message.chat.id, target.id, fetch="one")[0]
    await message.answer(
        f"⚠️ {await mention(target.id, target.full_name)} получил предупреждение!\n"
        f"📌 Причина: {reason}\n📊 Варнов: {count}/3", parse_mode="HTML")
    if count >= 3:
        await bot.ban_chat_member(message.chat.id, target.id)
        q("DELETE FROM warns WHERE chat_id=? AND user_id=?", message.chat.id, target.id)
        await message.answer(f"🚫 {await mention(target.id, target.full_name)} забанен за 3 варна.", parse_mode="HTML")

@router.message(Command(commands=["варны","warns"]))
async def cmd_warns(message: Message):
    if not is_group(message): return
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    warns = q("SELECT reason,issued_at FROM warns WHERE chat_id=? AND user_id=?",
              message.chat.id, target.id, fetch="all")
    if not warns:
        await message.answer(f"✅ У {await mention(target.id, target.full_name)} нет варнов.", parse_mode="HTML"); return
    lines = [f"{i+1}. {w[0]} — <i>{w[1]}</i>" for i, w in enumerate(warns)]
    await message.answer(
        f"⚠️ Варны {await mention(target.id, target.full_name)} ({len(warns)}/3):\n\n" + "\n".join(lines),
        parse_mode="HTML")

@router.message(Command(commands=["снятьварны","clearwarns"]))
async def cmd_clearwarns(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    q("DELETE FROM warns WHERE chat_id=? AND user_id=?", message.chat.id, target.id)
    await message.answer(f"✅ Варны {await mention(target.id, target.full_name)} сняты.", parse_mode="HTML")

@router.message(Command(commands=["очистить","purge"]))
async def cmd_purge(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    args = message.text.split()
    try: count = int(args[1]) if len(args) > 1 else 10
    except: count = 10
    deleted = 0
    for i in range(count):
        try:
            await bot.delete_message(message.chat.id, message.message_id - i)
            deleted += 1
        except: pass
    m = await message.answer(f"🗑 Удалено {deleted} сообщений.")
    await asyncio.sleep(3)
    try: await m.delete()
    except: pass

@router.message(Command(commands=["заморозить","lock"]))
async def cmd_lock(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    try:
        await bot.set_chat_permissions(message.chat.id, ChatPermissions(can_send_messages=False))
        set_chat(message.chat.id, "chat_locked", 1)
        await message.answer("🔒 Чат заморожен.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["разморозить","unlock"]))
async def cmd_unlock(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    try:
        await bot.set_chat_permissions(message.chat.id,
            ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                can_send_other_messages=True, can_add_web_page_previews=True))
        set_chat(message.chat.id, "chat_locked", 0)
        await message.answer("🔓 Чат открыт.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["пин","pin"]))
async def cmd_pin(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.answer("📌 Закреплено.")
    except Exception as e:
        await message.answer(f"❌ {e}")

@router.message(Command(commands=["анпин","unpin"]))
async def cmd_unpin(message: Message):
    if not is_group(message) or not await is_mod(bot, message.chat.id, message.from_user.id): return
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.answer("📌 Откреплено.")
    except Exception as e:
        await message.answer(f"❌ {e}")

# ══════════════════════════════════════════════
#  AUTOMOD SETTINGS
# ══════════════════════════════════════════════
@router.message(Command("фильтрссылок"))
async def cmd_filter_links(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split()
    val = 1 if len(args) > 1 and args[1] == "вкл" else 0
    set_chat(message.chat.id, "filter_links", val)
    await message.answer(f"🔗 Фильтр ссылок: {'✅ вкл' if val else '❌ выкл'}")

@router.message(Command("фильтркапс"))
async def cmd_filter_caps(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split()
    val = 1 if len(args) > 1 and args[1] == "вкл" else 0
    set_chat(message.chat.id, "filter_caps", val)
    await message.answer(f"🔠 Антикапс: {'✅ вкл' if val else '❌ выкл'}")

@router.message(Command("антифлуд"))
async def cmd_antiflood(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split()
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
    action_ru = args[2] if len(args) > 2 else "мут"
    action_map = {"бан": "ban", "мут": "mute", "кик": "kick"}
    set_chat(message.chat.id, "antiflood_limit", limit)
    set_chat(message.chat.id, "antiflood_action", action_map.get(action_ru, "mute"))
    await message.answer(f"🌊 Антифлуд: {limit} сообщ/5сек → {action_ru}")

@router.message(Command("антирейд"))
async def cmd_antiraid(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split()
    val = 1 if len(args) > 1 and args[1] == "вкл" else 0
    set_chat(message.chat.id, "antiraid", val)
    await message.answer(f"🛡 Антирейд: {'✅ вкл — новички кикаются' if val else '❌ выкл'}")

@router.message(Command("запретитьслово"))
async def cmd_addword(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Укажи слово."); return
    q("INSERT INTO mute_words (chat_id,word) VALUES (?,?)", message.chat.id, args[1].lower())
    await message.answer(f"✅ «{args[1]}» запрещено.")

@router.message(Command("разрешитьслово"))
async def cmd_delword(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Укажи слово."); return
    q("DELETE FROM mute_words WHERE chat_id=? AND word=?", message.chat.id, args[1].lower())
    await message.answer(f"✅ «{args[1]}» разрешено.")

@router.message(Command(commands=["запрещённые","запрещенные"]))
async def cmd_words(message: Message):
    if not is_group(message): return
    words = q("SELECT word FROM mute_words WHERE chat_id=?", message.chat.id, fetch="all") or []
    if not words:
        await message.answer("📋 Запрещённых слов нет."); return
    await message.answer("🚫 <b>Запрещённые слова:</b>\n" + "\n".join(f"• {w[0]}" for w in words), parse_mode="HTML")

# ══════════════════════════════════════════════
#  RANKS
# ══════════════════════════════════════════════
@router.message(Command(commands=["назначить","addmod"]))
async def cmd_addmod(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    args = message.text.split(maxsplit=1)
    rank = args[1] if len(args) > 1 else "Модератор"
    q("INSERT OR REPLACE INTO moderators (chat_id,user_id,rank_title) VALUES (?,?,?)",
      message.chat.id, target.id, rank)
    await message.answer(f"✅ {await mention(target.id, target.full_name)} — {rank}", parse_mode="HTML")

@router.message(Command(commands=["снятьмодера","removemod"]))
async def cmd_removemod(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    q("DELETE FROM moderators WHERE chat_id=? AND user_id=?", message.chat.id, target.id)
    await message.answer(f"✅ {await mention(target.id, target.full_name)} снят.", parse_mode="HTML")

@router.message(Command("ранг"))
async def cmd_rank(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Укажи ранг."); return
    target = message.reply_to_message.from_user
    q("UPDATE moderators SET rank_title=? WHERE chat_id=? AND user_id=?", args[1], message.chat.id, target.id)
    await message.answer(f"🎖 Ранг {await mention(target.id, target.full_name)}: «{args[1]}»", parse_mode="HTML")

# ══════════════════════════════════════════════
#  WELCOME / FAREWELL / RULES
# ══════════════════════════════════════════════
@router.message(Command(commands=["приветствие","welcome"]))
async def cmd_welcome(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ /приветствие [текст]\nПеременные: {user} {chat}"); return
    set_chat(message.chat.id, "welcome", args[1])
    await message.answer("✅ Приветствие установлено.")

@router.message(Command(commands=["прощание","farewell"]))
async def cmd_farewell(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ /прощание [текст]"); return
    set_chat(message.chat.id, "farewell", args[1])
    await message.answer("✅ Прощание установлено.")

@router.message(Command(commands=["правила","rules"]))
async def cmd_rules(message: Message):
    if not is_group(message): return
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and await is_admin(bot, message.chat.id, message.from_user.id):
        set_chat(message.chat.id, "rules", args[1])
        await message.answer("✅ Правила установлены.")
    else:
        rules = chat_col(message.chat.id, "rules")
        await message.answer(f"📜 <b>Правила:</b>\n\n{rules}" if rules else "📜 Правила не установлены.", parse_mode="HTML")

# ══════════════════════════════════════════════
#  TRIGGERS
# ══════════════════════════════════════════════
@router.message(Command(commands=["триггер","trigger"]))
async def cmd_add_trigger(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("⚠️ /триггер [слово] [ответ]"); return
    q("INSERT INTO triggers (chat_id,keyword,response) VALUES (?,?,?)", message.chat.id, args[1], args[2])
    await message.answer(f"✅ Триггер «{args[1]}» добавлен.")

@router.message(Command("удалитьтриггер"))
async def cmd_del_trigger(message: Message):
    if not is_group(message) or not await is_admin(bot, message.chat.id, message.from_user.id): return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Укажи слово."); return
    q("DELETE FROM triggers WHERE chat_id=? AND keyword=?", message.chat.id, args[1])
    await message.answer(f"✅ Триггер «{args[1]}» удалён.")

@router.message(Command(commands=["триггеры","triggers"]))
async def cmd_list_triggers(message: Message):
    if not is_group(message): return
    rows = q("SELECT keyword,response FROM triggers WHERE chat_id=?", message.chat.id, fetch="all") or []
    if not rows:
        await message.answer("📋 Триггеров нет."); return
    lines = [f"• <b>{kw}</b> → {resp[:50]}" for kw, resp in rows]
    await message.answer("🎯 <b>Триггеры:</b>\n\n" + "\n".join(lines), parse_mode="HTML")

# ══════════════════════════════════════════════
#  ECONOMY
# ══════════════════════════════════════════════
@router.message(Command(commands=["баланс","balance","кошелёк"]))
async def cmd_balance(message: Message):
    uid = message.from_user.id
    ensure_economy(uid)
    bal = q("SELECT balance FROM economy WHERE user_id=?", uid, fetch="one")[0]
    await message.answer(f"💰 Твой баланс: <b>{bal} 💎</b>", parse_mode="HTML")

@router.message(Command(commands=["бонус","bonus","daily"]))
async def cmd_bonus(message: Message):
    uid = message.from_user.id
    ensure_economy(uid)
    row = q("SELECT last_bonus FROM economy WHERE user_id=?", uid, fetch="one")
    if row and row[0]:
        last_dt = datetime.fromisoformat(row[0])
        if datetime.now() - last_dt < timedelta(hours=24):
            remaining = timedelta(hours=24) - (datetime.now() - last_dt)
            h, m = divmod(int(remaining.total_seconds()) // 60, 60)
            await message.answer(f"⏳ Следующий бонус через <b>{h}ч {m}мин</b>.", parse_mode="HTML"); return
    amount = random.randint(50, 200)
    q("UPDATE economy SET balance=balance+?, last_bonus=? WHERE user_id=?", amount, datetime.now().isoformat(), uid)
    await message.answer(f"🎁 Ты получил <b>{amount} 💎</b>!", parse_mode="HTML")

@router.message(Command(commands=["перевести","transfer"]))
async def cmd_transfer(message: Message):
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение получателя."); return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ /перевести [сумма]"); return
    amount = int(args[1])
    sender, receiver = message.from_user.id, message.reply_to_message.from_user.id
    if sender == receiver:
        await message.answer("❌ Нельзя переводить себе."); return
    ensure_economy(sender); ensure_economy(receiver)
    bal = q("SELECT balance FROM economy WHERE user_id=?", sender, fetch="one")[0]
    if bal < amount:
        await message.answer(f"❌ Недостаточно средств ({bal} 💎)."); return
    q("UPDATE economy SET balance=balance-? WHERE user_id=?", amount, sender)
    q("UPDATE economy SET balance=balance+? WHERE user_id=?", amount, receiver)
    target = message.reply_to_message.from_user
    await message.answer(
        f"✅ {await mention(sender, message.from_user.full_name)} → "
        f"{await mention(target.id, target.full_name)}: <b>{amount} 💎</b>",
        parse_mode="HTML")

@router.message(Command(commands=["магазин","shop"]))
async def cmd_shop(message: Message):
    rows = q("SELECT id,name,price,description FROM shop", fetch="all")
    lines = [f"<b>{r[0]}.</b> {r[1]} — <b>{r[2]} 💎</b>\n<i>{r[3]}</i>" for r in rows]
    await message.answer("🛒 <b>Магазин:</b>\n\n" + "\n\n".join(lines) + "\n\n/купить [id]", parse_mode="HTML")

@router.message(Command(commands=["купить","buy"]))
async def cmd_buy(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ /купить [id]"); return
    item = q("SELECT id,name,price FROM shop WHERE id=?", int(args[1]), fetch="one")
    if not item:
        await message.answer("❌ Товар не найден."); return
    uid = message.from_user.id
    ensure_economy(uid)
    bal = q("SELECT balance FROM economy WHERE user_id=?", uid, fetch="one")[0]
    if bal < item[2]:
        await message.answer(f"❌ Нужно {item[2]} 💎, у тебя {bal} 💎."); return
    q("UPDATE economy SET balance=balance-? WHERE user_id=?", item[2], uid)
    q("INSERT INTO inventory (user_id,item) VALUES (?,?)", uid, item[1])
    await message.answer(f"✅ Куплено: {item[1]}")

@router.message(Command(commands=["инвентарь","inventory","инв"]))
async def cmd_inventory(message: Message):
    uid = message.from_user.id
    items = q("SELECT item,COUNT(*) FROM inventory WHERE user_id=? GROUP BY item", uid, fetch="all") or []
    if not items:
        await message.answer("🎒 Инвентарь пуст."); return
    await message.answer("🎒 <b>Инвентарь:</b>\n" + "\n".join(f"• {n} x{c}" for n, c in items), parse_mode="HTML")

@router.message(Command(commands=["кейс","case"]))
async def cmd_case(message: Message):
    uid = message.from_user.id
    ensure_economy(uid)
    cost = 50
    bal = q("SELECT balance FROM economy WHERE user_id=?", uid, fetch="one")[0]
    if bal < cost:
        await message.answer(f"❌ Нужно {cost} 💎, у тебя {bal} 💎."); return
    prizes = [("💎 Кристалл",5),("🍀 Амулет",10),("🎭 VIP-роль",2),
              ("💰 100 монет",40),("💰 200 монет",25),("💰 50 монет",18)]
    total = sum(w for _,w in prizes)
    roll = random.uniform(0, total)
    cum = 0; prize = prizes[-1][0]
    for name, weight in prizes:
        cum += weight
        if roll <= cum: prize = name; break
    q("UPDATE economy SET balance=balance-? WHERE user_id=?", cost, uid)
    if "монет" in prize:
        coins = int(prize.split()[1])
        q("UPDATE economy SET balance=balance+? WHERE user_id=?", coins, uid)
    else:
        q("INSERT INTO inventory (user_id,item) VALUES (?,?)", uid, prize)
    await message.answer(f"🎲 Кейс открыт! Выпало: <b>{prize}</b>!", parse_mode="HTML")

# ══════════════════════════════════════════════
#  RP COMMANDS
# ══════════════════════════════════════════════
RP = {
    "обнять": ("🤗","обнял(а)"), "поцеловать": ("😘","поцеловал(а)"),
    "ударить": ("👊","ударил(а)"), "погладить": ("🥰","погладил(а)"),
    "укусить": ("😬","укусил(а)"), "подмигнуть": ("😉","подмигнул(а)"),
    "пнуть": ("🦵","пнул(а)"), "щёкотать": ("😂","щекочет"),
}

def make_rp_handler(emoji, action):
    async def handler(message: Message):
        if not message.reply_to_message:
            await message.answer("⚠️ Ответь на сообщение участника."); return
        s = message.from_user; t = message.reply_to_message.from_user
        await message.answer(
            f"{emoji} {await mention(s.id, s.full_name)} {action} {await mention(t.id, t.full_name)}",
            parse_mode="HTML")
    return handler

for cmd_name, (em, act) in RP.items():
    router.message(Command(cmd_name))(make_rp_handler(em, act))

# ══════════════════════════════════════════════
#  RELATIONSHIPS
# ══════════════════════════════════════════════
@router.message(Command(commands=["жениться","marry"]))
async def cmd_marry(message: Message):
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    u1, u2 = message.from_user.id, message.reply_to_message.from_user.id
    if u1 == u2:
        await message.answer("❌ Нельзя жениться на себе."); return
    if q("SELECT 1 FROM relationships WHERE user1=? OR user2=? OR user1=? OR user2=?", u1,u1,u2,u2, fetch="one"):
        await message.answer("❌ Один из вас уже в отношениях."); return
    q("INSERT INTO relationships (user1,user2) VALUES (?,?)", min(u1,u2), max(u1,u2))
    await message.answer(
        f"💑 {await mention(u1, message.from_user.full_name)} и "
        f"{await mention(u2, message.reply_to_message.from_user.full_name)} теперь вместе! 💕",
        parse_mode="HTML")

@router.message(Command(commands=["развестись","divorce"]))
async def cmd_divorce(message: Message):
    uid = message.from_user.id
    if not q("SELECT 1 FROM relationships WHERE user1=? OR user2=?", uid, uid, fetch="one"):
        await message.answer("❌ Ты не в отношениях."); return
    q("DELETE FROM relationships WHERE user1=? OR user2=?", uid, uid)
    await message.answer(f"💔 {await mention(uid, message.from_user.full_name)} вышел(а) из отношений.", parse_mode="HTML")

@router.message(Command(commands=["семья","family"]))
async def cmd_family(message: Message):
    uid = message.from_user.id
    rel = q("SELECT user1,user2 FROM relationships WHERE user1=? OR user2=?", uid, uid, fetch="one")
    children = q("SELECT child FROM family WHERE parent=?", uid, fetch="all") or []
    text = f"👨‍👩‍👧 <b>Семья</b> {await mention(uid, message.from_user.full_name)}\n\n"
    if rel:
        pid = rel[1] if rel[0] == uid else rel[0]
        text += f"💑 Партнёр: <code>{pid}</code>\n"
    else:
        text += "💑 Партнёра нет\n"
    if children:
        text += f"👶 Детей: {len(children)}\n" + "".join(f"  • <code>{c[0]}</code>\n" for c in children)
    await message.answer(text, parse_mode="HTML")

@router.message(Command(commands=["усыновить","adopt"]))
async def cmd_adopt(message: Message):
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    parent, child = message.from_user.id, message.reply_to_message.from_user.id
    if parent == child:
        await message.answer("❌ Нельзя усыновить себя."); return
    q("INSERT OR IGNORE INTO family (parent,child) VALUES (?,?)", parent, child)
    await message.answer(
        f"👶 {await mention(parent, message.from_user.full_name)} усыновил(а) "
        f"{await mention(child, message.reply_to_message.from_user.full_name)}!",
        parse_mode="HTML")

# ══════════════════════════════════════════════
#  OWNER COMMANDS (DM + groups)
# ══════════════════════════════════════════════
def owner_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != OWNER_ID:
            await message.answer("❌ Только для владельца.")
            return
        await func(message, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@router.message(Command(commands=["лог","setlog"]))
@owner_only
async def cmd_setlog(message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚠️ /лог [chat_id] [channel_id]"); return
    try:
        get_chat(int(args[1]))
        set_chat(int(args[1]), "log_channel", int(args[2]))
        await message.answer(f"✅ Лог чата {args[1]} → канал {args[2]}")
    except:
        await message.answer("❌ Неверные ID.")

@router.message(Command(commands=["мойчаты","mychats"]))
@owner_only
async def cmd_mychats(message: Message):
    rows = q("SELECT chat_id,title FROM chats", fetch="all") or []
    if not rows:
        await message.answer("📋 Нет чатов."); return
    lines = [f"• <code>{cid}</code> — {title or '—'}" for cid, title in rows]
    await message.answer("📋 <b>Мои чаты:</b>\n" + "\n".join(lines), parse_mode="HTML")

@router.message(Command(commands=["рассылка","broadcast"]))
@owner_only
async def cmd_broadcast(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ /рассылка [текст]"); return
    rows = q("SELECT chat_id FROM chats", fetch="all") or []
    ok, fail = 0, 0
    for (cid,) in rows:
        try:
            await bot.send_message(cid, f"📢 <b>Объявление:</b>\n\n{args[1]}", parse_mode="HTML")
            ok += 1
        except:
            fail += 1
    await message.answer(f"✅ Отправлено: {ok}\n❌ Ошибок: {fail}")

@router.message(Command(commands=["статбота","botstats"]))
@owner_only
async def cmd_botstats(message: Message):
    chats = q("SELECT COUNT(*) FROM chats", fetch="one")[0]
    users = q("SELECT COUNT(DISTINCT user_id) FROM stats", fetch="one")[0]
    warns_total = q("SELECT COUNT(*) FROM warns", fetch="one")[0]
    eco_users = q("SELECT COUNT(*) FROM economy", fetch="one")[0]
    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"💬 Чатов: <b>{chats}</b>\n"
        f"👥 Уникальных юзеров: <b>{users}</b>\n"
        f"⚠️ Активных варнов: <b>{warns_total}</b>\n"
        f"💰 Экономика юзеров: <b>{eco_users}</b>",
        parse_mode="HTML")

@router.message(Command(commands=["выдатьбаланс","givemoney"]))
@owner_only
async def cmd_givemoney(message: Message):
    args = message.text.split()
    if len(args) < 3 or not args[1].lstrip("-").isdigit() or not args[2].lstrip("-").isdigit():
        await message.answer("⚠️ /выдатьбаланс [user_id] [сумма]"); return
    uid, amount = int(args[1]), int(args[2])
    ensure_economy(uid)
    q("UPDATE economy SET balance=balance+? WHERE user_id=?", amount, uid)
    bal = q("SELECT balance FROM economy WHERE user_id=?", uid, fetch="one")[0]
    await message.answer(f"✅ Выдано <b>{amount} 💎</b> юзеру <code>{uid}</code>\nБаланс: <b>{bal} 💎</b>", parse_mode="HTML")

@router.message(Command(commands=["забратьбаланс","takemoney"]))
@owner_only
async def cmd_takemoney(message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚠️ /забратьбаланс [user_id] [сумма]"); return
    uid, amount = int(args[1]), int(args[2])
    ensure_economy(uid)
    q("UPDATE economy SET balance=MAX(0, balance-?) WHERE user_id=?", amount, uid)
    bal = q("SELECT balance FROM economy WHERE user_id=?", uid, fetch="one")[0]
    await message.answer(f"✅ Снято <b>{amount} 💎</b> у <code>{uid}</code>\nОстаток: <b>{bal} 💎</b>", parse_mode="HTML")

@router.message(Command(commands=["глобальныйбан","gban"]))
@owner_only
async def cmd_gban(message: Message):
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    chats = q("SELECT chat_id FROM chats", fetch="all") or []
    ok, fail = 0, 0
    for (cid,) in chats:
        try:
            await bot.ban_chat_member(cid, target.id)
            ok += 1
        except:
            fail += 1
    await message.answer(
        f"🚫 <b>Глобальный бан</b> {await mention(target.id, target.full_name)}\n"
        f"✅ Забанен в {ok} чатах\n❌ Ошибок: {fail}", parse_mode="HTML")

@router.message(Command(commands=["глобальныйразбан","gunban"]))
@owner_only
async def cmd_gunban(message: Message):
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение."); return
    target = message.reply_to_message.from_user
    chats = q("SELECT chat_id FROM chats", fetch="all") or []
    ok, fail = 0, 0
    for (cid,) in chats:
        try:
            await bot.unban_chat_member(cid, target.id)
            ok += 1
        except:
            fail += 1
    await message.answer(
        f"✅ <b>Глобальный разбан</b> {await mention(target.id, target.full_name)}\n"
        f"Разбанен в {ok} чатах", parse_mode="HTML")

@router.message(Command(commands=["добавитьтовар","additem"]))
@owner_only
async def cmd_additem(message: Message):
    args = message.text.split(maxsplit=3)
    if len(args) < 4:
        await message.answer("⚠️ /добавитьтовар [цена] [название] [описание]"); return
    try:
        price = int(args[1])
    except:
        await message.answer("❌ Цена должна быть числом."); return
    q("INSERT INTO shop (name,price,description) VALUES (?,?,?)", args[2], price, args[3])
    await message.answer(f"✅ Товар «{args[2]}» добавлен за {price} 💎")

@router.message(Command(commands=["удалитьтовар","delitem"]))
@owner_only
async def cmd_delitem(message: Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ /удалитьтовар [id]"); return
    q("DELETE FROM shop WHERE id=?", int(args[1]))
    await message.answer(f"✅ Товар #{args[1]} удалён.")

@router.message(Command(commands=["сбросбд","resetdb"]))
@owner_only
async def cmd_resetdb(message: Message):
    args = message.text.split()
    if len(args) < 2 or args[1] != "ПОДТВЕРЖДАЮ":
        await message.answer("⚠️ Это сбросит всю БД!\nНапиши: /сбросбд ПОДТВЕРЖДАЮ"); return
    db.executescript("""
        DELETE FROM warns; DELETE FROM stats; DELETE FROM economy;
        DELETE FROM inventory; DELETE FROM triggers; DELETE FROM mute_words;
        DELETE FROM relationships; DELETE FROM family; DELETE FROM moderators;
    """)
    conn.commit()
    await message.answer("✅ База данных очищена (чаты сохранены).")

@router.message(Command(commands=["владелецпомощь","ownerhelp"]))
@owner_only
async def cmd_ownerhelp(message: Message):
    await message.answer(
        "👑 <b>Команды владельца</b>\n\n"
        "/мойчаты — все чаты бота\n"
        "/статбота — статистика\n"
        "/рассылка [текст] — рассылка во все чаты\n"
        "/лог [chat_id] [ch_id] — лог чата\n\n"
        "<b>💰 Экономика</b>\n"
        "/выдатьбаланс [id] [сумма]\n"
        "/забратьбаланс [id] [сумма]\n"
        "/добавитьтовар [цена] [название] [описание]\n"
        "/удалитьтовар [id]\n\n"
        "<b>🚫 Глобальная модерация</b>\n"
        "/глобальныйбан — бан во всех чатах\n"
        "/глобальныйразбан — разбан во всех чатах\n\n"
        "<b>⚙️ Прочее</b>\n"
        "/сбросбд ПОДТВЕРЖДАЮ — очистить БД",
        parse_mode="HTML")

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
