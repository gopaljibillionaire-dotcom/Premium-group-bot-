"""
Production Telegram Pack Bot
Python 3.11+
aiogram 3.x + Motor + MongoDB + FastAPI + OxaPay

Everything is intentionally contained in this one file.

Before running:
1. Replace BOT_TOKEN / MONGO_URI / ADMIN_IDS / CHANNEL_ID.
2. Configure OxaPay API key.
3. Configure a public HTTPS OxaPay webhook URL.
4. Add the bot to the authorized source channel with sufficient permissions.
"""

import os
import re
import hmac
import hashlib
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Optional, Any

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import uvicorn

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8925435092:AAGQzPGeIoCq91T_IkP2GSAsiHUdrChwxVw")
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://Gopaljichoubey:gopaljichoubey12@cluster0.qlsuf4o.mongodb.net/?appName=Cluster0")
DATABASE_NAME = os.getenv("DATABASE_NAME", "pack_bot")

ADMIN_IDS = [
    7952327997
]

CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1004391777541"))

OXAPAY_MERCHANT_API_KEY = os.getenv(
    "OXAPAY_MERCHANT_API_KEY",
    "YOUR_OXAPAY_API_KEY",
)

# Public HTTPS endpoint, e.g.
# https://your-domain.com/webhook/oxapay
OXAPAY_WEBHOOK_URL = os.getenv(
    "OXAPAY_WEBHOOK_URL",
    "https://YOUR_DOMAIN/webhook/oxapay",
)

UPI_ID = os.getenv("UPI_ID", "yourupi@upi")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "your_admin_username")

STAR_TO_INR_RATE = float(os.getenv("STAR_TO_INR_RATE", "1.5"))

# OxaPay default v1 API.
OXAPAY_API_BASE = "https://api.oxapay.com/v1"

# How much USD/USDT is credited per dollar paid.
# For a real production service, make these rates configurable
# from the admin panel or an external price source.
USDT_TO_INR_RATE = float(os.getenv("USDT_TO_INR_RATE", "90"))

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("pack_bot")


# ============================================================
# GLOBALS
# ============================================================

mongo_client: Optional[AsyncIOMotorClient] = None
db = None

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

app = FastAPI(title="Telegram Pack Bot")


# ============================================================
# HELPERS
# ============================================================

def now() -> datetime:
    return datetime.now(timezone.utc)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def money(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "0.00"


def safe_text(value: Any, limit: int = 1000) -> str:
    text = str(value or "")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text[:limit]


def username_text(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return str(user.id)


def parse_amount(text: str) -> Optional[float]:
    try:
        value = Decimal(text.replace(",", "").strip())
        if value <= 0:
            return None
        return float(value)
    except (InvalidOperation, ValueError):
        return None


def main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📦 Buy Pack", callback_data="menu:packs", style="success"),
            InlineKeyboardButton(text="💳 Pay Money", callback_data="menu:money", style="primary"),
        ],
        [
            InlineKeyboardButton(
                text="📅 Book Custom Appointment",
                callback_data="menu:appointment",
                style="primary",
            ),
        ],
        [
            InlineKeyboardButton(
                text="💬 Direct Talk",
                callback_data="menu:support",
                style="primary",
            ),
        ],
        [
            InlineKeyboardButton(text="👛 Wallet", callback_data="menu:wallet", style="primary"),
            InlineKeyboardButton(text="⚡ Top Up", callback_data="menu:topup", style="success"),
        ],
        [
            InlineKeyboardButton(
                text="🛍️ My Purchases",
                callback_data="menu:purchases",
                style="primary",
            ),
            InlineKeyboardButton(
                text="❓ Help",
                callback_data="menu:help",
                style="primary",
            ),
        ],
    ]

    if is_admin(user_id):
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚙️ Admin Panel",
                    callback_data="admin:panel",
                    style="danger",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="menu:main",
                )
            ]
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="menu:main",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="flow:cancel",
                    style="danger",
                ),
            ]
        ]
    )


def pack_media_counts(pack: dict) -> tuple[int, int]:
    media = pack.get("media", [])
    photos = sum(1 for x in media if x.get("type") == "photo")
    videos = sum(1 for x in media if x.get("type") == "video")
    return photos, videos


async def notify_admins(text: str, reply_markup=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("Failed notifying admin %s", admin_id)


async def ensure_user(tg_user):
    existing = await db.users.find_one({"telegram_id": tg_user.id})

    if existing:
        update = {
            "username": tg_user.username,
            "first_name": tg_user.first_name,
            "updated_at": now(),
        }

        await db.users.update_one(
            {"telegram_id": tg_user.id},
            {"$set": update},
        )

        return existing

    document = {
        "telegram_id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "language": tg_user.language_code or "en",
        "created_at": now(),
        "updated_at": now(),
        "balances": {
            "inr": 0.0,
            "usd": 0.0,
            "usdt": 0.0,
            "stars": 0,
        },
        "blocked": False,
    }

    await db.users.insert_one(document)

    await notify_admins(
        "👤 <b>New User</b>\n\n"
        f"User: {safe_text(username_text(tg_user))}\n"
        f"ID: <code>{tg_user.id}</code>"
    )

    return document


async def get_user(user_id: int):
    return await db.users.find_one({"telegram_id": user_id})


async def next_pack_id() -> str:
    counter = await db.counters.find_one_and_update(
        {"_id": "packs"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    return f"PACK-{int(counter['value']):06d}"


async def next_purchase_id() -> str:
    counter = await db.counters.find_one_and_update(
        {"_id": "purchases"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    return f"PUR-{int(counter['value']):08d}"


async def next_payment_id() -> str:
    counter = await db.counters.find_one_and_update(
        {"_id": "payments"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    return f"PAY-{int(counter['value']):08d}"


# ============================================================
# FSM STATES
# ============================================================

class AddPackStates(StatesGroup):
    name = State()
    description = State()
    media = State()
    price_inr = State()
    price_usd = State()
    price_usdt = State()
    price_stars = State()
    preview = State()


class UPIStates(StatesGroup):
    amount = State()
    proof = State()


class CryptoStates(StatesGroup):
    amount = State()


class StarsTopUpStates(StatesGroup):
    amount = State()


class AppointmentStates(StatesGroup):
    name = State()
    date = State()
    time = State()
    description = State()
    confirmation = State()


class SupportStates(StatesGroup):
    chatting = State()


class BalanceAdminStates(StatesGroup):
    user_id = State()
    currency = State()
    amount = State()


class BroadcastStates(StatesGroup):
    message = State()


# ============================================================
# KEYBOARDS
# ============================================================

def admin_panel_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Add Pack",
                    callback_data="admin:add_pack",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="📦 Manage Packs",
                    callback_data="admin:packs",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👥 Manage Users",
                    callback_data="admin:users",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📑 Payment Verification",
                    callback_data="admin:payments",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📅 Appointments",
                    callback_data="admin:appointments",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📢 Broadcast",
                    callback_data="admin:broadcast",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="📊 Statistics",
                    callback_data="admin:stats",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Main Menu",
                    callback_data="menu:main",
                )
            ],
        ]
    )


def media_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📸 Add Photo",
                    callback_data="packmedia:photo",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🎥 Add Video",
                    callback_data="packmedia:video",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📢 Add From Channel",
                    callback_data="packmedia:channel",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Finish Pack",
                    callback_data="packmedia:finish",
                    style="success",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="admin:panel",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="flow:cancel",
                    style="danger",
                ),
            ],
        ]
    )


def price_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇮🇳 INR Price",
                    callback_data="packprice:inr",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🇺🇸 USD Price",
                    callback_data="packprice:usd",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💵 USDT Price",
                    callback_data="packprice:usdt",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="⭐ Stars Price",
                    callback_data="packprice:stars",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⏩ Skip",
                    callback_data="packprice:skip",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="admin:panel",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="flow:cancel",
                    style="danger",
                ),
            ],
        ]
    )


def wallet_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚡ Top Up",
                    callback_data="menu:topup",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="📜 Transaction History",
                    callback_data="wallet:history",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="menu:main",
                )
            ],
        ]
    )


def topup_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇮🇳 UPI",
                    callback_data="topup:upi",
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="💵 Crypto",
                    callback_data="topup:crypto",
                    style="primary",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Telegram Stars",
                    callback_data="topup:stars",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Back",
                    callback_data="menu:main",
                )
            ],
        ]
    )


# ============================================================
# START / BASIC COMMANDS
# ============================================================

