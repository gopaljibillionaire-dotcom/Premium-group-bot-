import asyncio
import datetime
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
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

from config import BOT_TOKEN, ADMIN_IDS, CHANNEL_ID, DB_FILE, CHECK_INTERVAL, WALLETS, logger

# =============================================================================
# 1. FSM STATES
# =============================================================================

class AdminPlanStates(StatesGroup):
    waiting_name = State()
    waiting_price = State()
    waiting_currency = State()
    waiting_duration_val = State()
    waiting_duration_unit = State()
    waiting_description = State()
    confirmation = State()

class AdminBulkInviteStates(StatesGroup):
    waiting_links = State()

class UserPaymentProofStates(StatesGroup):
    waiting_proof = State()

class BroadcastStates(StatesGroup):
    waiting_message = State()
    confirmation = State()

class AdminUserSearchStates(StatesGroup):
    waiting_query = State()

# =============================================================================
# 2. DATABASE MANAGER
# =============================================================================

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            
            await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                is_blocked INTEGER DEFAULT 0
            );
            """)

            await db.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                duration_value INTEGER NOT NULL,
                duration_unit TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                description TEXT,
                active INTEGER DEFAULT 1,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            """)

            await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (plan_id) REFERENCES plans (id) ON DELETE CASCADE
            );
            """)

            await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                provider_payment_id TEXT UNIQUE NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                paid_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (plan_id) REFERENCES plans (id) ON DELETE CASCADE
            );
            """)

            await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_links_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_link TEXT UNIQUE NOT NULL,
                is_used INTEGER DEFAULT 0,
                created_at TIMESTAMP NOT NULL
            );
            """)

            await db.commit()

    async def upsert_user(self, telegram_id: int, username: Optional[str], first_name: str) -> None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            INSERT INTO users (telegram_id, username, first_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """, (telegram_id, username, first_name, now, now))
            await db.commit()

    async def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def add_bulk_invites(self, links: List[str]) -> int:
        now = datetime.now(timezone.utc)
        added = 0
        async with aiosqlite.connect(self.db_path) as db:
            for link in links:
                try:
                    await db.execute(
                        "INSERT INTO invite_links_pool (invite_link, is_used, created_at) VALUES (?, 0, ?)",
                        (link.strip(), now)
                    )
                    added += 1
                except Exception:
                    continue
            await db.commit()
        return added

    async def get_available_invite(self) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM invite_links_pool WHERE is_used = 0 LIMIT 1") as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def mark_invite_used(self, link_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM invite_links_pool WHERE id = ?", (link_id,))
            await db.commit()

    async def get_all_user_ids(self) -> List[int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT telegram_id FROM users WHERE is_blocked = 0") as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    async def create_plan(self, name: str, price: float, currency: str,
                          duration_val: int, duration_unit: str, duration_sec: int,
                          description: str) -> int:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
            INSERT INTO plans (name, price, currency, duration_value, duration_unit, duration_seconds, description, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (name, price, currency.upper(), duration_val, duration_unit, duration_sec, description, now, now))
            await db.commit()
            return cursor.lastrowid

    async def get_active_plans(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM plans WHERE active = 1 ORDER BY price ASC") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_all_plans(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM plans ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_plan(self, plan_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def toggle_plan_status(self, plan_id: int) -> None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
            UPDATE plans SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END, updated_at = ?
            WHERE id = ?
            """, (now, plan_id))
            await db.commit()

    async def delete_plan(self, plan_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
            await db.commit()

    async def get_active_subscription(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
            SELECT s.*, p.name as plan_name 
            FROM subscriptions s
            JOIN plans p ON s.plan_id = p.id
            WHERE s.user_id = ? AND s.status = 'active' AND s.expires_at > ?
            ORDER BY s.expires_at DESC LIMIT 1
            """, (telegram_id, now)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def add_or_extend_subscription(self, telegram_id: int, plan_id: int, duration_seconds: int) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        active_sub = await self.get_active_subscription(telegram_id)

        if active_sub:
            curr_expires = active_sub['expires_at']
            if isinstance(curr_expires, str):
                curr_expires = datetime.fromisoformat(curr_expires)
            if curr_expires.tzinfo is None:
                curr_expires = curr_expires.replace(tzinfo=timezone.utc)
            
            start_time = active_sub['started_at']
            expires_time = curr_expires + timedelta(seconds=duration_seconds)
            
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("UPDATE subscriptions SET status = 'extended', updated_at = ? WHERE id = ?", (now, active_sub['id']))
                await db.commit()
        else:
            start_time = now
            expires_time = now + timedelta(seconds=duration_seconds)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
            INSERT INTO subscriptions (user_id, plan_id, started_at, expires_at, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """, (telegram_id, plan_id, start_time, expires_time, now, now))
            sub_id = cursor.lastrowid
            await db.commit()
            
            async with db.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)) as c:
                row = await c.fetchone()
                return dict(row)

    async def get_expired_subscriptions(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
            SELECT s.*, u.telegram_id 
            FROM subscriptions s
            JOIN users u ON s.user_id = u.telegram_id
            WHERE s.status = 'active' AND s.expires_at <= ?
            """, (now,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def mark_subscription_expired(self, sub_id: int) -> None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE subscriptions SET status = 'expired', updated_at = ? WHERE id = ?", (now, sub_id))
            await db.commit()

    async def revoke_user_subscriptions(self, telegram_id: int) -> None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE subscriptions SET status = 'revoked', updated_at = ? WHERE user_id = ? AND status = 'active'", (now, telegram_id))
            await db.commit()

    async def create_payment_record(self, user_id: int, plan_id: int, provider_payment_id: str, amount: float, currency: str) -> int:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
            INSERT INTO payments (user_id, plan_id, provider_payment_id, amount, currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (user_id, plan_id, provider_payment_id, amount, currency.upper(), now))
            await db.commit()
            return cursor.lastrowid

    async def get_payment_by_provider_id(self, provider_payment_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM payments WHERE provider_payment_id = ?", (provider_payment_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update_payment_status(self, provider_payment_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            if status == 'paid':
                await db.execute("UPDATE payments SET status = ?, paid_at = ? WHERE provider_payment_id = ?", (status, now, provider_payment_id))
            else:
                await db.execute("UPDATE payments SET status = ? WHERE provider_payment_id = ?", (status, provider_payment_id))
            await db.commit()

    async def get_user_payments(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
            SELECT p.*, pl.name as plan_name 
            FROM payments p
            LEFT JOIN plans pl ON p.plan_id = pl.id
            WHERE p.user_id = ? 
            ORDER BY p.created_at DESC LIMIT ?
            """, (user_id, limit)) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    async def get_stats(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as c:
                total_users = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'active' AND expires_at > ?", (now,)) as c:
                active_subs = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'expired' OR expires_at <= ?", (now,)) as c:
                expired_subs = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid'") as c:
                row = await c.fetchone()
                total_payments, total_revenue = row[0], row[1]

            async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid' AND paid_at >= ?", (today_start,)) as c:
                row = await c.fetchone()
                today_payments, today_revenue = row[0], row[1]

            async with db.execute("SELECT COUNT(*) FROM plans WHERE active = 1") as c:
                total_plans = (await c.fetchone())[0]

            async with db.execute("SELECT COUNT(*) FROM invite_links_pool WHERE is_used = 0") as c:
                pool_links = (await c.fetchone())[0]

            return {
                "total_users": total_users,
                "active_subs": active_subs,
                "expired_subs": expired_subs,
                "total_payments": total_payments,
                "total_revenue": total_revenue,
                "today_payments": today_payments,
                "today_revenue": today_revenue,
                "total_plans": total_plans,
                "pool_links": pool_links,
            }

db = Database(DB_FILE)

# =============================================================================
# 3. HELPERS & KEYBOARDS
# =============================================================================

async def grant_subscription_and_send_link(bot: Bot, user_id: int, plan: Dict[str, Any], provider_tx_id: str) -> bool:
    await db.update_payment_status(provider_tx_id, 'paid')
    sub = await db.add_or_extend_subscription(
        telegram_id=user_id,
        plan_id=plan['id'],
        duration_seconds=plan['duration_seconds']
    )

    exp_time = sub['expires_at']
    if isinstance(exp_time, str):
        exp_time = datetime.fromisoformat(exp_time)
    if exp_time.tzinfo is None:
        exp_time = exp_time.replace(tzinfo=timezone.utc)

    pool_item = await db.get_available_invite()
    
    if pool_item:
        invite_link = pool_item['invite_link']
        await db.mark_invite_used(pool_item['id'])
        
        try:
            await bot.revoke_chat_invite_link(chat_id=CHANNEL_ID, invite_link=invite_link)
        except Exception as e:
            logger.warning(f"Failed to revoke link from Telegram channel: {e}")
            
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"🚨 <b>INVITE LINK CONSUMED & REVOKED</b>\n\nUser <code>{user_id}</code> completed payment.\nLink: {invite_link}\nStatus: Removed from Pool & Revoked in Channel."
                )
            except Exception:
                pass
    else:
        try:
            link_expire = min(exp_time, datetime.now(timezone.utc) + timedelta(days=7))
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                name=f"Sub #{sub['id']} - User {user_id}",
                member_limit=1,
                expire_date=link_expire
            )
            invite_link = invite.invite_link
        except Exception as e:
            logger.error(f"Failed dynamic invite creation for user {user_id}: {e}")
            return False

    formatted_exp = exp_time.strftime("%d %b %Y, %H:%M UTC")

    success_msg = (
        "🎉 <b>ACCESS GRANTED!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Plan:</b> {html.quote(plan['name'])}\n"
        f"<b>Status:</b> 🟢 Active\n"
        f"⏳ <b>Expires:</b> <code>{formatted_exp}</code>\n\n"
        "<b>Your Exclusive Access Link:</b>\n"
        f"🔗 {invite_link}\n\n"
        "⚠️ <i>Use this link immediately to join.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 JOIN CHANNEL NOW", url=invite_link)],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="nav_main")]
    ])

    try:
        await bot.send_message(chat_id=user_id, text=success_msg, reply_markup=kb)
        return True
    except Exception as e:
        logger.error(f"Failed sending access message to user {user_id}: {e}")
        return False

def parse_duration_to_seconds(value: int, unit: str) -> int:
    unit = unit.lower()
    if unit in ["minute", "minutes"]: return value * 60
    elif unit in ["hour", "hours"]: return value * 3600
    elif unit in ["day", "days"]: return value * 86400
    elif unit in ["week", "weeks"]: return value * 604800
    elif unit in ["month", "months"]: return value * 30 * 86400
    elif unit in ["year", "years"]: return value * 365 * 86400
    else: raise ValueError(f"Unsupported duration unit: {unit}")

def format_remaining_time(seconds: float) -> str:
    if seconds <= 0: return "Expired"
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0 or days > 0: parts.append(f"{hours:02d}h")
    parts.append(f"{minutes:02d}m")
    return " ".join(parts)

def get_main_menu_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="💎 BUY PREMIUM", callback_data="user_buy")],
        [
            InlineKeyboardButton(text="👤 MY PROFILE", callback_data="user_profile"),
            InlineKeyboardButton(text="⏳ SUBSCRIPTION", callback_data="user_sub")
        ],
        [
            InlineKeyboardButton(text="💳 PAYMENTS", callback_data="user_payments"),
            InlineKeyboardButton(text="🆘 SUPPORT", callback_data="user_support")
        ]
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 ADMIN PANEL", callback_data="admin_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard(target: str = "nav_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data=target)]
    ])

def get_plans_keyboard(plans: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    buttons = []
    for plan in plans:
        symbol = "⭐" if plan['currency'] == "XTR" else "💵"
        btn_text = f"{symbol} {plan['name']} — {int(plan['price']) if plan['currency'] == 'XTR' else plan['price']} {plan['currency']}"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"select_plan_{plan['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Main Menu", callback_data="nav_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Statistics", callback_data="admin_stats"),
            InlineKeyboardButton(text="📦 Manage Plans", callback_data="admin_plans")
        ],
        [
            InlineKeyboardButton(text="🔗 Bulk Invites", callback_data="admin_bulk_invites"),
            InlineKeyboardButton(text="👥 Users", callback_data="admin_users")
        ],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="nav_main")]
    ])

def get_crypto_options_keyboard(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟧 BTC", callback_data=f"pay_crypto_{plan_id}_BTC"),
            InlineKeyboardButton(text="🟦 LTC", callback_data=f"pay_crypto_{plan_id}_LTC"),
            InlineKeyboardButton(text="🔷 ETH", callback_data=f"pay_crypto_{plan_id}_ETH")
        ],
        [
            InlineKeyboardButton(text="🟣 SOL", callback_data=f"pay_crypto_{plan_id}_SOL"),
            InlineKeyboardButton(text="💎 TON", callback_data=f"pay_crypto_{plan_id}_TON"),
            InlineKeyboardButton(text="🔒 XMR", callback_data=f"pay_crypto_{plan_id}_XMR")
        ],
        [
            InlineKeyboardButton(text="🟡 BNB", callback_data=f"pay_crypto_{plan_id}_BNB"),
            InlineKeyboardButton(text="🔴 TRX", callback_data=f"pay_crypto_{plan_id}_TRX"),
            InlineKeyboardButton(text="🐕 DOGE", callback_data=f"pay_crypto_{plan_id}_DOGE")
        ],
        [
            InlineKeyboardButton(text="💲 USDC (SOL)", callback_data=f"pay_crypto_{plan_id}_USDC_SOL"),
            InlineKeyboardButton(text="💲 USDC (BSC)", callback_data=f"pay_crypto_{plan_id}_USDC_BSC"),
            InlineKeyboardButton(text="🟡 DAI (ETH)", callback_data=f"pay_crypto_{plan_id}_DAI_ETH")
        ],
        [
            InlineKeyboardButton(text="💵 USDT (TRX)", callback_data=f"pay_crypto_{plan_id}_USDT_TRX"),
            InlineKeyboardButton(text="💵 USDT (ETH)", callback_data=f"pay_crypto_{plan_id}_USDT_ETH"),
            InlineKeyboardButton(text="💵 USDT (SOL)", callback_data=f"pay_crypto_{plan_id}_USDT_SOL")
        ],
        [
            InlineKeyboardButton(text="💵 USDT (BSC)", callback_data=f"pay_crypto_{plan_id}_USDT_BSC"),
            InlineKeyboardButton(text="💵 USDT (TON)", callback_data=f"pay_crypto_{plan_id}_USDT_TON")
        ],
        [InlineKeyboardButton(text="🔙 Back", callback_data="user_buy")]
    ])

# =============================================================================
# 4. ROUTER & HANDLERS
# =============================================================================

router = Router()

def is_admin_user(user_id: int) -> bool:
    return user_id in ADMIN_IDS

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name
    )

    text = (
        "🌟 <b>WELCOME TO PREMIUM HUB</b> 🌟\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Hello <b>{html.quote(message.from_user.first_name)}</b>! 👋\n\n"
        "Get instant automated access to our VIP Private Channel.\n"
        "Choose an option below to get started:"
    )
    
    is_admin = is_admin_user(message.from_user.id)
    await message.answer(text, reply_markup=get_main_menu_keyboard(is_admin))

@router.callback_query(F.data == "nav_main")
async def nav_main_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    text = (
        "🌟 <b>PREMIUM MAIN MENU</b> 🌟\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Welcome back, <b>{html.quote(callback.from_user.first_name)}</b>!\n"
        "Please choose an action below:"
    )
    is_admin = is_admin_user(callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=get_main_menu_keyboard(is_admin))

@router.callback_query(F.data == "user_profile")
async def user_profile_callback(callback: CallbackQuery):
    await callback.answer()
    user = await db.get_user(callback.from_user.id)
    sub = await db.get_active_subscription(callback.from_user.id)

    status_str = "🟢 ACTIVE" if sub else "⚪ INACTIVE"
    plan_str = sub['plan_name'] if sub else "None"
    
    if sub:
        exp_dt = sub['expires_at']
        if isinstance(exp_dt, str): exp_dt = datetime.fromisoformat(exp_dt)
        if exp_dt.tzinfo is None: exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            
        rem_sec = (exp_dt - datetime.now(timezone.utc)).total_seconds()
        rem_str = format_remaining_time(rem_sec)
        exp_str = exp_dt.strftime("%d %b %Y, %H:%M UTC")
    else:
        rem_str = "N/A"
        exp_str = "N/A"

    uname = f"@{user['username']}" if user and user.get('username') else "Not set"

    text = (
        "👤 <b>YOUR VIP PROFILE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>User ID:</b> <code>{callback.from_user.id}</code>\n"
        f"<b>Username:</b> {html.quote(uname)}\n\n"
        f"💎 <b>Status:</b> {status_str}\n"
        f"📦 <b>Current Plan:</b> {html.quote(plan_str)}\n"
        f"⏳ <b>Time Remaining:</b> {rem_str}\n"
        f"📅 <b>Expires On:</b> <code>{exp_str}</code>"
    )

    await callback.message.edit_text(text, reply_markup=get_back_keyboard())

@router.callback_query(F.data == "user_sub")
async def user_sub_callback(callback: CallbackQuery):
    await callback.answer()
    sub = await db.get_active_subscription(callback.from_user.id)
    if not sub:
        text = (
            "⏳ <b>MY SUBSCRIPTION</b>\n\n"
            "You do not have an active subscription right now."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 BUY ACCESS NOW", callback_data="user_buy")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="nav_main")]
        ])
    else:
        exp_dt = sub['expires_at']
        if isinstance(exp_dt, str): exp_dt = datetime.fromisoformat(exp_dt)
        if exp_dt.tzinfo is None: exp_dt = exp_dt.replace(tzinfo=timezone.utc)

        rem_sec = (exp_dt - datetime.now(timezone.utc)).total_seconds()
        
        text = (
            "⏳ <b>ACTIVE SUBSCRIPTION</b>\n\n"
            f"📦 <b>Plan:</b> {html.quote(sub['plan_name'])}\n"
            f"⏳ <b>Time Left:</b> {format_remaining_time(rem_sec)}\n"
            f"📅 <b>Expires At:</b> <code>{exp_dt.strftime('%d %b %Y, %H:%M UTC')}</code>"
        )
        kb = get_back_keyboard()

    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data == "user_buy")
async def user_buy_callback(callback: CallbackQuery):
    await callback.answer()
    plans = await db.get_active_plans()
    if not plans:
        await callback.message.edit_text("💎 <b>PREMIUM PLANS</b>\n\nNo subscription plans available currently.", reply_markup=get_back_keyboard())
        return

    text = "💎 <b>AVAILABLE PREMIUM PLANS</b>\n\nSelect a plan to unlock full VIP channel access:"
    await callback.message.edit_text(text, reply_markup=get_plans_keyboard(plans))

@router.callback_query(F.data == "user_payments")
async def user_payments_callback(callback: CallbackQuery):
    await callback.answer()
    payments = await db.get_user_payments(callback.from_user.id)
    
    if not payments:
        text = "💳 <b>PAYMENT HISTORY</b>\n\nNo active or past transaction records found."
    else:
        text = "💳 <b>PAYMENT HISTORY</b>\n\n"
        for p in payments:
            status_icon = "✅" if p['status'] == 'paid' else "🟡" if p['status'] == 'pending' else "❌"
            dt = datetime.fromisoformat(str(p['created_at'])).strftime("%d %b %Y, %H:%M")
            plan_name = p['plan_name'] if p['plan_name'] else "Subscription Plan"
            text += f"{status_icon} <b>{html.quote(plan_name)}</b> — {p['amount']} {p['currency']}\n<code>{dt}</code>\n\n"

    await callback.message.edit_text(text, reply_markup=get_back_keyboard())

@router.callback_query(F.data == "user_support")
async def user_support_callback(callback: CallbackQuery):
    await callback.answer()
    text = (
        "🆘 <b>CUSTOMER SUPPORT</b>\n\n"
        "Have questions or need help with access?\n"
        "Feel free to reach out to our team directly:\n\n"
        "💬 <b>Support Contact:</b> @YourSupportUsername"
    )
    await callback.message.edit_text(text, reply_markup=get_back_keyboard())

@router.callback_query(F.data.startswith("select_plan_"))
async def select_plan_callback(callback: CallbackQuery):
    await callback.answer()
    plan_id = int(callback.data.split("_")[2])
    plan = await db.get_plan(plan_id)
    
    if not plan or not plan['active']:
        await callback.message.edit_text("❌ Plan is no longer active.", reply_markup=get_back_keyboard("user_buy"))
        return

    symbol = "⭐" if plan['currency'] == "XTR" else "💰"
    formatted_price = int(plan['price']) if plan['currency'] == "XTR" else plan['price']

    text = (
        f"💎 <b>{html.quote(plan['name'].upper())}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{symbol} <b>Price:</b> {formatted_price} {plan['currency']}\n"
        f"⏱ <b>Duration:</b> {plan['duration_value']} {plan['duration_unit'].capitalize()}\n\n"
        f"📝 <b>Description:</b>\n<i>{html.quote(plan['description'] or 'No description provided.')}</i>"
    )

    if plan['currency'] == "XTR":
        buttons = [[InlineKeyboardButton(text="⭐ PAY WITH STARS", callback_data=f"pay_plan_stars_{plan['id']}")]]
    else:
        buttons = [[InlineKeyboardButton(text="💳 PAY WITH CRYPTO", callback_data=f"show_crypto_opts_{plan['id']}")]]

    if is_admin_user(callback.from_user.id):
        buttons.append([InlineKeyboardButton(text="🧪 Free Admin Pass (Testing)", callback_data=f"admin_free_test_{plan['id']}")])

    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="user_buy")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("show_crypto_opts_"))
async def show_crypto_opts_callback(callback: CallbackQuery):
    await callback.answer()
    plan_id = int(callback.data.split("_")[3])
    text = "💳 <b>SELECT CRYPTOCURRENCY PAYMENT</b>\n\nChoose your preferred payment method below:"
    await callback.message.edit_text(text, reply_markup=get_crypto_options_keyboard(plan_id))

@router.callback_query(F.data.startswith("pay_crypto_"))
async def pay_crypto_selected(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split("_")
    plan_id = int(parts[2])
    coin_key = "_".join(parts[3:])
    
    plan = await db.get_plan(plan_id)
    wallet_addr = WALLETS.get(coin_key, "WALLET_ADDRESS_NOT_SET")
    
    tx_id = f"CRYPTO-{callback.from_user.id}-{plan_id}-{int(datetime.now(timezone.utc).timestamp())}"
    await db.create_payment_record(callback.from_user.id, plan_id, tx_id, plan['price'], coin_key)
    
    await state.update_data(pending_tx_id=tx_id, plan_id=plan_id, coin=coin_key)
    await state.set_state(UserPaymentProofStates.waiting_proof)

    text = (
        f"💰 <b>PAYMENT INSTRUCTIONS ({coin_key.replace('_', ' ')})</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 <b>Plan:</b> {html.quote(plan['name'])}\n"
        f"💵 <b>Amount Due:</b> ${plan['price']} USD (equivalent in {coin_key.split('_')[0]})\n\n"
        f"📥 <b>Deposit Address:</b>\n<code>{wallet_addr}</code>\n\n"
        "📌 <b>Next Steps:</b>\n"
        "1. Send the payment to the wallet address above.\n"
        "2. Send your <b>Transaction ID / Hash</b> or a <b>Screenshot proof</b> here in the chat below:"
    )

    await callback.message.edit_text(text, reply_markup=get_back_keyboard("user_buy"))

@router.message(UserPaymentProofStates.waiting_proof)
async def process_user_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    tx_id = data.get("pending_tx_id")
    plan_id = data.get("plan_id")
    coin = data.get("coin")
    
    await state.clear()

    for admin_id in ADMIN_IDS:
        try:
            admin_msg = (
                "🚨 <b>NEW PAYMENT PROOF RECEIVED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<b>User ID:</b> <code>{message.from_user.id}</code>\n"
                f"<b>Username:</b> @{message.from_user.username or 'N/A'}\n"
                f"<b>Method:</b> {coin}\n"
                f"<b>System TX:</b> <code>{tx_id}</code>\n\n"
                "Verify and click below to approve access:"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ APPROVE PAYMENT", callback_data=f"admin_approve_{message.from_user.id}_{plan_id}_{tx_id}")],
                [InlineKeyboardButton(text="❌ REJECT PAYMENT", callback_data=f"admin_reject_{message.from_user.id}_{tx_id}")]
            ])
            
            await message.bot.send_message(chat_id=admin_id, text=admin_msg)
            if message.photo:
                await message.bot.send_photo(chat_id=admin_id, photo=message.photo[-1].file_id, caption="📸 Payment Screenshot Proof")
            elif message.text:
                await message.bot.send_message(chat_id=admin_id, text=f"💬 <b>Proof Text / TX Hash:</b>\n<code>{html.quote(message.text)}</code>")
        except Exception as e:
            logger.error(f"Error forwarding proof to admin {admin_id}: {e}")

    await message.answer(
        "✅ <b>PAYMENT PROOF SUBMITTED!</b>\n\n"
        "Your payment proof has been forwarded to our admins for verification. "
        "Once verified, your invite link will be sent automatically.",
        reply_markup=get_back_keyboard()
    )

@router.callback_query(F.data.startswith("admin_approve_"))
async def admin_approve_payment(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    
    parts = callback.data.split("_")
    target_user_id = int(parts[2])
    plan_id = int(parts[3])
    tx_id = parts[4]
    
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.message.edit_text("❌ Plan no longer exists.")
        return

    success = await grant_subscription_and_send_link(callback.bot, target_user_id, plan, tx_id)
    if success:
        await callback.message.edit_text(f"✅ Payment approved and invite link sent to User <code>{target_user_id}</code>.")
    else:
        await callback.message.edit_text("❌ Failed to issue invite link. Check bot permissions or invite pool.")

@router.callback_query(F.data.startswith("admin_reject_"))
async def admin_reject_payment(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    
    parts = callback.data.split("_")
    target_user_id = int(parts[2])
    tx_id = parts[3]
    
    await db.update_payment_status(tx_id, 'rejected')
    try:
        await callback.bot.send_message(
            chat_id=target_user_id,
            text="❌ <b>PAYMENT REJECTED</b>\n\nYour payment proof could not be verified. Please contact support."
        )
    except Exception:
        pass
    
    await callback.message.edit_text(f"🔴 Payment rejected for User <code>{target_user_id}</code>.")

@router.callback_query(F.data.startswith("admin_free_test_"))
async def admin_free_test_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id):
        await callback.answer("Unauthorized!", show_alert=True)
        return
    
    await callback.answer("⚡ Processing Admin Free Pass...")
    plan_id = int(callback.data.split("_")[3])
    plan = await db.get_plan(plan_id)
    
    tx_id = f"ADMIN-FREE-{callback.from_user.id}-{int(datetime.now(timezone.utc).timestamp())}"
    await db.create_payment_record(callback.from_user.id, plan_id, tx_id, 0.0, "FREE")
    
    success = await grant_subscription_and_send_link(callback.bot, callback.from_user.id, plan, tx_id)
    if success:
        await callback.message.answer("🧪 <b>ADMIN TEST SUCCESSFUL</b>\nYour free test invite link has been delivered.")

@router.callback_query(F.data.startswith("pay_plan_stars_"))
async def pay_plan_stars_callback(callback: CallbackQuery):
    await callback.answer()
    plan_id = int(callback.data.split("_")[3])
    plan = await db.get_plan(plan_id)

    if not plan or not plan['active']:
        await callback.message.edit_text("❌ Plan is no longer available.", reply_markup=get_back_keyboard("user_buy"))
        return

    tx_id = f"STARS-{callback.from_user.id}-{plan['id']}-{int(datetime.now(timezone.utc).timestamp())}"
    await db.create_payment_record(callback.from_user.id, plan['id'], tx_id, plan['price'], plan['currency'])

    title = f"Subscription: {plan['name']}"
    description = f"Access for {plan['duration_value']} {plan['duration_unit']}"

    prices = [LabeledPrice(label=title, amount=int(plan['price']))]
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=description,
        payload=tx_id,
        provider_token="",
        currency="XTR",
        prices=prices
    )

@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@router.message(F.successful_payment)
async def process_successful_payment_handler(message: Message):
    pmt = message.successful_payment
    tx_id = pmt.invoice_payload
    
    payment_rec = await db.get_payment_by_provider_id(tx_id)
    if payment_rec:
        plan = await db.get_plan(payment_rec['plan_id'])
        if plan:
            await grant_subscription_and_send_link(message.bot, message.from_user.id, plan, tx_id)

# --- ADMIN PANEL HANDLERS ---

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin_user(message.from_user.id):
        await message.answer("❌ Unauthorized access.")
        return
    text = "🛠 <b>ADMIN CONTROL PANEL</b>\n━━━━━━━━━━━━━━━━━━━━━"
    await message.answer(text, reply_markup=get_admin_menu_keyboard())

@router.callback_query(F.data == "admin_main")
async def admin_main_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    text = "🛠 <b>ADMIN CONTROL PANEL</b>\n━━━━━━━━━━━━━━━━━━━━━"
    await callback.message.edit_text(text, reply_markup=get_admin_menu_keyboard())

@router.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    
    stats = await db.get_stats()
    text = (
        "📊 <b>BOT STATISTICS OVERVIEW</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>Total Users:</b> {stats['total_users']}\n"
        f"🟢 <b>Active Subscriptions:</b> {stats['active_subs']}\n"
        f"⏰ <b>Expired Subscriptions:</b> {stats['expired_subs']}\n"
        f"📦 <b>Active Plans:</b> {stats['total_plans']}\n"
        f"🔗 <b>Available Pool Invites:</b> {stats['pool_links']}\n\n"
        "💵 <b>FINANCIAL METRICS</b>\n"
        f"💰 <b>Total Payments:</b> {stats['total_payments']}\n"
        f"📈 <b>Total Revenue:</b> {stats['total_revenue']}\n"
        f"📊 <b>Today Payments:</b> {stats['today_payments']}"
    )
    await callback.message.edit_text(text, reply_markup=get_back_keyboard("admin_main"))

@router.callback_query(F.data == "admin_bulk_invites")
async def admin_bulk_invites_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    await state.set_state(AdminBulkInviteStates.waiting_links)
    await callback.message.edit_text(
        "🔗 <b>ADD BULK INVITE LINKS</b>\n\n"
        "Send multiple invite links separated by newline or space:\n\n"
        "<code>https://t.me/+ExampleLink1\nhttps://t.me/+ExampleLink2</code>",
        reply_markup=get_back_keyboard("admin_main")
    )

@router.message(AdminBulkInviteStates.waiting_links)
async def process_bulk_invite_links(message: Message, state: FSMContext):
    links = [line.strip() for line in message.text.split() if "t.me" in line]
    if not links:
        await message.answer("❌ No valid Telegram invite links found. Try again.")
        return
        
    added_count = await db.add_bulk_invites(links)
    await state.clear()
    await message.answer(
        f"✅ <b>BULK INVITES ADDED</b>\n\nSuccessfully added {added_count} unique invite link(s) to the storage pool.",
        reply_markup=get_back_keyboard("admin_main")
    )

@router.callback_query(F.data == "admin_plans")
async def admin_plans_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    
    plans = await db.get_all_plans()
    text = "📦 <b>MANAGE SUBSCRIPTION PLANS</b>\n\nSelect a plan to configure or create a new one:"
    
    buttons = [
        [InlineKeyboardButton(text="➕ Add New Plan", callback_data="admin_add_plan")]
    ]
    for p in plans:
        status_icon = "🟢" if p['active'] else "🔴"
        sym = "⭐" if p['currency'] == "XTR" else "$"
        buttons.append([InlineKeyboardButton(
            text=f"{status_icon} {p['name']} ({sym}{p['price']} {p['currency']})",
            callback_data=f"admin_manage_plan_{p['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_main")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "admin_add_plan")
async def admin_add_plan_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    await state.set_state(AdminPlanStates.waiting_name)
    await callback.message.edit_text(
        "➕ <b>CREATE PLAN (Step 1/6)</b>\n\nEnter the title for this subscription plan (e.g., <code>VIP Pass 30 Days</code>):",
        reply_markup=get_back_keyboard("admin_plans")
    )

@router.message(AdminPlanStates.waiting_name)
async def process_plan_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminPlanStates.waiting_price)
    await message.answer("💰 <b>CREATE PLAN (Step 2/6)</b>\n\nEnter price (numeric, e.g., <code>100</code> for Stars or <code>9.99</code>):")

@router.message(AdminPlanStates.waiting_price)
async def process_plan_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip())
        if price <= 0: raise ValueError()
        await state.update_data(price=price)
        await state.set_state(AdminPlanStates.waiting_currency)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Telegram Stars (XTR)", callback_data="curr_XTR")],
            [InlineKeyboardButton(text="💵 USD (Crypto / Fiat)", callback_data="curr_USD")]
        ])
        await message.answer("💱 <b>CREATE PLAN (Step 3/6)</b>\n\nSelect plan currency:", reply_markup=kb)
    except ValueError:
        await message.answer("❌ Invalid price. Enter a positive number.")

@router.callback_query(AdminPlanStates.waiting_currency, F.data.startswith("curr_"))
async def process_plan_currency(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    currency = callback.data.split("_")[1]
    await state.update_data(currency=currency)
    await state.set_state(AdminPlanStates.waiting_duration_val)
    await callback.message.edit_text("⏱ <b>CREATE PLAN (Step 4/6)</b>\n\nEnter duration amount (integer, e.g., <code>30</code>):")

@router.message(AdminPlanStates.waiting_duration_val)
async def process_plan_dur_val(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("❌ Enter a valid integer for duration.")
        return
    await state.update_data(duration_val=int(message.text))
    await state.set_state(AdminPlanStates.waiting_duration_unit)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Minutes", callback_data="unit_minutes"),
            InlineKeyboardButton(text="Hours", callback_data="unit_hours")
        ],
        [
            InlineKeyboardButton(text="Days", callback_data="unit_days"),
            InlineKeyboardButton(text="Weeks", callback_data="unit_weeks")
        ],
        [
            InlineKeyboardButton(text="Months", callback_data="unit_months"),
            InlineKeyboardButton(text="Years", callback_data="unit_years")
        ]
    ])
    await message.answer("⏱ <b>CREATE PLAN (Step 5/6)</b>\n\nSelect time unit:", reply_markup=kb)

@router.callback_query(AdminPlanStates.waiting_duration_unit, F.data.startswith("unit_"))
async def process_plan_dur_unit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    unit = callback.data.split("_")[1]
    await state.update_data(duration_unit=unit)
    await state.set_state(AdminPlanStates.waiting_description)
    await callback.message.edit_text("📝 <b>CREATE PLAN (Step 6/6)</b>\n\nEnter plan description:")

@router.message(AdminPlanStates.waiting_description)
async def process_plan_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    data = await state.get_data()
    dur_sec = parse_duration_to_seconds(data['duration_val'], data['duration_unit'])
    await state.update_data(duration_sec=dur_sec)

    text = (
        "<b>CONFIRM PLAN CREATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 <b>Name:</b> {html.quote(data['name'])}\n"
        f"💰 <b>Price:</b> {data['price']} {data['currency']}\n"
        f"⏱ <b>Duration:</b> {data['duration_val']} {data['duration_unit'].capitalize()}\n"
        f"📝 <b>Description:</b> {html.quote(data['description'])}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ CONFIRM & SAVE", callback_data="confirm_create_plan")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="admin_plans")]
    ])
    await state.set_state(AdminPlanStates.confirmation)
    await message.answer(text, reply_markup=kb)

@router.callback_query(AdminPlanStates.confirmation, F.data == "confirm_create_plan")
async def confirm_create_plan(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    data = await state.get_data()
    
    await db.create_plan(
        name=data['name'],
        price=data['price'],
        currency=data['currency'],
        duration_val=data['duration_val'],
        duration_unit=data['duration_unit'],
        duration_sec=data['duration_sec'],
        description=data['description']
    )
    await state.clear()
    await callback.message.edit_text("✅ Plan created successfully!", reply_markup=get_back_keyboard("admin_plans"))

@router.callback_query(F.data.startswith("admin_manage_plan_"))
async def manage_plan_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    plan_id = int(callback.data.split("_")[3])
    plan = await db.get_plan(plan_id)
    if not plan:
        await callback.message.edit_text("Plan not found.", reply_markup=get_back_keyboard("admin_plans"))
        return

    status_str = "🟢 Active" if plan['active'] else "🔴 Disabled"
    text = (
        f"📦 <b>PLAN DETAILS: {html.quote(plan['name'])}</b>\n\n"
        f"<b>Status:</b> {status_str}\n"
        f"<b>Price:</b> {plan['price']} {plan['currency']}\n"
        f"<b>Duration:</b> {plan['duration_value']} {plan['duration_unit']}\n"
        f"<b>Description:</b> {html.quote(plan['description'] or '')}"
    )

    toggle_txt = "🔴 Disable Plan" if plan['active'] else "🟢 Enable Plan"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_txt, callback_data=f"admin_toggle_plan_{plan['id']}")],
        [InlineKeyboardButton(text="🗑 Completely Delete Plan", callback_data=f"admin_delete_plan_{plan['id']}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_plans")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@router.callback_query(F.data.startswith("admin_toggle_plan_"))
async def toggle_plan_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    plan_id = int(callback.data.split("_")[3])
    await db.toggle_plan_status(plan_id)
    await manage_plan_callback(callback)

@router.callback_query(F.data.startswith("admin_delete_plan_"))
async def delete_plan_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    plan_id = int(callback.data.split("_")[3])
    await db.delete_plan(plan_id)
    await callback.message.edit_text("✅ Plan completely deleted from database.", reply_markup=get_back_keyboard("admin_plans"))

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    await state.set_state(BroadcastStates.waiting_message)
    await callback.message.edit_text(
        "📢 <b>ADMIN BROADCAST</b>\n\nSend the message you want to broadcast to all registered users:",
        reply_markup=get_back_keyboard("admin_main")
    )

@router.message(BroadcastStates.waiting_message)
async def admin_broadcast_confirm(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastStates.confirmation)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 START BROADCAST", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="❌ CANCEL", callback_data="admin_main")]
    ])
    await message.answer(f"⚠️ <b>CONFIRM BROADCAST</b>\n\nPreview:\n{message.text}", reply_markup=kb)

@router.callback_query(BroadcastStates.confirmation, F.data == "confirm_broadcast")
async def admin_broadcast_execute(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    data = await state.get_data()
    text = data['broadcast_text']
    await state.clear()

    user_ids = await db.get_all_user_ids()
    await callback.message.edit_text(f"⏳ Broadcasting message to {len(user_ids)} users...")

    success, failed = 0, 0
    for uid in user_ids:
        try:
            await callback.bot.send_message(chat_id=uid, text=text)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await callback.bot.send_message(chat_id=uid, text=text)
                success += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

    await callback.message.answer(
        f"📢 <b>BROADCAST COMPLETED</b>\n\n"
        f"✅ <b>Successful:</b> {success}\n"
        f"❌ <b>Failed:</b> {failed}",
        reply_markup=get_back_keyboard("admin_main")
    )

@router.callback_query(F.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery, state: FSMContext):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    await state.set_state(AdminUserSearchStates.waiting_query)
    await callback.message.edit_text(
        "👥 <b>USER MANAGEMENT</b>\n\nEnter Telegram User ID to search database:",
        reply_markup=get_back_keyboard("admin_main")
    )

@router.message(AdminUserSearchStates.waiting_query)
async def admin_user_search_result(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Invalid User ID. Enter numeric ID.")
        return

    target_id = int(message.text)
    user = await db.get_user(target_id)
    if not user:
        await message.answer("❌ User not found.", reply_markup=get_back_keyboard("admin_main"))
        return

    sub = await db.get_active_subscription(target_id)
    status_str = "🟢 Active" if sub else "⚪ Inactive"

    text = (
        f"👤 <b>USER DETAILS</b>\n\n"
        f"<b>ID:</b> <code>{user['telegram_id']}</code>\n"
        f"<b>Name:</b> {html.quote(user['first_name'])}\n"
        f"<b>Username:</b> @{user['username'] or 'N/A'}\n"
        f"<b>Subscription:</b> {status_str}\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Extend Sub (+7 Days)", callback_data=f"admin_ext_sub_{target_id}")],
        [InlineKeyboardButton(text="🔴 Revoke Access", callback_data=f"admin_revoke_sub_{target_id}")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_main")]
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("admin_ext_sub_"))
async def admin_ext_sub_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    target_id = int(callback.data.split("_")[3])
    
    plans = await db.get_active_plans()
    plan_id = plans[0]['id'] if plans else 1
    await db.add_or_extend_subscription(target_id, plan_id, duration_seconds=7*86400)
    
    await callback.message.edit_text(f"✅ Granted 7 days extension to user {target_id}.", reply_markup=get_back_keyboard("admin_main"))

@router.callback_query(F.data.startswith("admin_revoke_sub_"))
async def admin_revoke_sub_callback(callback: CallbackQuery):
    if not is_admin_user(callback.from_user.id): return
    await callback.answer()
    target_id = int(callback.data.split("_")[3])
    
    await db.revoke_user_subscriptions(target_id)
    try:
        await callback.bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=target_id)
        await callback.bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=target_id)
    except Exception as e:
        logger.error(f"Failed kicking user {target_id}: {e}")

    await callback.message.edit_text(f"🔴 Access revoked for user {target_id}.", reply_markup=get_back_keyboard("admin_main"))

# =============================================================================
# 5. EXPIRATION WORKER & MAIN ENTRY POINT
# =============================================================================

async def expiration_worker(bot: Bot):
    while True:
        try:
            expired_subs = await db.get_expired_subscriptions()
            for sub in expired_subs:
                user_id = sub['telegram_id']
                sub_id = sub['id']

                await db.mark_subscription_expired(sub_id)

                try:
                    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
                except Exception as e:
                    logger.error(f"Error kicking user {user_id}: {e}")

                try:
                    exp_text = (
                        "⏰ <b>SUBSCRIPTION EXPIRED</b>\n\n"
                        "Your VIP channel access has expired. Please renew your subscription to continue."
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💎 BUY ACCESS", callback_data="user_buy")]
                    ])
                    await bot.send_message(chat_id=user_id, text=exp_text, reply_markup=kb)
                except Exception as e:
                    logger.error(f"Failed to notify user {user_id}: {e}")

        except Exception as e:
            logger.error(f"Error in background worker: {e}")

        await asyncio.sleep(CHECK_INTERVAL)

async def main():
    await db.init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(expiration_worker(bot))

    logger.info("Bot starting long polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
