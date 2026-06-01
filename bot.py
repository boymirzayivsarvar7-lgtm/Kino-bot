"""
🎬 KINO BOT - To'liq Telegram Bot
Barcha funksiyalar bitta faylda
"""

import asyncio
import logging
import os
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InputMediaVideo, ContentType
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# ⚙️ SOZLAMALAR
# ============================================================
BOT_TOKEN = "8776094927:AAHA7sDHDNyBIXdClXbrqtW7E62qG-8q5MY"
ADMIN_ID = 8088975078
KANAL_ID = -1003908351921
KARTA_RAQAM = "8600 0000 0000 0000"

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://kino-bot-ppwl.onrender.com")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 10000))

DB_PATH = "kino_bot.db"

# VIP narxlari (so'm)
VIP_TARIFLAR = {
    "1oy": {"nomi": "🥈 1 oy", "kun": 30, "narx": 10000},
    "2oy": {"nomi": "🥇 2 oy", "kun": 60, "narx": 15000},
    "3oy": {"nomi": "👑 3 oy", "kun": 90, "narx": 20000},
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

from aiogram.client.default import DefaultBotProperties
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# Anti-flood: {user_id: last_time}
flood_cache: dict = {}

# ============================================================
# 🗄️ DATABASE
# ============================================================
async def db_init():
    """Barcha jadvallarni yaratish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            vip_status INTEGER DEFAULT 0,
            vip_expiry TEXT,
            movies_watched INTEGER DEFAULT 0,
            join_date TEXT DEFAULT CURRENT_TIMESTAMP,
            is_blocked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            title TEXT,
            year TEXT,
            country TEXT,
            genre TEXT,
            director TEXT,
            duration TEXT,
            file_id_720 TEXT,
            file_id_1080 TEXT,
            rating_sum INTEGER DEFAULT 0,
            rating_count INTEGER DEFAULT 0,
            added_date TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ratings (
            user_id INTEGER,
            movie_code TEXT,
            rating INTEGER,
            date TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, movie_code)
        );
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            channel_username TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS vip_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            tariff TEXT,
            amount INTEGER,
            photo_id TEXT,
            status TEXT DEFAULT 'pending',
            request_date TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT OR IGNORE INTO settings VALUES ('karta', '8600 0000 0000 0000');
        INSERT OR IGNORE INTO settings VALUES ('bot_active', '1');
        INSERT OR IGNORE INTO settings VALUES ('vip_1oy', '10000');
        INSERT OR IGNORE INTO settings VALUES ('vip_2oy', '15000');
        INSERT OR IGNORE INTO settings VALUES ('vip_3oy', '20000');
        INSERT OR IGNORE INTO channels (channel_id, channel_username) VALUES ('-1003908351921', '@kino_kanal');
        """)
        await db.commit()
    log.info("✅ Database tayyor")

async def db_get(query: str, params=()) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def db_all(query: str, params=()) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def db_exec(query: str, params=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

async def get_setting(key: str) -> str:
    row = await db_get("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else ""

async def set_setting(key: str, value: str):
    await db_exec("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

async def register_user(user):
    await db_exec(
        "INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?,?,?)",
        (user.id, user.full_name, user.username or "")
    )

async def get_user(user_id: int) -> Optional[dict]:
    return await db_get("SELECT * FROM users WHERE user_id=?", (user_id,))

async def is_vip(user_id: int) -> bool:
    u = await get_user(user_id)
    if not u or not u["vip_status"]:
        return False
    if u["vip_expiry"]:
        exp = datetime.fromisoformat(u["vip_expiry"])
        if exp < datetime.now():
            await db_exec("UPDATE users SET vip_status=0 WHERE user_id=?", (user_id,))
            return False
    return True

async def get_movie(code: str) -> Optional[dict]:
    return await db_get("SELECT * FROM movies WHERE code=?", (code,))

async def get_channels() -> list:
    return await db_all("SELECT * FROM channels WHERE is_active=1")

# ============================================================
# 🔒 OBUNA TEKSHIRUVI
# ============================================================
async def check_subscription(user_id: int) -> tuple[bool, list]:
    """Foydalanuvchi barcha kanallarga obuna bo'lganini tekshirish"""
    channels = await get_channels()
    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subscribed.append(ch)
        except Exception:
            not_subscribed.append(ch)
    return len(not_subscribed) == 0, not_subscribed

# ============================================================
# ⌨️ KLAVIATURALAR
# ============================================================
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🌟 VIP"), KeyboardButton(text="📊 Statistika")]],
        resize_keyboard=True
    )

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="adm_stat")],
        [InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="adm_add_movie"),
         InlineKeyboardButton(text="🗑 Kino o'chirish", callback_data="adm_del_movie")],
        [InlineKeyboardButton(text="📋 Kinolar ro'yxati", callback_data="adm_list_movies")],
        [InlineKeyboardButton(text="👑 VIP boshqaruv", callback_data="adm_vip"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="⚙️ Sozlamalar", callback_data="adm_settings")],
    ])

def vip_tarif_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥈 1 oy — 10,000 so'm", callback_data="buy_1oy")],
        [InlineKeyboardButton(text="🥇 2 oy — 15,000 so'm", callback_data="buy_2oy")],
        [InlineKeyboardButton(text="👑 3 oy — 20,000 so'm", callback_data="buy_3oy")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")],
    ])