@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.from_user)

    await message.answer(
        "Hello 👋\n"
        "Welcome! Choose an option below to continue.",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(Command("help"))
async def help_handler(message: Message):
    await ensure_user(message.from_user)

    await message.answer(
        "<b>Help</b>\n\n"
        "Use the menu to buy packs, manage your wallet, "
        "top up your balance, book an appointment, or contact support.",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(Command("wallet"))
async def wallet_command(message: Message):
    await show_wallet(message.from_user.id, message)


@router.message(Command("packs"))
async def packs_command(message: Message):
    await show_packs(message, 1)


@router.message(Command("purchases"))
async def purchases_command(message: Message):
    await show_purchases(message.from_user.id, message)


@router.message(Command("topup"))
async def topup_command(message: Message):
    await message.answer(
        "Choose a top-up method:",
        reply_markup=topup_keyboard(),
    )


@router.message(Command("appointment"))
async def appointment_command(message: Message, state: FSMContext):
    await start_appointment(message, state)


@router.message(Command("support"))
async def support_command(message: Message, state: FSMContext):
    await start_support(message, state)


@router.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("This command is only available to administrators.")
        return

    await message.answer(
        "<b>Admin Panel</b>",
        reply_markup=admin_panel_keyboard(),
    )


# ============================================================
# MAIN MENU CALLBACKS
# ============================================================

@router.callback_query(F.data == "menu:main")
async def menu_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()

    await call.message.edit_text(
        "Hello 👋\n"
        "Welcome! Choose an option below to continue.",
        reply_markup=main_menu(call.from_user.id),
    )


@router.callback_query(F.data == "menu:packs")
async def menu_packs(call: CallbackQuery):
    await call.answer()
    await show_packs(call.message, 1)


@router.callback_query(F.data == "menu:money")
async def menu_money(call: CallbackQuery):
    await call.answer()

    user = await get_user(call.from_user.id)
    balances = user.get("balances", {})

    await call.message.edit_text(
        "<b>Payment / Wallet</b>\n\n"
        f"🇮🇳 INR: ₹{money(balances.get('inr', 0))}\n"
        f"💵 USDT: {money(balances.get('usdt', 0), 6)}\n"
        f"⭐ Stars: {int(balances.get('stars', 0))}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🇮🇳 Add INR",
                        callback_data="topup:upi",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="💵 Add USDT",
                        callback_data="topup:crypto",
                        style="success",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⭐ Add Stars",
                        callback_data="topup:stars",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📜 Wallet History",
                        callback_data="wallet:history",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="menu:main",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data == "menu:wallet")
async def menu_wallet(call: CallbackQuery):
    await call.answer()
    await show_wallet(call.from_user.id, call.message)


@router.callback_query(F.data == "menu:topup")
async def menu_topup(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "Choose a top-up method:",
        reply_markup=topup_keyboard(),
    )


@router.callback_query(F.data == "menu:purchases")
async def menu_purchases(call: CallbackQuery):
    await call.answer()
    await show_purchases(call.from_user.id, call.message)


@router.callback_query(F.data == "menu:help")
async def menu_help(call: CallbackQuery):
    await call.answer()

    await call.message.edit_text(
        "<b>Help</b>\n\n"
        "• Buy Pack — browse available packs.\n"
        "• Pay Money — view payment balances.\n"
        "• Top Up — add funds.\n"
        "• Wallet — view balances and transactions.\n"
        "• Appointment — request a custom appointment.\n"
        "• Direct Talk — contact support.",
        reply_markup=back_menu(),
    )


@router.callback_query(F.data == "menu:appointment")
async def menu_appointment(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_appointment(call.message, state)


@router.callback_query(F.data == "menu:support")
async def menu_support(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_support(call.message, state)


# ============================================================
# PACK LISTING
# ============================================================

async def show_packs(message: Message, page: int = 1):
    page_size = 5
    skip = (page - 1) * page_size

    total = await db.packs.count_documents({"active": True})

    packs = await (
        db.packs.find({"active": True})
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(page_size)
        .to_list(length=page_size)
    )

    total_pages = max(1, (total + page_size - 1) // page_size)

    if page > total_pages:
        page = total_pages

    if not packs:
        await message.edit_text(
            "📦 No packs are currently available.",
            reply_markup=back_menu(),
        )
        return

    rows = []

    for pack in packs:
        photos, videos = pack_media_counts(pack)

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👁️ View • {pack['name'][:25]}",
                    callback_data=f"pack:view:{pack['pack_id']}",
                    style="primary",
                )
            ]
        )

    navigation = []

    if page > 1:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Previous",
                callback_data=f"packs:page:{page - 1}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"📄 Page {page}/{total_pages}",
            callback_data="noop",
        )
    )

    if page < total_pages:
        navigation.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=f"packs:page:{page + 1}",
            )
        )

    rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="menu:main",
            )
        ]
    )

    display = "📦 <b>Available Packs</b>\n\n"

    for pack in packs:
        photos, videos = pack_media_counts(pack)

        display += (
            f"<b>{safe_text(pack['name'])}</b>\n"
            f"📸 {photos} Photos • 🎥 {videos} Videos\n"
            f"₹{money(pack.get('price_inr', 0))} / "
            f"${money(pack.get('price_usd', 0))} / "
            f"{money(pack.get('price_usdt', 0), 6)} USDT / "
            f"{int(pack.get('price_stars', 0))} ⭐\n\n"
        )

    await message.edit_text(
        display,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("packs:page:"))
async def packs_page(call: CallbackQuery):
    await call.answer()

    page = int(call.data.split(":")[-1])
    await show_packs(call.message, page)


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


# ============================================================
# PACK DETAILS
# ============================================================

@router.callback_query(F.data.startswith("pack:view:"))
async def pack_view(call: CallbackQuery):
    await call.answer()

    pack_id = call.data.split(":", 2)[2]
    pack = await db.packs.find_one(
        {
            "pack_id": pack_id,
            "active": True,
        }
    )

    if not pack:
        await call.message.edit_text(
            "This pack is no longer available.",
            reply_markup=back_menu(),
        )
        return

    photos, videos = pack_media_counts(pack)

    await call.message.edit_text(
        f"📦 <b>{safe_text(pack['name'])}</b>\n\n"
        f"{safe_text(pack.get('description', ''))}\n\n"
        f"📸 Photos: {photos}\n"
        f"🎥 Videos: {videos}\n"
        f"📦 Total Media: {photos + videos}\n\n"
        "<b>Prices</b>\n"
        f"🇮🇳 INR: ₹{money(pack.get('price_inr', 0))}\n"
        f"🇺🇸 USD: ${money(pack.get('price_usd', 0))}\n"
        f"💵 USDT: {money(pack.get('price_usdt', 0), 6)}\n"
        f"⭐ Stars: {int(pack.get('price_stars', 0))}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🛒 Buy Now",
                        callback_data=f"pack:buy:{pack_id}",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="menu:packs",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("pack:buy:"))
async def pack_buy(call: CallbackQuery):
    await call.answer()

    pack_id = call.data.split(":", 2)[2]
    pack = await db.packs.find_one(
        {
            "pack_id": pack_id,
            "active": True,
        }
    )

    if not pack:
        await call.message.edit_text(
            "Pack unavailable.",
            reply_markup=back_menu(),
        )
        return

    await call.message.edit_text(
        "Choose your payment method:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⭐ Pay with Stars",
                        callback_data=f"pay:stars:{pack_id}",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💵 Pay with USDT Balance",
                        callback_data=f"pay:usdt:{pack_id}",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🇮🇳 Pay with INR Balance",
                        callback_data=f"pay:inr:{pack_id}",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💳 Top Up Wallet",
                        callback_data="menu:topup",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data=f"pack:view:{pack_id}",
                    )
                ],
            ]
        ),
    )


# ============================================================
# STARS PACK PAYMENT
# ============================================================

@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_pack_stars(call: CallbackQuery):
    await call.answer()

    pack_id = call.data.split(":", 2)[2]

    pack = await db.packs.find_one(
        {
            "pack_id": pack_id,
            "active": True,
        }
    )

    if not pack:
        await call.message.answer("Pack unavailable.")
        return

    amount = int(pack.get("price_stars", 0))

    if amount <= 0:
        await call.message.answer(
            "Stars payment is not configured for this pack."
        )
        return

    payload = f"pack|{pack_id}|{call.from_user.id}|{amount}"

    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=pack["name"][:32],
            description=(
                pack.get("description", "Pack purchase")[:255]
            ),
            payload=payload,
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=pack["name"][:32],
                    amount=amount,
                )
            ],
            provider_token="",
        )
    except Exception:
        logger.exception("Stars invoice error")
        await call.message.answer(
            "Unable to create the Stars invoice right now."
        )


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    try:
        parts = query.invoice_payload.split("|")

        if len(parts) != 4 or parts[0] != "pack":
            await query.answer(
                ok=False,
                error_message="Invalid payment payload.",
            )
            return

        pack_id = parts[1]
        user_id = int(parts[2])
        expected_amount = int(parts[3])

        if user_id != query.from_user.id:
            await query.answer(
                ok=False,
                error_message="Payment user mismatch.",
            )
            return

        pack = await db.packs.find_one(
            {
                "pack_id": pack_id,
                "active": True,
            }
        )

        if not pack:
            await query.answer(
                ok=False,
                error_message="This pack is unavailable.",
            )
            return

        if int(pack.get("price_stars", 0)) != expected_amount:
            await query.answer(
                ok=False,
                error_message="Price has changed. Please create a new invoice.",
            )
            return

        if query.total_amount != expected_amount:
            await query.answer(
                ok=False,
                error_message="Invalid payment amount.",
            )
            return

        await query.answer(ok=True)

    except Exception:
        logger.exception("Pre-checkout error")

        try:
            await query.answer(
                ok=False,
                error_message="Unable to validate payment.",
            )
        except Exception:
            pass


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    if not payment:
        return

    if payment.currency != "XTR":
        return

    payload = payment.invoice_payload
    parts = payload.split("|")

    if len(parts) != 4 or parts[0] != "pack":
        return

    pack_id = parts[1]

    try:
        expected_user_id = int(parts[2])
        expected_amount = int(parts[3])
    except ValueError:
        return

    if expected_user_id != message.from_user.id:
        logger.warning("Payment user mismatch")
        return

    if payment.total_amount != expected_amount:
        logger.warning("Stars amount mismatch")
        return

    # Idempotency based on Telegram charge ID.
    existing = await db.payments.find_one(
        {
            "telegram_payment_charge_id":
                payment.telegram_payment_charge_id
        }
    )

    if existing:
        await message.answer(
            "This payment has already been processed."
        )
        return

    pack = await db.packs.find_one(
        {
            "pack_id": pack_id,
            "active": True,
        }
    )

    if not pack:
        await message.answer(
            "Payment received, but the pack is unavailable. "
            "Please contact support."
        )
        return

    payment_id = await next_payment_id()

    await db.payments.insert_one(
        {
            "payment_id": payment_id,
            "user_id": message.from_user.id,
            "method": "stars",
            "status": "paid",
            "pack_id": pack_id,
            "amount": payment.total_amount,
            "currency": "XTR",
            "telegram_payment_charge_id":
                payment.telegram_payment_charge_id,
            "provider_payment_charge_id":
                payment.provider_payment_charge_id,
            "created_at": now(),
        }
    )

    purchase = await create_purchase_and_deliver(
        user_id=message.from_user.id,
        pack=pack,
        amount=payment.total_amount,
        currency="XTR",
        payment_method="telegram_stars",
        reference=payment.telegram_payment_charge_id,
    )

    if purchase:
        await message.answer(
            "Purchase completed successfully ✅\n\n"
            "Your pack has been delivered."
        )

        await notify_admins(
            "⭐ <b>Successful Stars Purchase</b>\n\n"
            f"User: {safe_text(username_text(message.from_user))}\n"
            f"Pack: {safe_text(pack['name'])}\n"
            f"Amount: {payment.total_amount} ⭐"
        )


# ============================================================
# BALANCE PURCHASES
# ============================================================

async def deduct_balance(
    user_id: int,
    currency: str,
    amount: float,
    reference: str,
) -> Optional[dict]:
    if amount <= 0:
        return None

    field = f"balances.{currency}"

    user = await db.users.find_one_and_update(
        {
            "telegram_id": user_id,
            field: {"$gte": amount},
            "blocked": {"$ne": True},
        },
        {
            "$inc": {
                field: -amount,
            },
            "$set": {
                "updated_at": now(),
            },
        },
        return_document=ReturnDocument.BEFORE,
    )

    if not user:
        return None

    before = float(
        user.get("balances", {}).get(currency, 0)
    )
    after = before - amount

    await db.transactions.insert_one(
        {
            "user_id": user_id,
            "type": "purchase_debit",
            "currency": currency,
            "amount": -amount,
            "balance_before": before,
            "balance_after": after,
            "reference": reference,
            "status": "completed",
            "created_at": now(),
        }
    )

    return {
        "before": before,
        "after": after,
    }


@router.callback_query(F.data.startswith("pay:usdt:"))
async def pay_pack_usdt(call: CallbackQuery):
    await call.answer()

    pack_id = call.data.split(":", 2)[2]

    pack = await db.packs.find_one(
        {
            "pack_id": pack_id,
            "active": True,
        }
    )

    if not pack:
        await call.message.answer("Pack unavailable.")
        return

    amount = float(pack.get("price_usdt", 0))

    if amount <= 0:
        await call.message.answer(
            "USDT balance payment is not configured."
        )
        return

    reference = f"PACK:{pack_id}:USDT:{call.from_user.id}"

    result = await deduct_balance(
        call.from_user.id,
        "usdt",
        amount,
        reference,
    )

    if not result:
        await call.message.edit_text(
            "Insufficient USDT balance.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💵 Top Up USDT",
                            callback_data="topup:crypto",
                            style="success",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ Back",
                            callback_data=f"pack:view:{pack_id}",
                        )
                    ],
                ]
            ),
        )
        return

    await create_purchase_and_deliver(
        user_id=call.from_user.id,
        pack=pack,
        amount=amount,
        currency="USDT",
        payment_method="usdt_balance",
        reference=reference,
    )

    await call.message.edit_text(
        "Purchase completed successfully ✅\n\n"
        "Your pack has been delivered.",
        reply_markup=main_menu(call.from_user.id),
    )


@router.callback_query(F.data.startswith("pay:inr:"))
async def pay_pack_inr(call: CallbackQuery):
    await call.answer()

    pack_id = call.data.split(":", 2)[2]

    pack = await db.packs.find_one(
        {
            "pack_id": pack_id,
            "active": True,
        }
    )

    if not pack:
        await call.message.answer("Pack unavailable.")
        return

    amount = float(pack.get("price_inr", 0))

    if amount <= 0:
        await call.message.answer(
            "INR balance payment is not configured."
        )
        return

    reference = f"PACK:{pack_id}:INR:{call.from_user.id}"

    result = await deduct_balance(
        call.from_user.id,
        "inr",
        amount,
        reference,
    )

    if not result:
        await call.message.edit_text(
            "Insufficient INR balance.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🇮🇳 Top Up INR",
                            callback_data="topup:upi",
                            style="success",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ Back",
                            callback_data=f"pack:view:{pack_id}",
                        )
                    ],
                ]
            ),
        )
        return

    await create_purchase_and_deliver(
        user_id=call.from_user.id,
        pack=pack,
        amount=amount,
        currency="INR",
        payment_method="inr_balance",
        reference=reference,
    )

    await call.message.edit_text(
        "Purchase completed successfully ✅\n\n"
        "Your pack has been delivered.",
        reply_markup=main_menu(call.from_user.id),
    )


# ============================================================
# PURCHASE DELIVERY
# ============================================================

async def create_purchase_and_deliver(
    user_id: int,
    pack: dict,
    amount: float,
    currency: str,
    payment_method: str,
    reference: str,
):
    purchase_id = await next_purchase_id()

    photos, videos = pack_media_counts(pack)

    purchase = {
        "purchase_id": purchase_id,
        "user_id": user_id,
        "pack_id": pack["pack_id"],
        "pack_name": pack["name"],
        "amount": amount,
        "currency": currency,
        "photos_count": photos,
        "videos_count": videos,
        "payment_method": payment_method,
        "reference": reference,
        "created_at": now(),
    }

    # Unique reference prevents duplicate purchase creation.
    try:
        await db.purchases.insert_one(purchase)
    except Exception:
        existing = await db.purchases.find_one(
            {"reference": reference}
        )
        if existing:
            return existing
        raise

    media = pack.get("media", [])

    for item in media:
        try:
            if item["type"] == "photo":
                await bot.send_photo(
                    chat_id=user_id,
                    photo=item["file_id"],
                )

            elif item["type"] == "video":
                await bot.send_video(
                    chat_id=user_id,
                    video=item["file_id"],
                )

            await asyncio.sleep(0.05)

        except TelegramForbiddenError:
            logger.warning(
                "User blocked bot: %s",
                user_id,
            )
            break
        except TelegramBadRequest:
            logger.exception(
                "Media delivery failed for %s",
                user_id,
            )

    return purchase


# ============================================================
# WALLET
# ============================================================

async def show_wallet(user_id: int, target: Message):
    user = await get_user(user_id)

    if not user:
        return

    balances = user.get("balances", {})

    text = (
        "💰 <b>My Wallet</b>\n\n"
        f"🇮🇳 INR Balance: ₹{money(balances.get('inr', 0))}\n"
        f"💵 USDT Balance: {money(balances.get('usdt', 0), 6)}\n"
        f"⭐ Stars Balance: {int(balances.get('stars', 0))}"
    )

    await target.edit_text(
        text,
        reply_markup=wallet_keyboard(),
    )


@router.callback_query(F.data == "wallet:history")
async def wallet_history(call: CallbackQuery):
    await call.answer()

    transactions = await (
        db.transactions.find(
            {"user_id": call.from_user.id}
        )
        .sort("created_at", DESCENDING)
        .limit(10)
        .to_list(length=10)
    )

    if not transactions:
        text = "💰 <b>Transaction History</b>\n\nNo transactions yet."
    else:
        text = "💰 <b>Transaction History</b>\n\n"

        for tx in transactions:
            sign = "+" if tx.get("amount", 0) > 0 else ""
            text += (
                f"{tx.get('currency', '').upper()} "
                f"{sign}{tx.get('amount', 0)}\n"
                f"{safe_text(tx.get('type'))}\n"
                f"{tx.get('created_at')}\n\n"
            )

    await call.message.edit_text(
        text,
        reply_markup=back_menu(),
    )


# ============================================================
# UPI TOP UP
# ============================================================