def subscribe_kb(channels: list, movie_code: str = ""):
    btns = [[InlineKeyboardButton(text=f"📢 {ch['channel_username']}", url=f"https://t.me/{ch['channel_username'].lstrip('@')}")] for ch in channels]
    btns.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data=f"check_sub:{movie_code}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def rating_kb(movie_code: str):
    stars = ["⭐", "⭐⭐", "⭐⭐⭐", "⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"]
    btns = [[InlineKeyboardButton(text=s, callback_data=f"rate:{movie_code}:{i+1}")] for i, s in enumerate(stars)]
    return InlineKeyboardMarkup(inline_keyboard=btns)

def movie_vip_kb(movie_code: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌟 Baholash", callback_data=f"rate_menu:{movie_code}"),
         InlineKeyboardButton(text="📤 Do'stlarga", switch_inline_query=f"kino_{movie_code}")],
    ])

def confirm_vip_kb(req_id: int, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"vip_confirm:{req_id}:{user_id}"),
         InlineKeyboardButton(text="❌ Rad etish", callback_data=f"vip_reject:{req_id}:{user_id}")],
    ])

def back_kb(cb: str = "back_admin"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data=cb)]])

# ============================================================
# 🎨 FORMATLAR
# ============================================================
def stars_str(rating_sum: int, rating_count: int) -> str:
    if rating_count == 0:
        return "☆☆☆☆☆"
    avg = rating_sum / rating_count
    filled = int(round(avg))
    return "★" * filled + "☆" * (5 - filled)

def movie_caption(m: dict, quality: str) -> str:
    stars = stars_str(m["rating_sum"], m["rating_count"])
    avg = f"{m['rating_sum']/m['rating_count']:.1f}" if m["rating_count"] else "—"
    return (
        f"╔════════════════════╗\n"
        f"║ 🎬 {m['title'][:18]:<18} ║\n"
        f"║ 🗓 Yil: {m['year']:<14} ║\n"
        f"║ 🌍 Mamlakat: {m['country'][:9]:<9} ║\n"
        f"║ 🎭 Janr: {m['genre'][:12]:<12} ║\n"
        f"║ 🎬 Rejissyor: {m['director'][:7]:<7} ║\n"
        f"║ ⏱ Davomiylik: {m['duration']:<7} ║\n"
        f"║ ⭐ Reyting: {stars} ║\n"
        f"╚════════════════════╝\n"
        f"👤 Sifat: <b>{quality}</b>  |  ⭐ {avg}/5"
    )

# ============================================================
# 🛡️ ANTI-FLOOD
# ============================================================
def anti_flood(user_id: int, seconds: int = 2) -> bool:
    now = datetime.now().timestamp()
    last = flood_cache.get(user_id, 0)
    if now - last < seconds:
        return False
    flood_cache[user_id] = now
    return True

# ============================================================
# 📌 FSM STATES
# ============================================================
class AddMovie(StatesGroup):
    kod = State()
    nom = State()
    yil = State()
    mamlakat = State()
    janr = State()
    rejissyor = State()
    davomiylik = State()
    video_720 = State()
    video_1080 = State()
    tasdiqlash = State()

class DeleteMovie(StatesGroup):
    kod = State()
    tasdiq = State()

class VipPayment(StatesGroup):
    tarif = State()
    chek = State()

class BroadcastState(StatesGroup):
    target = State()
    xabar = State()

class ManualVip(StatesGroup):
    user_id = State()
    muddat = State()

class EditSetting(StatesGroup):
    karta = State()
    vip_narx = State()

class AddChannel(StatesGroup):
    channel = State()

# ============================================================
# 👤 FOYDALANUVCHI HANDLERLARI
# ============================================================
@router.message(CommandStart())
async def start_handler(msg: Message):
    try:
        bot_active = await get_setting("bot_active")
        if bot_active == "0" and msg.from_user.id != ADMIN_ID:
            await msg.answer("🔧 Bot hozirda texnik ishlar uchun to'xtatilgan. Keyinroq urinib ko'ring.")
            return
        await register_user(msg.from_user)
        text = (
            "🎬 <b>KINO BOT</b>ga xush kelibsiz!\n\n"
            "📽 Bu botda siz eng so'nggi kinolarni tomosha qilishingiz mumkin.\n\n"
            "📌 <b>Qanday ishlaydi?</b>\n"
            "• Kino kodini yuboring → kino keladi\n"
            "• Bepul: 720p sifatda\n"
            "• VIP: 1080p sifatda (yuklab olish mumkin)\n\n"
            "👇 Pastdagi tugmani bosing:"
        )
        await msg.answer(text, reply_markup=main_menu_kb())
    except Exception as e:
        log.error(f"Start xatosi: {e}")

@router.message(F.text == "🌟 VIP")
@router.message(Command("vip"))
async def vip_handler(msg: Message):
    try:
        await register_user(msg.from_user)
        user_id = msg.from_user.id
        vip = await is_vip(user_id)
        if vip:
            u = await get_user(user_id)
            exp = datetime.fromisoformat(u["vip_expiry"])
            qolgan = (exp - datetime.now()).days
            text = (
                f"👑 <b>Siz VIP foydalanuvchisiz!</b>\n\n"
                f"📅 Muddat: <b>{exp.strftime('%d.%m.%Y')}</b> gacha\n"
                f"⏳ Qoldi: <b>{qolgan} kun</b>\n\n"
                f"🔄 Muddatni uzaytirish uchun tarif tanlang:"
            )
            await msg.answer(text, reply_markup=vip_tarif_kb())
        else:
            text = (
                "🌟 <b>VIP a'zolik afzalliklari:</b>\n\n"
                "✅ 1080p yuqori sifat\n"
                "✅ Yuklab olish mumkin\n"
                "✅ Reklama yo'q\n"
                "✅ Yangi kinolar birinchi bo'lib\n\n"
                "💳 <b>Tariflar:</b>"
            )
            await msg.answer(text, reply_markup=vip_tarif_kb())
    except Exception as e:
        log.error(f"VIP handler xatosi: {e}")