@router.callback_query(F.data == "topup:upi")
async def topup_upi(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await state.set_state(UPIStates.amount)

    await call.message.edit_text(
        "Enter the amount you want to add in INR.",
        reply_markup=cancel_keyboard(),
    )


@router.message(UPIStates.amount)
async def upi_amount(message: Message, state: FSMContext):
    amount = parse_amount(message.text or "")

    if not amount:
        await message.answer(
            "Please enter a valid positive amount.",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.update_data(amount=amount)
    await state.set_state(UPIStates.proof)

    await message.answer(
        "<b>UPI Top Up</b>\n\n"
        f"Amount: ₹{money(amount)}\n"
        f"UPI ID: <code>{safe_text(UPI_ID)}</code>\n\n"
        "Complete the payment and send the payment screenshot.",
        reply_markup=cancel_keyboard(),
    )


@router.message(UPIStates.proof, F.photo)
async def upi_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    amount = float(data["amount"])

    proof_file_id = message.photo[-1].file_id

    payment_id = await next_payment_id()

    await db.payments.insert_one(
        {
            "payment_id": payment_id,
            "user_id": message.from_user.id,
            "method": "upi",
            "amount": amount,
            "currency": "INR",
            "proof_file_id": proof_file_id,
            "status": "pending",
            "created_at": now(),
        }
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Approve",
                    callback_data=f"payment:approve:{payment_id}",
                    style="success",
                ),
                InlineKeyboardButton(
                    text="❌ Reject",
                    callback_data=f"payment:reject:{payment_id}",
                    style="danger",
                ),
            ]
        ]
    )

    await notify_admins(
        "💳 <b>New UPI Top-Up</b>\n\n"
        f"User: {safe_text(username_text(message.from_user))}\n"
        f"User ID: <code>{message.from_user.id}</code>\n"
        f"Amount: ₹{money(amount)}\n"
        f"Payment ID: <code>{payment_id}</code>",
        reply_markup=keyboard,
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                proof_file_id,
                caption=(
                    f"UPI proof\n"
                    f"Payment: {payment_id}\n"
                    f"User: {message.from_user.id}\n"
                    f"Amount: ₹{money(amount)}"
                ),
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed sending UPI proof")

    await state.clear()

    await message.answer(
        "Payment proof submitted successfully ✅\n"
        "An administrator will verify it.",
        reply_markup=main_menu(message.from_user.id),
    )


@router.message(UPIStates.proof)
async def upi_proof_invalid(message: Message):
    await message.answer(
        "Please send the payment screenshot as a photo.",
        reply_markup=cancel_keyboard(),
    )


# ============================================================
# ADMIN PAYMENT APPROVAL
# ============================================================

@router.callback_query(F.data.startswith("payment:approve:"))
async def approve_payment(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    payment_id = call.data.split(":")[-1]

    payment = await db.payments.find_one_and_update(
        {
            "payment_id": payment_id,
            "status": "pending",
            "method": "upi",
        },
        {
            "$set": {
                "status": "approved",
                "approved_by": call.from_user.id,
                "approved_at": now(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not payment:
        await call.message.answer(
            "This payment is already processed or unavailable."
        )
        return

    user_id = payment["user_id"]
    amount = float(payment["amount"])

    user = await db.users.find_one_and_update(
        {"telegram_id": user_id},
        {
            "$inc": {"balances.inr": amount},
            "$set": {"updated_at": now()},
        },
        return_document=ReturnDocument.BEFORE,
    )

    if not user:
        await call.message.answer("User no longer exists.")
        return

    before = float(
        user.get("balances", {}).get("inr", 0)
    )
    after = before + amount

    await db.transactions.insert_one(
        {
            "user_id": user_id,
            "type": "upi_topup",
            "currency": "inr",
            "amount": amount,
            "balance_before": before,
            "balance_after": after,
            "reference": payment_id,
            "status": "completed",
            "created_at": now(),
        }
    )

    await bot.send_message(
        user_id,
        f"✅ UPI top-up approved.\n\n"
        f"Added: ₹{money(amount)}",
    )

    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("payment:reject:"))
async def reject_payment(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    payment_id = call.data.split(":")[-1]

    payment = await db.payments.find_one_and_update(
        {
            "payment_id": payment_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "rejected",
                "rejected_by": call.from_user.id,
                "rejected_at": now(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not payment:
        await call.message.answer(
            "This payment is already processed."
        )
        return

    await bot.send_message(
        payment["user_id"],
        "❌ Your UPI top-up proof was rejected.\n"
        "Please contact support if you believe this was an error.",
    )

    await call.message.edit_reply_markup(reply_markup=None)


# ============================================================
# OXAPAY
# ============================================================

async def create_oxapay_invoice(
    amount: float,
    order_id: str,
    description: str,
):
    url = f"{OXAPAY_API_BASE}/payment/invoice"

    payload = {
        "amount": amount,
        "currency": "USDT",
        "lifetime": 60,
        "callback_url": OXAPAY_WEBHOOK_URL,
        "order_id": order_id,
        "description": description,
        "sandbox": False,
    }

    headers = {
        "merchant_api_key": OXAPAY_MERCHANT_API_KEY,
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            json=payload,
            headers=headers,
        ) as response:
            data = await response.json(content_type=None)

            if response.status >= 400:
                raise RuntimeError(
                    f"OxaPay HTTP {response.status}: {data}"
                )

            return data


@router.callback_query(F.data == "topup:crypto")
async def topup_crypto(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await state.set_state(CryptoStates.amount)

    await call.message.edit_text(
        "Enter the USDT amount you want to add.",
        reply_markup=cancel_keyboard(),
    )


@router.message(CryptoStates.amount)
async def crypto_amount(message: Message, state: FSMContext):
    amount = parse_amount(message.text or "")

    if not amount:
        await message.answer(
            "Enter a valid positive amount.",
            reply_markup=cancel_keyboard(),
        )
        return

    payment_id = await next_payment_id()
    order_id = f"{payment_id}-{message.from_user.id}"

    try:
        result = await create_oxapay_invoice(
            amount=amount,
            order_id=order_id,
            description=f"USDT wallet top-up for {message.from_user.id}",
        )

        # OxaPay v1 response normally contains data fields.
        data = result.get("data", result)

        track_id = (
            data.get("track_id")
            or data.get("trackId")
        )

        payment_url = (
            data.get("payment_url")
            or data.get("paymentUrl")
            or data.get("pay_link")
            or data.get("payLink")
        )

        if not track_id or not payment_url:
            logger.error("Unexpected OxaPay response: %s", result)
            raise RuntimeError("Invalid OxaPay response")

        await db.payments.insert_one(
            {
                "payment_id": payment_id,
                "user_id": message.from_user.id,
                "method": "oxapay",
                "order_id": order_id,
                "track_id": str(track_id),
                "amount": amount,
                "currency": "USDT",
                "status": "pending",
                "created_at": now(),
            }
        )

        await state.clear()

        await message.answer(
            f"💳 <b>Crypto Payment Created</b>\n\n"
            f"Amount: {money(amount, 6)} USDT\n"
            f"Order: <code>{order_id}</code>\n\n"
            "Complete the payment using the button below.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="💳 Pay Now",
                            url=payment_url,
                            style="success",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="⬅️ Main Menu",
                            callback_data="menu:main",
                        )
                    ],
                ]
            ),
        )

    except Exception:
        logger.exception("OxaPay invoice creation failed")

        await message.answer(
            "Unable to create the crypto payment right now. "
            "Please try again later.",
            reply_markup=cancel_keyboard(),
        )


@app.post(
    "/webhook/oxapay",
    response_class=PlainTextResponse,
)
async def oxapay_webhook(request: Request):
    """
    OxaPay signs the raw POST body using HMAC-SHA512
    with the Merchant API Key.

    The HMAC header must be checked against the raw body,
    not reconstructed JSON.
    """

    raw_body = await request.body()
    signature = request.headers.get("HMAC")

    if not signature:
        raise HTTPException(
            status_code=400,
            detail="Missing HMAC",
        )

    calculated = hmac.new(
        OXAPAY_MERCHANT_API_KEY.encode(),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(
        calculated.lower(),
        signature.lower(),
    ):
        logger.warning("Invalid OxaPay HMAC")
        raise HTTPException(
            status_code=400,
            detail="Invalid HMAC",
        )

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON",
        )

    if data.get("type") != "invoice":
        return "ok"

    status = str(data.get("status", "")).lower()
    order_id = data.get("order_id")
    track_id = str(data.get("track_id", ""))

    if not order_id:
        return "ok"

    payment = await db.payments.find_one(
        {
            "order_id": order_id,
            "method": "oxapay",
        }
    )

    if not payment:
        logger.warning(
            "Unknown OxaPay order: %s",
            order_id,
        )
        return "ok"

    if track_id and payment.get("track_id") != track_id:
        logger.warning("OxaPay track ID mismatch")
        return "ok"

    if status != "paid":
        # Paying / other intermediate statuses are recorded,
        # but they do not credit the wallet.
        await db.payments.update_one(
            {
                "_id": payment["_id"],
                "status": {"$ne": "paid"},
            },
            {
                "$set": {
                    "gateway_status": data.get("status"),
                    "updated_at": now(),
                }
            },
        )
        return "ok"

    # Idempotency: only pending payments can be credited.
    updated = await db.payments.find_one_and_update(
        {
            "_id": payment["_id"],
            "status": {"$ne": "paid"},
        },
        {
            "$set": {
                "status": "paid",
                "gateway_status": data.get("status"),
                "paid_at": now(),
                "webhook_data": data,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        return "ok"

    amount = float(payment["amount"])
    user_id = int(payment["user_id"])

    user = await db.users.find_one_and_update(
        {"telegram_id": user_id},
        {
            "$inc": {"balances.usdt": amount},
            "$set": {"updated_at": now()},
        },
        return_document=ReturnDocument.BEFORE,
    )

    if not user:
        logger.error(
            "OxaPay payment user missing: %s",
            user_id,
        )
        return "ok"

    before = float(
        user.get("balances", {}).get("usdt", 0)
    )
    after = before + amount

    await db.transactions.insert_one(
        {
            "user_id": user_id,
            "type": "oxapay_topup",
            "currency": "usdt",
            "amount": amount,
            "balance_before": before,
            "balance_after": after,
            "reference": order_id,
            "status": "completed",
            "created_at": now(),
        }
    )

    try:
        await bot.send_message(
            user_id,
            "✅ Crypto payment confirmed.\n\n"
            f"Added: {money(amount, 6)} USDT",
        )
    except Exception:
        logger.exception("Unable to notify OxaPay user")

    await notify_admins(
        "💵 <b>OxaPay Payment Confirmed</b>\n\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Amount: {money(amount, 6)} USDT\n"
        f"Order: <code>{order_id}</code>"
    )

    return "ok"


# ============================================================
# STARS WALLET TOP-UP
# ============================================================

@router.callback_query(F.data == "topup:stars")
async def topup_stars(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await state.set_state(StarsTopUpStates.amount)

    await call.message.edit_text(
        "Enter the number of Telegram Stars you want to add.",
        reply_markup=cancel_keyboard(),
    )


@router.message(StarsTopUpStates.amount)
async def stars_topup_amount(message: Message, state: FSMContext):
    try:
        stars = int((message.text or "").strip())
        if stars <= 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "Enter a valid positive whole number of Stars.",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.clear()

    # The configured conversion determines INR credit.
    credited_inr = stars * STAR_TO_INR_RATE

    payload = (
        f"topup|stars|{message.from_user.id}|"
        f"{stars}|{credited_inr}"
    )

    try:
        await bot.send_invoice(
            chat_id=message.from_user.id,
            title="Wallet Top Up",
            description=(
                f"{stars} Telegram Stars wallet top-up"
            ),
            payload=payload,
            currency="XTR",
            prices=[
                LabeledPrice(
                    label="Wallet Top Up",
                    amount=stars,
                )
            ],
            provider_token="",
        )
    except Exception:
        logger.exception("Stars topup invoice failed")
        await message.answer(
            "Unable to create Stars invoice.",
            reply_markup=main_menu(message.from_user.id),
        )


# Extend successful payment handler to support wallet Stars.
# This second handler is intentionally separated by a helper.
@router.message(F.successful_payment)
async def successful_stars_wallet_handler(message: Message):
    payment = message.successful_payment

    if not payment or payment.currency != "XTR":
        return

    payload = payment.invoice_payload

    if not payload.startswith("topup|stars|"):
        return

    parts = payload.split("|")

    if len(parts) != 5:
        return

    try:
        user_id = int(parts[2])
        stars = int(parts[3])
        credited_inr = float(parts[4])
    except ValueError:
        return

    if user_id != message.from_user.id:
        return

    if payment.total_amount != stars:
        return

    existing = await db.payments.find_one(
        {
            "telegram_payment_charge_id":
                payment.telegram_payment_charge_id
        }
    )

    if existing:
        return

    payment_id = await next_payment_id()

    await db.payments.insert_one(
        {
            "payment_id": payment_id,
            "user_id": user_id,
            "method": "stars_topup",
            "amount": stars,
            "currency": "XTR",
            "credited_currency": "INR",
            "credited_amount": credited_inr,
            "status": "paid",
            "telegram_payment_charge_id":
                payment.telegram_payment_charge_id,
            "provider_payment_charge_id":
                payment.provider_payment_charge_id,
            "created_at": now(),
        }
    )

    user = await db.users.find_one_and_update(
        {"telegram_id": user_id},
        {
            "$inc": {
                "balances.inr": credited_inr,
                "balances.stars": stars,
            },
            "$set": {
                "updated_at": now(),
            },
        },
        return_document=ReturnDocument.BEFORE,
    )

    if not user:
        return

    before = float(
        user.get("balances", {}).get("inr", 0)
    )

    await db.transactions.insert_one(
        {
            "user_id": user_id,
            "type": "stars_topup",
            "currency": "inr",
            "amount": credited_inr,
            "balance_before": before,
            "balance_after": before + credited_inr,
            "reference": payment.telegram_payment_charge_id,
            "stars_amount": stars,
            "status": "completed",
            "created_at": now(),
        }
    )

    await message.answer(
        "✅ Stars top-up successful.\n\n"
        f"Stars received: {stars} ⭐\n"
        f"INR credited: ₹{money(credited_inr)}",
        reply_markup=main_menu(user_id),
    )


# ============================================================
# MY PURCHASES
# ============================================================

async def show_purchases(user_id: int, target: Message):
    purchases = await (
        db.purchases.find(
            {"user_id": user_id}
        )
        .sort("created_at", DESCENDING)
        .limit(20)
        .to_list(length=20)
    )

    if not purchases:
        await target.edit_text(
            "📦 You have no purchases yet.",
            reply_markup=back_menu(),
        )
        return

    rows = []

    for purchase in purchases:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📦 {purchase.get('pack_name', purchase['pack_id'])[:30]}",
                    callback_data=(
                        f"purchase:view:"
                        f"{purchase['purchase_id']}"
                    ),
                    style="primary",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="menu:main",
            )
        ]
    )

    text = "📦 <b>My Purchases</b>\n\n"

    for purchase in purchases:
        text += (
            f"📦 {safe_text(purchase.get('pack_name'))}\n"
            f"Purchased: {purchase.get('created_at')}\n"
            f"Payment: {safe_text(purchase.get('payment_method'))}\n\n"
        )

    await target.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )


@router.callback_query(F.data.startswith("purchase:view:"))
async def purchase_view(call: CallbackQuery):
    await call.answer()

    purchase_id = call.data.split(":")[-1]

    purchase = await db.purchases.find_one(
        {
            "purchase_id": purchase_id,
            "user_id": call.from_user.id,
        }
    )

    if not purchase:
        await call.message.answer("Purchase not found.")
        return

    await call.message.edit_text(
        "📦 <b>Purchase Details</b>\n\n"
        f"Pack: {safe_text(purchase.get('pack_name'))}\n"
        f"Purchase ID: <code>{purchase_id}</code>\n"
        f"Amount: {purchase.get('amount')} "
        f"{purchase.get('currency')}\n"
        f"Payment: {safe_text(purchase.get('payment_method'))}\n"
        f"Photos: {purchase.get('photos_count', 0)}\n"
        f"Videos: {purchase.get('videos_count', 0)}\n"
        f"Date: {purchase.get('created_at')}",
        reply_markup=back_menu(),
    )


# ============================================================
# APPOINTMENTS
# ============================================================

async def start_appointment(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    await state.set_state(AppointmentStates.name)

    await message.answer(
        "Please enter your name.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AppointmentStates.name)
async def appointment_name(
    message: Message,
    state: FSMContext,
):
    await state.update_data(name=(message.text or "")[:200])
    await state.set_state(AppointmentStates.date)

    await message.answer(
        "Choose your preferred date.\n"
        "Example: 25 September 2026",
        reply_markup=cancel_keyboard(),
    )


@router.message(AppointmentStates.date)
async def appointment_date(
    message: Message,
    state: FSMContext,
):
    await state.update_data(date=(message.text or "")[:100])
    await state.set_state(AppointmentStates.time)

    await message.answer(
        "Choose your preferred time.\n"
        "Example: 6:30 PM",
        reply_markup=cancel_keyboard(),
    )


@router.message(AppointmentStates.time)
async def appointment_time(
    message: Message,
    state: FSMContext,
):
    await state.update_data(time=(message.text or "")[:100])
    await state.set_state(AppointmentStates.description)

    await message.answer(
        "Describe what you need.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AppointmentStates.description)
async def appointment_description(
    message: Message,
    state: FSMContext,
):
    await state.update_data(
        description=(message.text or "")[:2000]
    )

    data = await state.get_data()

    await state.set_state(AppointmentStates.confirmation)

    await message.answer(
        "<b>Appointment Request</b>\n\n"
        f"Name: {safe_text(data['name'])}\n"
        f"Date: {safe_text(data['date'])}\n"
        f"Time: {safe_text(data['time'])}\n"
        f"Description: {safe_text(data['description'])}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Confirm",
                        callback_data="appointment:confirm",
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data="flow:cancel",
                        style="danger",
                    ),
                ]
            ]
        ),
    )


@router.callback_query(
    F.data == "appointment:confirm",
    AppointmentStates.confirmation,
)
async def appointment_confirm(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()

    data = await state.get_data()

    appointment_id = await db.counters.find_one_and_update(
        {"_id": "appointments"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    appointment_number = (
        f"APT-{int(appointment_id['value']):06d}"
    )

    await db.appointments.insert_one(
        {
            "appointment_id": appointment_number,
            "user_id": call.from_user.id,
            "username": call.from_user.username,
            "name": data["name"],
            "date": data["date"],
            "time": data["time"],
            "description": data["description"],
            "status": "pending",
            "created_at": now(),
        }
    )

    await notify_admins(
        "📅 <b>New Appointment Request</b>\n\n"
        f"ID: <code>{appointment_number}</code>\n"
        f"User: {safe_text(username_text(call.from_user))}\n"
        f"User ID: <code>{call.from_user.id}</code>\n"
        f"Date: {safe_text(data['date'])}\n"
        f"Time: {safe_text(data['time'])}\n\n"
        f"Description:\n{safe_text(data['description'])}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Accept",
                        callback_data=(
                            f"appointment:accept:"
                            f"{appointment_number}"
                        ),
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="❌ Reject",
                        callback_data=(
                            f"appointment:reject:"
                            f"{appointment_number}"
                        ),
                        style="danger",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Contact User",
                        callback_data=(
                            f"appointment:contact:"
                            f"{appointment_number}"
                        ),
                        style="primary",
                    )
                ],
            ]
        ),
    )

    await state.clear()

    await call.message.edit_text(
        "Appointment request submitted successfully ✅",
        reply_markup=main_menu(call.from_user.id),
    )


# ============================================================
# ADMIN APPOINTMENTS
# ============================================================

@router.callback_query(F.data.startswith("appointment:accept:"))
async def appointment_accept(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    appointment_id = call.data.split(":")[-1]

    appointment = await db.appointments.find_one_and_update(
        {
            "appointment_id": appointment_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "accepted",
                "handled_by": call.from_user.id,
                "handled_at": now(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if appointment:
        await bot.send_message(
            appointment["user_id"],
            "✅ Your appointment request has been accepted.",
        )

    await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("appointment:reject:"))
async def appointment_reject(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    appointment_id = call.data.split(":")[-1]

    appointment = await db.appointments.find_one_and_update(
        {
            "appointment_id": appointment_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "rejected",
                "handled_by": call.from_user.id,
                "handled_at": now(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if appointment:
        await bot.send_message(
            appointment["user_id"],
            "❌ Your appointment request was rejected.",
        )

    await call.message.edit_reply_markup(reply_markup=None)


# ============================================================
# SUPPORT
# ============================================================

async def start_support(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    await state.set_state(SupportStates.chatting)

    await message.answer(
        "💬 <b>Direct Talk</b>\n\n"
        "Send your message and it will be forwarded to the admin.\n"
        "Use /start to leave support mode.",
        reply_markup=cancel_keyboard(),
    )


@router.message(SupportStates.chatting)
async def support_message(
    message: Message,
    state: FSMContext,
):
    if not message.text:
        await message.answer(
            "Please send your support message as text."
        )
        return

    support_id = await db.counters.find_one_and_update(
        {"_id": "support"},
        {"$inc": {"value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    conversation_id = (
        f"SUP-{int(support_id['value']):08d}"
    )

    await db.support_messages.insert_one(
        {
            "conversation_id": conversation_id,
            "user_id": message.from_user.id,
            "sender": "user",
            "text": message.text[:4000],
            "created_at": now(),
        }
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↩️ Reply",
                    callback_data=(
                        f"support:reply:"
                        f"{message.from_user.id}"
                    ),
                    style="primary",
                )
            ]
        ]
    )

    await notify_admins(
        "💬 <b>New Support Message</b>\n\n"
        f"User: {safe_text(username_text(message.from_user))}\n"
        f"User ID: <code>{message.from_user.id}</code>\n\n"
        f"{safe_text(message.text)}",
        reply_markup=keyboard,
    )

    await message.answer(
        "Message sent to admin ✅\n"
        "You can send another message.",
    )


@router.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    user_id = int(call.data.split(":")[-1])

    await state.update_data(
        support_target_user_id=user_id
    )

    await state.set_state(SupportStates.chatting)

    await call.message.answer(
        f"Send your reply for user <code>{user_id}</code>."
    )


# ============================================================
# CANCEL
# ============================================================

@router.callback_query(F.data == "flow:cancel")
async def cancel_flow(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()

    await state.clear()

    await call.message.edit_text(
        "Operation cancelled.",
        reply_markup=main_menu(call.from_user.id),
    )


# ============================================================
# ADMIN PANEL
# ============================================================

@router.callback_query(F.data == "admin:panel")
async def admin_panel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    await call.message.edit_text(
        "<b>Admin Panel</b>",
        reply_markup=admin_panel_keyboard(),
    )


# ============================================================
# ADD PACK
# ============================================================

@router.callback_query(F.data == "admin:add_pack")
async def admin_add_pack(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    await state.clear()
    await state.set_state(AddPackStates.name)
    await state.update_data(media=[])

    await call.message.edit_text(
        "Enter pack name:",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.name)
async def add_pack_name(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    name = (message.text or "").strip()

    if not name:
        await message.answer("Enter a valid pack name.")
        return

    await state.update_data(name=name[:100])
    await state.set_state(AddPackStates.description)

    await message.answer(
        "Enter pack description:",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.description)
async def add_pack_description(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    await state.update_data(
        description=(message.text or "")[:2000]
    )

    await state.set_state(AddPackStates.media)

    await message.answer(
        "How would you like to add media?",
        reply_markup=media_keyboard(),
    )


# ============================================================
# MEDIA ADMIN FLOW
# ============================================================

@router.callback_query(
    F.data == "packmedia:photo",
    AddPackStates.media,
)
async def pack_add_photo(call: CallbackQuery):
    await call.answer()

    await call.message.edit_text(
        "Send a photo now.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(
    F.data == "packmedia:video",
    AddPackStates.media,
)
async def pack_add_video(call: CallbackQuery):
    await call.answer()

    await call.message.edit_text(
        "Send a video now.",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(
    F.data == "packmedia:channel",
    AddPackStates.media,
)
async def pack_add_channel(call: CallbackQuery):
    await call.answer()

    await call.message.edit_text(
        "Send an authorized channel post link.\n\n"
        "Example:\n"
        "<code>https://t.me/c/1234567890/123</code>",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.media, F.photo)
async def add_pack_photo(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    media = data.get("media", [])

    photo = message.photo[-1]

    media.append(
        {
            "type": "photo",
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
            "source": "upload",
            "created_at": now(),
        }
    )

    await state.update_data(media=media)

    photos = sum(
        1 for item in media
        if item["type"] == "photo"
    )
    videos = sum(
        1 for item in media
        if item["type"] == "video"
    )

    await message.answer(
        "Photo added successfully ✅\n\n"
        f"📸 Photos: {photos}\n"
        f"🎥 Videos: {videos}",
        reply_markup=media_keyboard(),
    )


@router.message(AddPackStates.media, F.video)
async def add_pack_video(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    media = data.get("media", [])

    video = message.video

    media.append(
        {
            "type": "video",
            "file_id": video.file_id,
            "file_unique_id": video.file_unique_id,
            "source": "upload",
            "created_at": now(),
        }
    )

    await state.update_data(media=media)

    photos = sum(
        1 for item in media
        if item["type"] == "photo"
    )
    videos = sum(
        1 for item in media
        if item["type"] == "video"
    )

    await message.answer(
        "Video added successfully ✅\n\n"
        f"📸 Photos: {photos}\n"
        f"🎥 Videos: {videos}",
        reply_markup=media_keyboard(),
    )


# ============================================================
# CHANNEL LINK PARSER
# ============================================================

def parse_channel_post_link(link: str):
    """
    Supports:
      https://t.me/c/1234567890/123
      https://t.me/c/1234567890/123?...
    """

    link = link.strip()

    match = re.match(
        r"^https?://t\.me/c/(\d+)/(\d+)(?:\?.*)?$",
        link,
    )

    if not match:
        return None

    internal_id = int(match.group(1))
    message_id = int(match.group(2))

    # Telegram's /c/ internal identifier maps to a supergroup/channel
    # by prefixing -100.
    channel_id = int(f"-100{internal_id}")

    return channel_id, message_id


@router.message(AddPackStates.media, F.text)
async def add_pack_channel_link(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    parsed = parse_channel_post_link(message.text or "")

    if not parsed:
        await message.answer(
            "Invalid channel post link.\n\n"
            "Use a link such as:\n"
            "<code>https://t.me/c/1234567890/123</code>",
            reply_markup=media_keyboard(),
        )
        return

    source_channel_id, message_id = parsed

    if source_channel_id != CHANNEL_ID:
        await message.answer(
            "Rejected ❌\n\n"
            "This post is not from the configured authorized "
            "source channel.",
            reply_markup=media_keyboard(),
        )
        return

    # Check bot access to the authorized channel.
    try:
        member = await bot.get_chat_member(
            CHANNEL_ID,
            bot.id,
        )

        if member.status not in {
            "administrator",
            "creator",
            "member",
        }:
            await message.answer(
                "The bot does not have sufficient access "
                "to the configured source channel.",
                reply_markup=media_keyboard(),
            )
            return

    except Exception:
        logger.exception("Channel access check failed")

        await message.answer(
            "Unable to verify access to the configured channel.",
            reply_markup=media_keyboard(),
        )
        return

    # Telegram Bot API does not provide a generic "fetch arbitrary
    # historical message by ID" method for bots.
    #
    # We therefore attempt copy_message from the authorized channel.
    # This does not require downloading/re-uploading the media.
    try:
        copied = await bot.copy_message(
            chat_id=message.from_user.id,
            from_chat_id=CHANNEL_ID,
            message_id=message_id,
        )

        data = await state.get_data()
        media = data.get("media", [])

        if copied.photo:
            photo = copied.photo[-1]

            media.append(
                {
                    "type": "photo",
                    "file_id": photo.file_id,
                    "file_unique_id": photo.file_unique_id,
                    "source": "channel",
                    "source_channel_id": CHANNEL_ID,
                    "source_message_id": message_id,
                    "created_at": now(),
                }
            )

        elif copied.video:
            video = copied.video

            media.append(
                {
                    "type": "video",
                    "file_id": video.file_id,
                    "file_unique_id": video.file_unique_id,
                    "source": "channel",
                    "source_channel_id": CHANNEL_ID,
                    "source_message_id": message_id,
                    "created_at": now(),
                }
            )

        else:
            await message.answer(
                "The channel post does not contain supported "
                "photo/video media.",
                reply_markup=media_keyboard(),
            )
            return

        await state.update_data(media=media)

        photos, videos = (
            sum(1 for x in media if x["type"] == "photo"),
            sum(1 for x in media if x["type"] == "video"),
        )

        await message.answer(
            "Media added to this pack ✅\n\n"
            f"📸 Photos: {photos}\n"
            f"🎥 Videos: {videos}\n\n"
            "Send another authorized channel post link or "
            "choose an option.",
            reply_markup=media_keyboard(),
        )

    except TelegramBadRequest:
        logger.exception("Channel message copy failed")

        await message.answer(
            "Telegram could not access/copy that channel post.\n\n"
            "Make sure the bot can access the configured channel "
            "and the post is available to it.",
            reply_markup=media_keyboard(),
        )


@router.callback_query(
    F.data == "packmedia:finish",
    AddPackStates.media,
)
async def finish_media(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    data = await state.get_data()
    media = data.get("media", [])

    if not media:
        await call.message.answer(
            "Add at least one photo or video before finishing."
        )
        return

    await state.set_state(AddPackStates.price_inr)
    await state.update_data(
        price_inr=0,
        price_usd=0,
        price_usdt=0,
        price_stars=0,
    )

    await call.message.edit_text(
        "Set pack prices.\n\n"
        "Choose a currency:",
        reply_markup=price_keyboard(),
    )


# ============================================================
# PACK PRICES
# ============================================================

@router.callback_query(
    F.data == "packprice:inr",
    AddPackStates.price_inr,
)
async def price_inr(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await state.set_state(AddPackStates.price_inr)

    await call.message.edit_text(
        "Enter INR price.\n"
        "Example: 499",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.price_inr)
async def price_inr_value(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    amount = parse_amount(message.text or "")

    if amount is None:
        await message.answer(
            "Enter a valid positive INR price."
        )
        return

    await state.update_data(price_inr=amount)

    await state.set_state(AddPackStates.price_usd)

    await message.answer(
        "Choose the next price field:",
        reply_markup=price_keyboard(),
    )


@router.callback_query(
    F.data == "packprice:usd",
    AddPackStates.price_usd,
)
async def price_usd(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await call.message.edit_text(
        "Enter USD price.\n"
        "Example: 5.99",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.price_usd)
async def price_usd_value(
    message: Message,
    state: FSMContext,
):
    amount = parse_amount(message.text or "")

    if amount is None:
        await message.answer(
            "Enter a valid positive USD price."
        )
        return

    await state.update_data(price_usd=amount)

    await state.set_state(AddPackStates.price_usdt)

    await message.answer(
        "Choose the next price field:",
        reply_markup=price_keyboard(),
    )


@router.callback_query(
    F.data == "packprice:usdt",
    AddPackStates.price_usdt,
)
async def price_usdt(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await call.message.edit_text(
        "Enter USDT price.\n"
        "Example: 5.50",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.price_usdt)
async def price_usdt_value(
    message: Message,
    state: FSMContext,
):
    amount = parse_amount(message.text or "")

    if amount is None:
        await message.answer(
            "Enter a valid positive USDT price."
        )
        return

    await state.update_data(price_usdt=amount)

    await state.set_state(AddPackStates.price_stars)

    await message.answer(
        "Choose the next price field:",
        reply_markup=price_keyboard(),
    )


@router.callback_query(
    F.data == "packprice:stars",
    AddPackStates.price_stars,
)
async def price_stars(call: CallbackQuery, state: FSMContext):
    await call.answer()

    await call.message.edit_text(
        "Enter Telegram Stars price.\n"
        "Example: 350",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddPackStates.price_stars)
async def price_stars_value(
    message: Message,
    state: FSMContext,
):
    try:
        amount = int((message.text or "").strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "Enter a valid whole number of Stars."
        )
        return

    await state.update_data(price_stars=amount)
    await show_pack_preview(message, state)


@router.callback_query(
    F.data == "packprice:skip",
)
async def price_skip(call: CallbackQuery, state: FSMContext):
    current = await state.get_state()

    if current not in {
        AddPackStates.price_inr.state,
        AddPackStates.price_usd.state,
        AddPackStates.price_usdt.state,
        AddPackStates.price_stars.state,
    }:
        return

    await call.answer()

    data = await state.get_data()

    current_state = current

    if current_state == AddPackStates.price_inr.state:
        await state.set_state(AddPackStates.price_usd)
    elif current_state == AddPackStates.price_usd.state:
        await state.set_state(AddPackStates.price_usdt)
    elif current_state == AddPackStates.price_usdt.state:
        await state.set_state(AddPackStates.price_stars)
    else:
        await show_pack_preview(call.message, state)
        return

    await call.message.edit_text(
        "Choose the next price field:",
        reply_markup=price_keyboard(),
    )


async def show_pack_preview(
    message: Message,
    state: FSMContext,
):
    data = await state.get_data()

    media = data.get("media", [])

    photos = sum(
        1 for item in media
        if item["type"] == "photo"
    )

    videos = sum(
        1 for item in media
        if item["type"] == "video"
    )

    await state.set_state(AddPackStates.preview)

    await message.answer(
        "<b>PACK PREVIEW</b>\n\n"
        f"Name: {safe_text(data.get('name'))}\n\n"
        f"Description:\n{safe_text(data.get('description'))}\n\n"
        f"Photos: {photos}\n"
        f"Videos: {videos}\n\n"
        "<b>Prices</b>\n"
        f"INR: ₹{money(data.get('price_inr', 0))}\n"
        f"USD: ${money(data.get('price_usd', 0))}\n"
        f"USDT: {money(data.get('price_usdt', 0), 6)}\n"
        f"Stars: {int(data.get('price_stars', 0))}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💾 Save Pack",
                        callback_data="pack:save",
                        style="success",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Edit",
                        callback_data="pack:edit",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data="flow:cancel",
                        style="danger",
                    ),
                ],
            ]
        ),
    )


@router.callback_query(
    F.data == "pack:save",
    AddPackStates.preview,
)
async def save_pack(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    data = await state.get_data()

    pack_id = await next_pack_id()

    document = {
        "pack_id": pack_id,
        "name": data["name"],
        "description": data["description"],
        "media": data.get("media", []),
        "price_inr": float(data.get("price_inr", 0)),
        "price_usd": float(data.get("price_usd", 0)),
        "price_usdt": float(data.get("price_usdt", 0)),
        "price_stars": int(data.get("price_stars", 0)),
        "active": True,
        "created_by": call.from_user.id,
        "created_at": now(),
        "updated_at": now(),
    }

    await db.packs.insert_one(document)
    await state.clear()

    await call.message.edit_text(
        "Pack created successfully ✅\n\n"
        f"Pack ID: <code>{pack_id}</code>",
        reply_markup=admin_panel_keyboard(),
    )


@router.callback_query(
    F.data == "pack:edit",
    AddPackStates.preview,
)
async def pack_edit(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()

    await state.set_state(AddPackStates.name)

    await call.message.edit_text(
        "Enter the pack name again to edit it.",
        reply_markup=cancel_keyboard(),
    )


# ============================================================
# ADMIN PACK MANAGEMENT
# ============================================================

@router.callback_query(F.data == "admin:packs")
async def admin_packs(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()
    await show_admin_packs(call.message, 1)


async def show_admin_packs(message: Message, page: int):
    size = 5
    skip = (page - 1) * size

    total = await db.packs.count_documents({})

    packs = await (
        db.packs.find({})
        .sort("created_at", DESCENDING)
        .skip(skip)
        .limit(size)
        .to_list(length=size)
    )

    pages = max(1, (total + size - 1) // size)

    if not packs:
        await message.edit_text(
            "No packs found.",
            reply_markup=admin_panel_keyboard(),
        )
        return

    rows = []

    text = "<b>Manage Packs</b>\n\n"

    for pack in packs:
        status = "Active" if pack.get("active") else "Disabled"

        text += (
            f"📦 <b>{safe_text(pack['name'])}</b>\n"
            f"ID: <code>{pack['pack_id']}</code>\n"
            f"Status: {status}\n\n"
        )

        rows.append(
            [
                InlineKeyboardButton(
                    text="👁️ View",
                    callback_data=(
                        f"adminpack:view:{pack['pack_id']}"
                    ),
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🔄 Toggle",
                    callback_data=(
                        f"adminpack:toggle:{pack['pack_id']}"
                    ),
                    style="primary",
                ),
                InlineKeyboardButton(
                    text="🗑️ Delete",
                    callback_data=(
                        f"adminpack:delete:{pack['pack_id']}"
                    ),
                    style="danger",
                ),
            ]
        )

    nav = []

    if page > 1:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Previous",
                callback_data=f"adminpacks:page:{page - 1}",
            )
        )

    nav.append(
        InlineKeyboardButton(
            text=f"📄 Page {page}/{pages}",
            callback_data="noop",
        )
    )

    if page < pages:
        nav.append(
            InlineKeyboardButton(
                text="Next ➡️",
                callback_data=f"adminpacks:page:{page + 1}",
            )
        )

    rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Back",
                callback_data="admin:panel",
            )
        ]
    )

    await message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=rows
        ),
    )


@router.callback_query(F.data.startswith("adminpacks:page:"))
async def admin_packs_page(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    page = int(call.data.split(":")[-1])
    await show_admin_packs(call.message, page)


@router.callback_query(F.data.startswith("adminpack:view:"))
async def admin_pack_view(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    pack_id = call.data.split(":")[-1]

    pack = await db.packs.find_one(
        {"pack_id": pack_id}
    )

    if not pack:
        await call.message.answer("Pack not found.")
        return

    photos, videos = pack_media_counts(pack)

    await call.message.edit_text(
        f"<b>{safe_text(pack['name'])}</b>\n\n"
        f"ID: <code>{pack['pack_id']}</code>\n"
        f"Description: {safe_text(pack['description'])}\n\n"
        f"Photos: {photos}\n"
        f"Videos: {videos}\n"
        f"INR: ₹{money(pack.get('price_inr'))}\n"
        f"USD: ${money(pack.get('price_usd'))}\n"
        f"USDT: {money(pack.get('price_usdt'), 6)}\n"
        f"Stars: {pack.get('price_stars', 0)}\n"
        f"Active: {pack.get('active', False)}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="admin:packs",
                    )
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("adminpack:toggle:"))
async def admin_pack_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    pack_id = call.data.split(":")[-1]

    pack = await db.packs.find_one(
        {"pack_id": pack_id}
    )

    if not pack:
        return

    await db.packs.update_one(
        {"pack_id": pack_id},
        {
            "$set": {
                "active": not pack.get("active", False),
                "updated_at": now(),
            }
        },
    )

    await show_admin_packs(call.message, 1)


@router.callback_query(F.data.startswith("adminpack:delete:"))
async def admin_pack_delete_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    pack_id = call.data.split(":")[-1]

    await call.message.edit_text(
        f"Delete <code>{pack_id}</code> permanently?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑️ Confirm Delete",
                        callback_data=(
                            f"adminpack:deleteconfirm:{pack_id}"
                        ),
                        style="danger",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="admin:packs",
                    )
                ],
            ]
        ),
    )


@router.callback_query(
    F.data.startswith("adminpack:deleteconfirm:")
)
async def admin_pack_delete(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    pack_id = call.data.split(":")[-1]

    await db.packs.delete_one(
        {"pack_id": pack_id}
    )

    await call.message.edit_text(
        "Pack deleted successfully.",
        reply_markup=admin_panel_keyboard(),
    )


# ============================================================
# ADMIN USERS
# ============================================================

@router.callback_query(F.data == "admin:users")
async def admin_users(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    users = await (
        db.users.find({})
        .sort("created_at", DESCENDING)
        .limit(20)
        .to_list(length=20)
    )

    text = "<b>Users</b>\n\n"

    for user in users:
        balances = user.get("balances", {})

        text += (
            f"ID: <code>{user['telegram_id']}</code>\n"
            f"Username: {safe_text(user.get('username') or '-')} \n"
            f"INR: ₹{money(balances.get('inr', 0))}\n"
            f"USDT: {money(balances.get('usdt', 0), 6)}\n"
            f"Blocked: {user.get('blocked', False)}\n\n"
        )

    await call.message.edit_text(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚖️ Add / Remove Balance",
                        callback_data="admin:userbalance",
                        style="primary",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="admin:panel",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data == "admin:userbalance")
async def admin_user_balance(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    await state.set_state(BalanceAdminStates.user_id)

    await call.message.edit_text(
        "Enter the user's Telegram ID.",
        reply_markup=cancel_keyboard(),
    )


@router.message(BalanceAdminStates.user_id)
async def admin_balance_user(
    message: Message,
    state: FSMContext,
):
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Enter a valid Telegram ID.")
        return

    user = await get_user(user_id)

    if not user:
        await message.answer("User not found.")
        return

    await state.update_data(target_user_id=user_id)
    await state.set_state(BalanceAdminStates.currency)

    await message.answer(
        "Choose currency.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🇮🇳 INR",
                        callback_data="balancecur:inr",
                        style="primary",
                    ),
                    InlineKeyboardButton(
                        text="💵 USDT",
                        callback_data="balancecur:usdt",
                        style="primary",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="admin:panel",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancel",
                        callback_data="flow:cancel",
                        style="danger",
                    ),
                ],
            ]
        ),
    )


@router.callback_query(
    F.data.startswith("balancecur:"),
    BalanceAdminStates.currency,
)
async def admin_balance_currency(
    call: CallbackQuery,
    state: FSMContext,
):
    await call.answer()

    currency = call.data.split(":")[-1]

    await state.update_data(currency=currency)
    await state.set_state(BalanceAdminStates.amount)

    await call.message.edit_text(
        f"Enter amount to add/remove in {currency.upper()}.\n\n"
        "Use a positive number to add.\n"
        "Use a negative number to remove.",
        reply_markup=cancel_keyboard(),
    )


@router.message(BalanceAdminStates.amount)
async def admin_balance_amount(
    message: Message,
    state: FSMContext,
):
    try:
        amount = float((message.text or "").strip())
    except ValueError:
        await message.answer("Enter a valid amount.")
        return

    data = await state.get_data()

    user_id = int(data["target_user_id"])
    currency = data["currency"]

    field = f"balances.{currency}"

    user = await db.users.find_one_and_update(
        {"telegram_id": user_id},
        {
            "$inc": {field: amount},
            "$set": {"updated_at": now()},
        },
        return_document=ReturnDocument.BEFORE,
    )

    if not user:
        await message.answer("User not found.")
        await state.clear()
        return

    before = float(
        user.get("balances", {}).get(currency, 0)
    )
    after = before + amount

    if after < 0:
        # Reverse the operation.
        await db.users.update_one(
            {"telegram_id": user_id},
            {"$inc": {field: -amount}},
        )

        await message.answer(
            "Operation rejected because the balance cannot become negative."
        )
        return

    await db.transactions.insert_one(
        {
            "user_id": user_id,
            "type": "admin_balance_adjustment",
            "currency": currency,
            "amount": amount,
            "balance_before": before,
            "balance_after": after,
            "reference": f"ADMIN:{message.from_user.id}",
            "status": "completed",
            "created_at": now(),
        }
    )

    await bot.send_message(
        user_id,
        "💰 Your wallet balance was updated by an administrator.\n\n"
        f"{currency.upper()}: {amount:+.6f}",
    )

    await state.clear()

    await message.answer(
        "Balance updated successfully.",
        reply_markup=admin_panel_keyboard(),
    )


# ============================================================
# ADMIN PAYMENT VERIFICATION LIST
# ============================================================

@router.callback_query(F.data == "admin:payments")
async def admin_payments(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    payments = await (
        db.payments.find(
            {
                "status": "pending",
                "method": "upi",
            }
        )
        .sort("created_at", DESCENDING)
        .limit(20)
        .to_list(length=20)
    )

    if not payments:
        await call.message.edit_text(
            "No pending manual payments.",
            reply_markup=admin_panel_keyboard(),
        )
        return

    text = "<b>Pending UPI Payments</b>\n\n"

    for payment in payments:
        text += (
            f"Payment: <code>{payment['payment_id']}</code>\n"
            f"User: <code>{payment['user_id']}</code>\n"
            f"Amount: ₹{money(payment['amount'])}\n\n"
        )

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="admin:panel",
                    )
                ]
            ]
        ),
    )


# ============================================================
# ADMIN APPOINTMENTS LIST
# ============================================================

@router.callback_query(F.data == "admin:appointments")
async def admin_appointments(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    appointments = await (
        db.appointments.find({})
        .sort("created_at", DESCENDING)
        .limit(20)
        .to_list(length=20)
    )

    text = "<b>Appointments</b>\n\n"

    if not appointments:
        text += "No appointments."

    for appointment in appointments:
        text += (
            f"<b>{appointment['appointment_id']}</b>\n"
            f"User: {appointment['user_id']}\n"
            f"Date: {safe_text(appointment['date'])}\n"
            f"Time: {safe_text(appointment['time'])}\n"
            f"Status: {appointment['status']}\n\n"
        )

    await call.message.edit_text(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Back",
                        callback_data="admin:panel",
                    )
                ]
            ]
        ),
    )


# ============================================================
# ADMIN STATISTICS
# ============================================================

@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    users = await db.users.count_documents({})
    packs = await db.packs.count_documents(
        {"active": True}
    )
    purchases = await db.purchases.count_documents({})
    pending = await db.payments.count_documents(
        {"status": "pending"}
    )

    await call.message.edit_text(
        "<b>Statistics</b>\n\n"
        f"Users: {users}\n"
        f"Active Packs: {packs}\n"
        f"Purchases: {purchases}\n"
        f"Pending Payments: {pending}",
        reply_markup=admin_panel_keyboard(),
    )


# ============================================================
# ADMIN BROADCAST
# ============================================================

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(
    call: CallbackQuery,
    state: FSMContext,
):
    if not is_admin(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    await state.set_state(BroadcastStates.message)

    await call.message.edit_text(
        "Send the broadcast message.",
        reply_markup=cancel_keyboard(),
    )


@router.message(BroadcastStates.message)
async def admin_broadcast_message(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    if not message.text:
        await message.answer(
            "For this simple broadcast flow, send text."
        )
        return

    users = db.users.find(
        {
            "blocked": {"$ne": True}
        },
        {
            "telegram_id": 1
        },
    )

    sent = 0

    async for user in users:
        try:
            await bot.send_message(
                user["telegram_id"],
                message.text,
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await state.clear()

    await message.answer(
        f"Broadcast completed.\nSent: {sent}",
        reply_markup=admin_panel_keyboard(),
    )


# ============================================================
# ADMIN MEDIA FALLBACK
# ============================================================

@router.message(
    F.photo | F.video,
)
async def unexpected_media(message: Message):
    """
    If an admin sends media while not in the Add Pack FSM,
    explain how to use it rather than silently discarding it.
    """
    if is_admin(message.from_user.id):
        await message.answer(
            "Media received. To add it to a pack, open "
            "Admin Panel → Add Pack."
        )


# ============================================================
# ERROR HANDLER
# ============================================================

@router.errors()
async def global_error_handler(event):
    logger.exception(
        "Unhandled Telegram error: %s",
        event.exception,
    )

    return True


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    global mongo_client, db

    mongo_client = AsyncIOMotorClient(
        MONGO_URI,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
    )

    db = mongo_client[DATABASE_NAME]

    await db.command("ping")

    # Required indexes.
    await db.users.create_index(
        [("telegram_id", ASCENDING)],
        unique=True,
    )

    await db.packs.create_index(
        [("pack_id", ASCENDING)],
        unique=True,
    )

    await db.packs.create_index(
        [("active", ASCENDING)]
    )

    await db.purchases.create_index(
        [("user_id", ASCENDING)]
    )

    await db.purchases.create_index(
        [("reference", ASCENDING)],
        unique=True,
    )

    await db.payments.create_index(
        [("user_id", ASCENDING)]
    )

    await db.payments.create_index(
        [("status", ASCENDING)]
    )

    await db.payments.create_index(
        [("order_id", ASCENDING)],
        unique=True,
        sparse=True,
    )

    await db.payments.create_index(
        [("telegram_payment_charge_id", ASCENDING)],
        unique=True,
        sparse=True,
    )

    await db.payments.create_index(
        [("track_id", ASCENDING)]
    )

    await db.appointments.create_index(
        [("user_id", ASCENDING)]
    )

    await db.transactions.create_index(
        [("user_id", ASCENDING)]
    )

    await db.support_messages.create_index(
        [("user_id", ASCENDING)]
    )

    logger.info("MongoDB initialized successfully")


# ============================================================
# HEALTH ENDPOINT
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "telegram-pack-bot",
    }


@app.get("/health")
async def health():
    try:
        await db.command("ping")
        return {
            "status": "healthy",
            "database": "connected",
        }
    except Exception:
        return {
            "status": "unhealthy",
            "database": "disconnected",
        }


# ============================================================
# FASTAPI + AIROGRAM RUNNER
# ============================================================

async def run_http_server():
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(config)

    await server.serve()


async def run_bot():
    logger.info("Starting Telegram polling...")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


async def main():
    await init_db()

    try:
        await asyncio.gather(
            run_bot(),
            run_http_server(),
        )
    finally:
        await bot.session.close()

        if mongo_client:
            mongo_client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application stopped.")