@router.callback_query(F.data.startswith("buy_"))
async def buy_vip(cb: CallbackQuery, state: FSMContext):
    try:
        tarif_key = cb.data.replace("buy_", "")
        tarif = VIP_TARIFLAR.get(tarif_key)
        if not tarif:
            await cb.answer("Noma'lum tarif")
            return
        karta = await get_setting("karta")
        narx_key = f"vip_{tarif_key}"
        narx = await get_setting(narx_key)
        await state.set_state(VipPayment.chek)
        await state.update_data(tarif=tarif_key, narx=int(narx))
        text = (
            f"💳 <b>To'lov ma'lumotlari</b>\n\n"
            f"📦 Tarif: <b>{tarif['nomi']}</b>\n"
            f"💰 Summa: <b>{int(narx):,} so'm</b>\n\n"
            f"🏦 Karta raqami:\n"
            f"<code>{karta}</code>\n\n"
            f"📸 To'lovdan so'ng <b>chek rasmini</b> yuboring:"
        )
        await cb.message.edit_text(text, reply_markup=back_kb("cancel_payment"))
        await cb.answer()
    except Exception as e:
        log.error(f"Buy VIP xatosi: {e}")

@router.message(VipPayment.chek, F.photo)
async def receive_check(msg: Message, state: FSMContext):
    try:
        data = await state.get_data()
        tarif_key = data["tarif"]
        narx = data["narx"]
        tarif = VIP_TARIFLAR[tarif_key]
        photo_id = msg.photo[-1].file_id

        # DB ga saqlash
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO vip_requests (user_id, tariff, amount, photo_id) VALUES (?,?,?,?)",
                (msg.from_user.id, tarif_key, narx, photo_id)
            )
            req_id = cur.lastrowid
            await db.commit()

        await state.clear()
        await msg.answer(
            "✅ <b>Chekingiz qabul qilindi!</b>\n\n"
            "⏳ Admin tekshirib, tez orada VIP aktivlashtiradi.\n"
            "📩 Tasdiqlanganida sizga xabar yuboramiz."
        )

        # Adminga yuborish
        user = msg.from_user
        caption = (
            f"💳 <b>Yangi VIP so'rovi</b>\n\n"
            f"👤 Ism: {user.full_name}\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"📦 Tarif: {tarif['nomi']}\n"
            f"💰 Summa: {narx:,} so'm\n"
            f"📅 Sana: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
        await bot.send_photo(
            ADMIN_ID, photo_id,
            caption=caption,
            reply_markup=confirm_vip_kb(req_id, user.id)
        )
    except Exception as e:
        log.error(f"Chek qabul xatosi: {e}")
        await msg.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.")

@router.callback_query(F.data == "cancel_payment")
async def cancel_payment(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ To'lov bekor qilindi.", reply_markup=None)
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Bekor qilindi.")
    await cb.answer()

# ✅ Admin VIP tasdiqlash
@router.callback_query(F.data.startswith("vip_confirm:"))
async def vip_confirm(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q!")
        return
    try:
        _, req_id, user_id = cb.data.split(":")
        req_id, user_id = int(req_id), int(user_id)

        req = await db_get("SELECT * FROM vip_requests WHERE id=?", (req_id,))
        if not req:
            await cb.answer("So'rov topilmadi")
            return

        tarif_key = req["tariff"]
        kunlar = VIP_TARIFLAR[tarif_key]["kun"]
        expiry = (datetime.now() + timedelta(days=kunlar)).isoformat()

        await db_exec(
            "UPDATE users SET vip_status=1, vip_expiry=? WHERE user_id=?",
            (expiry, user_id)
        )
        await db_exec(
            "UPDATE vip_requests SET status='confirmed' WHERE id=?", (req_id,)
        )

        await cb.message.edit_caption(
            cb.message.caption + "\n\n✅ <b>TASDIQLANDI</b>",
            reply_markup=None
        )
        await cb.answer("✅ VIP tasdiqlandi")

        exp_date = datetime.fromisoformat(expiry).strftime("%d.%m.%Y")
        await bot.send_message(
            user_id,
            f"🎉 <b>Tabriklaymiz! VIP aktivlashtirildi!</b>\n\n"
            f"👑 Tarif: {VIP_TARIFLAR[tarif_key]['nomi']}\n"
            f"📅 Muddat: <b>{exp_date}</b> gacha\n\n"
            f"🎬 Endi 1080p sifatida kinolarni tomosha qiling!"
        )
    except Exception as e:
        log.error(f"VIP confirm xatosi: {e}")
        await cb.answer("Xatolik!")

# ❌ Admin VIP rad etish
@router.callback_query(F.data.startswith("vip_reject:"))
async def vip_reject(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q!")
        return
    try:
        _, req_id, user_id = cb.data.split(":")
        req_id, user_id = int(req_id), int(user_id)
        await db_exec("UPDATE vip_requests SET status='rejected' WHERE id=?", (req_id,))
        await cb.message.edit_caption(
            cb.message.caption + "\n\n❌ <b>RAD ETILDI</b>",
            reply_markup=None
        )
        await cb.answer("❌ Rad etildi")
        await bot.send_message(
            user_id,
            "❌ <b>Afsuski, to'lovingiz rad etildi.</b>\n\n"
            "💬 Muammo bo'lsa admin bilan bog'laning.\n"
            "🔄 Qayta to'lov qilib chek yuboring."
        )
    except Exception as e:
        log.error(f"VIP reject xatosi: {e}")

# Obuna tekshirish callback
@router.callback_query(F.data.startswith("check_sub:"))
async def check_sub_cb(cb: CallbackQuery):
    try:
        movie_code = cb.data.split(":")[1]
        ok, not_sub = await check_subscription(cb.from_user.id)
        if not ok:
            await cb.answer("❌ Hali ham obuna emassiz!", show_alert=True)
        else:
            await cb.answer("✅ Obuna tasdiqlandi!")
            await cb.message.delete()
            # Kinoni yuborish
            await send_movie_to_user(cb.message, cb.from_user, movie_code)
    except Exception as e:
        log.error(f"Check sub xatosi: {e}")

# Baholash menyu
@router.callback_query(F.data.startswith("rate_menu:"))
async def rate_menu(cb: CallbackQuery):
    movie_code = cb.data.split(":")[1]
    movie = await get_movie(movie_code)
    if not movie:
        await cb.answer("Kino topilmadi")
        return
    await cb.message.answer(
        f"⭐ <b>{movie['title']}</b> filmini baholang:",
        reply_markup=rating_kb(movie_code)
    )
    await cb.answer()

# Baholash
@router.callback_query(F.data.startswith("rate:"))
async def rate_movie(cb: CallbackQuery):
    try:
        _, code, rating = cb.data.split(":")
        rating = int(rating)
        user_id = cb.from_user.id

        existing = await db_get("SELECT * FROM ratings WHERE user_id=? AND movie_code=?", (user_id, code))
        if existing:
            old_r = existing["rating"]
            await db_exec("UPDATE ratings SET rating=? WHERE user_id=? AND movie_code=?", (rating, user_id, code))
            await db_exec(
                "UPDATE movies SET rating_sum=rating_sum-?+? WHERE code=?",
                (old_r, rating, code)
            )
        else:
            await db_exec("INSERT INTO ratings VALUES (?,?,?,?)", (user_id, code, rating, datetime.now().isoformat()))
            await db_exec("UPDATE movies SET rating_sum=rating_sum+?, rating_count=rating_count+1 WHERE code=?", (rating, code))

        stars = "⭐" * rating
        await cb.answer(f"✅ Bahoyingiz: {stars}", show_alert=True)
        await cb.message.delete()
    except Exception as e:
        log.error(f"Rate xatosi: {e}")

# ============================================================
# 🎬 KINO YUBORISH
# ============================================================
async def send_movie_to_user(msg: Message, user, movie_code: str):
    """Kinoni foydalanuvchiga yuborish"""
    try:
        movie = await get_movie(movie_code)
        if not movie:
            await msg.answer("❌ Bunday kodli kino topilmadi. Kodni tekshiring.")
            return

        vip = await is_vip(user.id)

        if vip and movie["file_id_1080"]:
            file_id = movie["file_id_1080"]
            quality = "1080p"
            protect = False
            kb = movie_vip_kb(movie_code)
        else:
            file_id = movie["file_id_720"]
            quality = "720p"
            protect = True
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌟 Baholash", callback_data=f"rate_menu:{movie_code}")],
                [InlineKeyboardButton(text="👑 VIP olish (1080p)", callback_data=f"buy_1oy")],
            ])

        caption = movie_caption(movie, quality)
        await bot.send_video(
            user.id,
            video=file_id,
            caption=caption,
            protect_content=protect,
            reply_markup=kb
        )
        # Ko'rgan filmlar sonini yangilash
        await db_exec("UPDATE users SET movies_watched=movies_watched+1 WHERE user_id=?", (user.id,))
    except Exception as e:
        log.error(f"Kino yuborish xatosi: {e}")
        await msg.answer("❌ Kino yuborishda xatolik. Qayta urinib ko'ring.")

@router.message(F.text & ~F.text.startswith("/"))
async def movie_code_handler(msg: Message, state: FSMContext):
    """Kino kodi qabul qilish"""
    try:
        # FSM state da bo'lsa, o'tkazib yuborish
        current = await state.get_state()
        if current:
            return

        if msg.from_user.id == ADMIN_ID:
            return  # Admin uchun admin handlerlari ishlaydi

        if not anti_flood(msg.from_user.id):
            return

        bot_active = await get_setting("bot_active")
        if bot_active == "0":
            await msg.answer("🔧 Bot hozirda texnik ishlar uchun to'xtatilgan.")
            return

        await register_user(msg.from_user)
        code = msg.text.strip().upper()

        # Avval kinoni tekshir
        movie = await get_movie(code)
        if not movie:
            await msg.answer("❌ Bunday kino topilmadi. Kodni tekshiring yoki admin bilan bog'laning.")
            return

        # Obunani tekshir
        ok, not_sub = await check_subscription(msg.from_user.id)
        if not ok:
            text = (
                "📢 <b>Kinoni ko'rish uchun quyidagi kanallarga obuna bo'ling:</b>\n\n"
                "Obunadan so'ng ✅ tugmasini bosing."
            )
            await msg.answer(text, reply_markup=subscribe_kb(not_sub, code))
            return

        await send_movie_to_user(msg, msg.from_user, code)
    except Exception as e:
        log.error(f"Movie code handler xatosi: {e}")

# ============================================================
# 🔑 ADMIN PANEL
# ============================================================
def admin_only(func):
    async def wrapper(event, *args, **kwargs):
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if user_id != ADMIN_ID:
            if isinstance(event, Message):
                await event.answer("❌ Ruxsat yo'q!")
            elif isinstance(event, CallbackQuery):
                await event.answer("❌ Ruxsat yo'q!", show_alert=True)
            return
        return await func(event, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@router.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("❌ Ruxsat yo'q!")
        return
    await msg.answer("🔑 <b>Admin Panel</b>", reply_markup=admin_menu_kb())

@router.callback_query(F.data == "back_admin")
async def back_admin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("🔑 <b>Admin Panel</b>", reply_markup=admin_menu_kb())
    await cb.answer()

# 📊 Statistika
@router.callback_query(F.data == "adm_stat")
async def admin_stat(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q!")
        return
    try:
        total_users = await db_get("SELECT COUNT(*) as c FROM users")
        vip_users = await db_get("SELECT COUNT(*) as c FROM users WHERE vip_status=1")
        total_movies = await db_get("SELECT COUNT(*) as c FROM movies")
        today = datetime.now().strftime("%Y-%m-%d")
        today_vip = await db_get(
            "SELECT COUNT(*) as c FROM vip_requests WHERE status='confirmed' AND date(request_date)=?",
            (today,)
        )
        text = (
            f"📊 <b>Bot Statistikasi</b>\n\n"
            f"👥 Jami foydalanuvchilar: <b>{total_users['c']}</b>\n"
            f"👑 VIP foydalanuvchilar: <b>{vip_users['c']}</b>\n"
            f"🎬 Jami kinolar: <b>{total_movies['c']}</b>\n"
            f"💳 Bugungi VIP: <b>{today_vip['c']}</b>"
        )
        await cb.message.edit_text(text, reply_markup=back_kb())
        await cb.answer()
    except Exception as e:
        log.error(f"Stat xatosi: {e}")

# 🎬 KINO QO'SHISH
@router.callback_query(F.data == "adm_add_movie")
async def adm_add_movie_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    await state.set_state(AddMovie.kod)
    await cb.message.edit_text(
        "🎬 <b>Kino qo'shish</b>\n\n"
        "1️⃣ Kino <b>kodini</b> yuboring (masalan: KN001):",
        reply_markup=back_kb()
    )
    await cb.answer()

@router.message(AddMovie.kod)
async def add_movie_kod(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    code = msg.text.strip().upper()
    existing = await get_movie(code)
    if existing:
        await msg.answer(f"⚠️ <b>{code}</b> kodi allaqachon mavjud! Boshqa kod kiriting:")
        return
    await state.update_data(kod=code)
    await state.set_state(AddMovie.nom)
    await msg.answer("2️⃣ Kino <b>nomini</b> yuboring:")

@router.message(AddMovie.nom)
async def add_movie_nom(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await state.update_data(nom=msg.text.strip())
    await state.set_state(AddMovie.yil)
    await msg.answer("3️⃣ Kino <b>yilini</b> yuboring (masalan: 2023):")

@router.message(AddMovie.yil)
async def add_movie_yil(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await state.update_data(yil=msg.text.strip())
    await state.set_state(AddMovie.mamlakat)
    await msg.answer("4️⃣ <b>Mamlakatni</b> yuboring (masalan: AQSh):")

@router.message(AddMovie.mamlakat)
async def add_movie_mamlakat(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await state.update_data(mamlakat=msg.text.strip())
    await state.set_state(AddMovie.janr)
    await msg.answer("5️⃣ <b>Janrni</b> yuboring (masalan: Drama, Komediya):")

@router.message(AddMovie.janr)
async def add_movie_janr(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await state.update_data(janr=msg.text.strip())
    await state.set_state(AddMovie.rejissyor)
    await msg.answer("6️⃣ <b>Rejissyor</b> ismini yuboring:")

@router.message(AddMovie.rejissyor)
async def add_movie_rejissyor(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await state.update_data(rejissyor=msg.text.strip())
    await state.set_state(AddMovie.davomiylik)
    await msg.answer("7️⃣ <b>Davomiylikni</b> yuboring (masalan: 1:45):")

@router.message(AddMovie.davomiylik)
async def add_movie_davomiylik(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await state.update_data(davomiylik=msg.text.strip())
    await state.set_state(AddMovie.video_720)
    await msg.answer("8️⃣ <b>720p</b> videoni yuboring (bot kanalga saqlaydi):")

@router.message(AddMovie.video_720, F.video)
async def add_movie_720(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    # Kanalga forward qilish va file_id olish
    try:
        sent = await bot.send_video(KANAL_ID, msg.video.file_id, caption="720p")
        file_id_720 = sent.video.file_id
        await state.update_data(file_id_720=file_id_720)
        await state.set_state(AddMovie.video_1080)
        await msg.answer("✅ 720p saqlandi!\n\n9️⃣ <b>1080p</b> videoni yuboring:")
    except Exception as e:
        log.error(f"Video 720 xatosi: {e}")
        await msg.answer("❌ Videoni kanalga yuborishda xatolik. Bot kanalda admin ekanligini tekshiring.")

@router.message(AddMovie.video_1080, F.video)
async def add_movie_1080(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    try:
        sent = await bot.send_video(KANAL_ID, msg.video.file_id, caption="1080p")
        file_id_1080 = sent.video.file_id
        await state.update_data(file_id_1080=file_id_1080)
        await state.set_state(AddMovie.tasdiqlash)

        data = await state.get_data()
        preview = (
            f"📋 <b>Kino ma'lumotlari:</b>\n\n"
            f"🔑 Kod: <b>{data['kod']}</b>\n"
            f"🎬 Nom: {data['nom']}\n"
            f"🗓 Yil: {data['yil']}\n"
            f"🌍 Mamlakat: {data['mamlakat']}\n"
            f"🎭 Janr: {data['janr']}\n"
            f"🎬 Rejissyor: {data['rejissyor']}\n"
            f"⏱ Davomiylik: {data['davomiylik']}\n\n"
            f"✅ Saqlashni tasdiqlaysizmi?"
        )
        await msg.answer(preview, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Saqlash", callback_data="save_movie"),
             InlineKeyboardButton(text="❌ Bekor", callback_data="back_admin")]
        ]))
    except Exception as e:
        log.error(f"Video 1080 xatosi: {e}")
        await msg.answer("❌ Videoni saqlashda xatolik.")

@router.callback_query(F.data == "save_movie")
async def save_movie(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    try:
        data = await state.get_data()
        await db_exec(
            "INSERT INTO movies (code, title, year, country, genre, director, duration, file_id_720, file_id_1080) VALUES (?,?,?,?,?,?,?,?,?)",
            (data['kod'], data['nom'], data['yil'], data['mamlakat'], data['janr'], data['rejissyor'], data['davomiylik'], data['file_id_720'], data['file_id_1080'])
        )
        await state.clear()
        await cb.message.edit_text(
            f"✅ <b>{data['nom']}</b> kinosi muvaffaqiyatli saqlandi!\n"
            f"🔑 Kod: <b>{data['kod']}</b>",
            reply_markup=back_kb()
        )
        await cb.answer("✅ Saqlandi!")
    except Exception as e:
        log.error(f"Save movie xatosi: {e}")
        await cb.answer("❌ Xatolik!")

# 🗑 KINO O'CHIRISH
@router.callback_query(F.data == "adm_del_movie")
async def adm_del_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(DeleteMovie.kod)
    await cb.message.edit_text("🗑 <b>O'chiriladigan</b> kino kodini yuboring:", reply_markup=back_kb())
    await cb.answer()

@router.message(DeleteMovie.kod)
async def del_movie_kod(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    code = msg.text.strip().upper()
    movie = await get_movie(code)
    if not movie:
        await msg.answer("❌ Bunday kino topilmadi!")
        return
    await state.update_data(kod=code)
    await state.set_state(DeleteMovie.tasdiq)
    await msg.answer(
        f"⚠️ <b>{movie['title']}</b> ({code}) kinosi o'chirilsinmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Ha, o'chir", callback_data="confirm_del"),
             InlineKeyboardButton(text="❌ Yo'q", callback_data="back_admin")]
        ])
    )

@router.callback_query(F.data == "confirm_del")
async def confirm_del(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    await db_exec("DELETE FROM movies WHERE code=?", (data["kod"],))
    await state.clear()
    await cb.message.edit_text(f"✅ <b>{data['kod']}</b> kinosi o'chirildi.", reply_markup=back_kb())
    await cb.answer("✅ O'chirildi!")

# 📋 KINOLAR RO'YXATI
@router.callback_query(F.data == "adm_list_movies")
async def adm_list_movies(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    movies = await db_all("SELECT code, title, year FROM movies ORDER BY added_date DESC LIMIT 30")
    if not movies:
        await cb.message.edit_text("🎬 Hozircha kinolar yo'q.", reply_markup=back_kb())
        await cb.answer()
        return
    lines = [f"🎬 <b>Kinolar ro'yxati</b> ({len(movies)} ta)\n"]
    for m in movies:
        lines.append(f"• <code>{m['code']}</code> — {m['title']} ({m['year']})")
    await cb.message.edit_text("\n".join(lines), reply_markup=back_kb())
    await cb.answer()

# 👑 VIP BOSHQARUV
@router.callback_query(F.data == "adm_vip")
async def adm_vip_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    await cb.message.edit_text(
        "👑 <b>VIP Boshqaruv</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 VIP ro'yxati", callback_data="vip_list")],
            [InlineKeyboardButton(text="⚠️ Muddati tugaydiganlar (3 kun)", callback_data="vip_expiring")],
            [InlineKeyboardButton(text="➕ Qo'lda VIP berish", callback_data="vip_manual_add")],
            [InlineKeyboardButton(text="➖ VIP olish", callback_data="vip_manual_remove")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_admin")],
        ])
    )
    await cb.answer()

@router.callback_query(F.data == "vip_list")
async def vip_list(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    users = await db_all("SELECT user_id, full_name, vip_expiry FROM users WHERE vip_status=1")
    if not users:
        await cb.message.edit_text("👑 VIP foydalanuvchilar yo'q.", reply_markup=back_kb("adm_vip"))
        await cb.answer()
        return
    lines = [f"👑 <b>VIP foydalanuvchilar</b> ({len(users)} ta)\n"]
    for u in users:
        exp = datetime.fromisoformat(u["vip_expiry"]).strftime("%d.%m.%Y") if u["vip_expiry"] else "—"
        lines.append(f"• {u['full_name']} (<code>{u['user_id']}</code>) — {exp}")
    await cb.message.edit_text("\n".join(lines), reply_markup=back_kb("adm_vip"))
    await cb.answer()

@router.callback_query(F.data == "vip_expiring")
async def vip_expiring(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    deadline = (datetime.now() + timedelta(days=3)).isoformat()
    users = await db_all(
        "SELECT user_id, full_name, vip_expiry FROM users WHERE vip_status=1 AND vip_expiry<?",
        (deadline,)
    )
    if not users:
        await cb.message.edit_text("✅ 3 kun ichida muddati tugaydigan VIP yo'q.", reply_markup=back_kb("adm_vip"))
        await cb.answer()
        return
    lines = [f"⚠️ <b>3 kun ichida tugaydiganlar</b> ({len(users)} ta)\n"]
    for u in users:
        exp = datetime.fromisoformat(u["vip_expiry"]).strftime("%d.%m.%Y")
        lines.append(f"• {u['full_name']} (<code>{u['user_id']}</code>) — {exp}")
    await cb.message.edit_text("\n".join(lines), reply_markup=back_kb("adm_vip"))
    await cb.answer()

@router.callback_query(F.data == "vip_manual_add")
async def vip_manual_add_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(ManualVip.user_id)
    await state.update_data(action="add")
    await cb.message.edit_text("👤 VIP beriladigan foydalanuvchi <b>ID</b>sini yuboring:", reply_markup=back_kb("adm_vip"))
    await cb.answer()

@router.callback_query(F.data == "vip_manual_remove")
async def vip_manual_remove_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(ManualVip.user_id)
    await state.update_data(action="remove")
    await cb.message.edit_text("👤 VIP olinadigan foydalanuvchi <b>ID</b>sini yuboring:", reply_markup=back_kb("adm_vip"))
    await cb.answer()

@router.message(ManualVip.user_id)
async def manual_vip_userid(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    try:
        uid = int(msg.text.strip())
        data = await state.get_data()
        if data["action"] == "remove":
            await db_exec("UPDATE users SET vip_status=0, vip_expiry=NULL WHERE user_id=?", (uid,))
            await state.clear()
            await msg.answer(f"✅ <code>{uid}</code> foydalanuvchidan VIP olindi.")
            await bot.send_message(uid, "ℹ️ VIP a'zoligingiz admin tomonidan bekor qilindi.")
        else:
            await state.update_data(uid=uid)
            await state.set_state(ManualVip.muddat)
            await msg.answer(
                "📅 Muddat (kunlarda) yuboring:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="30 kun", callback_data="manualkun:30"),
                     InlineKeyboardButton(text="60 kun", callback_data="manualkun:60"),
                     InlineKeyboardButton(text="90 kun", callback_data="manualkun:90")],
                ])
            )
    except ValueError:
        await msg.answer("❌ Noto'g'ri ID. Faqat raqam yuboring.")

@router.callback_query(F.data.startswith("manualkun:"))
async def manual_vip_kunlar(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    kunlar = int(cb.data.split(":")[1])
    data = await state.get_data()
    uid = data["uid"]
    expiry = (datetime.now() + timedelta(days=kunlar)).isoformat()
    await db_exec("UPDATE users SET vip_status=1, vip_expiry=? WHERE user_id=?", (expiry, uid))
    await state.clear()
    exp_str = datetime.fromisoformat(expiry).strftime("%d.%m.%Y")
    await cb.message.edit_text(f"✅ <code>{uid}</code> ga {kunlar} kunlik VIP berildi. ({exp_str} gacha)")
    await cb.answer()
    try:
        await bot.send_message(uid, f"🎉 Admin tomonidan sizga {kunlar} kunlik VIP berildi! ({exp_str} gacha)")
    except Exception:
        pass

# 📢 BROADCAST
@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    await cb.message.edit_text(
        "📢 <b>Broadcast</b>\n\nKimga yuborasiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👑 Faqat VIP larga", callback_data="bc_vip")],
            [InlineKeyboardButton(text="👤 Faqat bepullarga", callback_data="bc_free")],
            [InlineKeyboardButton(text="📣 Hammaga", callback_data="bc_all")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_admin")],
        ])
    )
    await cb.answer()

@router.callback_query(F.data.startswith("bc_"))
async def broadcast_target(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    target = cb.data.replace("bc_", "")
    await state.set_state(BroadcastState.xabar)
    await state.update_data(target=target)
    await cb.message.edit_text(
        "✍️ Xabarni yuboring (matn, rasm yoki video):",
        reply_markup=back_kb("adm_broadcast")
    )
    await cb.answer()

@router.message(BroadcastState.xabar)
async def do_broadcast(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    target = data["target"]

    if target == "vip":
        users = await db_all("SELECT user_id FROM users WHERE vip_status=1 AND is_blocked=0")
    elif target == "free":
        users = await db_all("SELECT user_id FROM users WHERE vip_status=0 AND is_blocked=0")
    else:
        users = await db_all("SELECT user_id FROM users WHERE is_blocked=0")

    sent, failed = 0, 0
    for u in users:
        try:
            if msg.photo:
                await bot.send_photo(u["user_id"], msg.photo[-1].file_id, caption=msg.caption or "")
            elif msg.video:
                await bot.send_video(u["user_id"], msg.video.file_id, caption=msg.caption or "")
            else:
                await bot.send_message(u["user_id"], msg.text or "")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await state.clear()
    await msg.answer(
        f"📢 <b>Broadcast yakunlandi!</b>\n\n"
        f"✅ Yuborildi: {sent}\n❌ Xato: {failed}",
        reply_markup=admin_menu_kb()
    )

# ⚙️ SOZLAMALAR
@router.callback_query(F.data == "adm_settings")
async def adm_settings(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    karta = await get_setting("karta")
    bot_active = await get_setting("bot_active")
    status = "✅ Yoqilgan" if bot_active == "1" else "❌ To'xtatilgan"
    await cb.message.edit_text(
        f"⚙️ <b>Sozlamalar</b>\n\n"
        f"💳 Karta: <code>{karta}</code>\n"
        f"🤖 Bot holati: {status}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Kartani o'zgartirish", callback_data="set_karta")],
            [InlineKeyboardButton(text="💰 VIP narxlarini o'zgartirish", callback_data="set_vip_price")],
            [InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="set_add_channel")],
            [InlineKeyboardButton(text="🗑 Kanal o'chirish", callback_data="set_del_channel")],
            [InlineKeyboardButton(text="⏸ Botni to'xtatish/yoqish", callback_data="set_toggle_bot")],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_admin")],
        ])
    )
    await cb.answer()

@router.callback_query(F.data == "set_karta")
async def set_karta_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(EditSetting.karta)
    await cb.message.edit_text("💳 Yangi karta raqamini yuboring:", reply_markup=back_kb("adm_settings"))
    await cb.answer()

@router.message(EditSetting.karta)
async def set_karta_save(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    await set_setting("karta", msg.text.strip())
    await state.clear()
    await msg.answer(f"✅ Karta raqami yangilandi: <code>{msg.text.strip()}</code>")

@router.callback_query(F.data == "set_vip_price")
async def set_vip_price(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(EditSetting.vip_narx)
    await cb.message.edit_text(
        "💰 Narxni quyidagi formatda yuboring:\n"
        "<code>1oy:10000\n2oy:15000\n3oy:20000</code>",
        reply_markup=back_kb("adm_settings")
    )
    await cb.answer()

@router.message(EditSetting.vip_narx)
async def save_vip_price(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    try:
        for line in msg.text.strip().splitlines():
            key, val = line.split(":")
            await set_setting(f"vip_{key.strip()}", val.strip())
        await state.clear()
        await msg.answer("✅ VIP narxlari yangilandi!")
    except Exception:
        await msg.answer("❌ Format noto'g'ri. Qayta urinib ko'ring.")

@router.callback_query(F.data == "set_add_channel")
async def set_add_channel_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AddChannel.channel)
    await cb.message.edit_text(
        "📢 Kanal username yoki ID yuboring:\n"
        "Masalan: @mening_kanalim yoki -1001234567890",
        reply_markup=back_kb("adm_settings")
    )
    await cb.answer()

@router.message(AddChannel.channel)
async def add_channel_save(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    ch_input = msg.text.strip()
    try:
        chat = await bot.get_chat(ch_input)
        bot_member = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if bot_member.status not in ("administrator", "creator"):
            await msg.answer("❌ Bot bu kanalda admin emas! Avval adminlik bering.")
            return
        username = f"@{chat.username}" if chat.username else str(chat.id)
        await db_exec(
            "INSERT OR IGNORE INTO channels (channel_id, channel_username) VALUES (?,?)",
            (str(chat.id), username)
        )
        await state.clear()
        await msg.answer(f"✅ <b>{chat.title}</b> kanali qo'shildi!")
    except Exception as e:
        await msg.answer(f"❌ Kanal topilmadi yoki xatolik: {e}")

@router.callback_query(F.data == "set_del_channel")
async def set_del_channel(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    channels = await get_channels()
    if not channels:
        await cb.message.edit_text("📢 Kanallar yo'q.", reply_markup=back_kb("adm_settings"))
        await cb.answer()
        return
    btns = [[InlineKeyboardButton(text=f"🗑 {ch['channel_username']}", callback_data=f"del_ch:{ch['id']}")] for ch in channels]
    btns.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="adm_settings")])
    await cb.message.edit_text("🗑 O'chiriladigan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("del_ch:"))
async def del_channel(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    ch_id = int(cb.data.split(":")[1])
    await db_exec("DELETE FROM channels WHERE id=?", (ch_id,))
    await cb.answer("✅ Kanal o'chirildi!")
    await adm_settings(cb)

@router.callback_query(F.data == "set_toggle_bot")
async def toggle_bot(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    current = await get_setting("bot_active")
    new_val = "0" if current == "1" else "1"
    await set_setting("bot_active", new_val)
    status = "✅ Yoqildi" if new_val == "1" else "⏸ To'xtatildi"
    await cb.answer(status, show_alert=True)
    await adm_settings(cb)

# ============================================================
# ⏰ SCHEDULER — VIP muddati tekshiruvi
# ============================================================
async def check_vip_expiry():
    """Har kecha 00:00 da VIP muddati tugaganlarni tekshirish"""
    try:
        now = datetime.now().isoformat()
        expired_users = await db_all(
            "SELECT user_id, full_name FROM users WHERE vip_status=1 AND vip_expiry<?",
            (now,)
        )
        for u in expired_users:
            await db_exec(
                "UPDATE users SET vip_status=0 WHERE user_id=?", (u["user_id"],)
            )
            try:
                await bot.send_message(
                    u["user_id"],
                    "⏰ <b>VIP muddatingiz tugadi!</b>\n\n"
                    "Yangi VIP olish uchun /vip buyrug'ini yuboring.\n"
                    "🎬 720p filmlar hali ham mavjud!"
                )
            except Exception:
                pass
        if expired_users:
            log.info(f"✅ {len(expired_users)} ta VIP foydalanuvchi muddati tugagan")
    except Exception as e:
        log.error(f"VIP expiry check xatosi: {e}")

# ============================================================
# 🚀 ISHGA TUSHIRISH
# ============================================================
async def on_startup(app: web.Application):
    await db_init()
    await bot.set_webhook(WEBHOOK_URL)

    scheduler = AsyncIOScheduler(timezone="Asia/Tashkent")
    scheduler.add_job(check_vip_expiry, "cron", hour=0, minute=0)
    scheduler.start()
    app["scheduler"] = scheduler
    log.info(f"✅ Bot ishga tushdi. Webhook: {WEBHOOK_URL}")

async def on_shutdown(app: web.Application):
    scheduler = app.get("scheduler")
    if scheduler:
        scheduler.shutdown()
    await bot.delete_webhook()
    await bot.session.close()
    log.info("Bot to'xtatildi.")

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    main()
