import os
import sys
import logging
from typing import List, Dict

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("PremiumBot")

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "8915459958:AAFO-n9yWm_AXpANQOGpmmiizM0A90dlqEs").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "7952327997").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "-1004253941443").strip()
DB_FILE = os.getenv("DB_FILE", "bot_database.sqlite3").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

# Crypto Wallet Configurations
WALLETS: Dict[str, str] = {
    "BTC": os.getenv("WALLET_BTC", "YOUR_BTC_WALLET_ADDRESS"),
    "LTC": os.getenv("WALLET_LTC", "YOUR_LTC_WALLET_ADDRESS"),
    "ETH": os.getenv("WALLET_ETH", "YOUR_ETH_WALLET_ADDRESS"),
    "SOL": os.getenv("WALLET_SOL", "YOUR_SOL_WALLET_ADDRESS"),
    "TON": os.getenv("WALLET_TON", "YOUR_TON_WALLET_ADDRESS"),
    "XMR": os.getenv("WALLET_XMR", "YOUR_XMR_WALLET_ADDRESS"),
    "BNB": os.getenv("WALLET_BNB", "YOUR_BNB_WALLET_ADDRESS"),
    "TRX": os.getenv("WALLET_TRX", "YOUR_TRX_WALLET_ADDRESS"),
    "DOGE": os.getenv("WALLET_DOGE", "YOUR_DOGE_WALLET_ADDRESS"),
    "USDC_SOL": os.getenv("WALLET_USDC_SOL", "YOUR_USDC_SOL_WALLET"),
    "USDC_BSC": os.getenv("WALLET_USDC_BSC", "YOUR_USDC_BSC_WALLET"),
    "DAI_ETH": os.getenv("WALLET_DAI_ETH", "YOUR_DAI_ETH_WALLET"),
    "USDT_TRX": os.getenv("WALLET_USDT_TRX", "YOUR_USDT_TRX_WALLET"),
    "USDT_ETH": os.getenv("WALLET_USDT_ETH", "YOUR_USDT_ETH_WALLET"),
    "USDT_SOL": os.getenv("WALLET_USDT_SOL", "YOUR_USDT_SOL_WALLET"),
    "USDT_BSC": os.getenv("WALLET_USDT_BSC", "YOUR_USDT_BSC_WALLET"),
    "USDT_TON": os.getenv("WALLET_USDT_TON", "YOUR_USDT_TON_WALLET"),
}

# Environmental Variable Parsing & Validation
if not BOT_TOKEN:
    logger.critical("FATAL: BOT_TOKEN environment variable is not set!")
    sys.exit(1)

try:
    ADMIN_IDS: List[int] = [
        int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().lstrip("-").isdigit()
    ]
except Exception as e:
    logger.critical(f"FATAL: Invalid ADMIN_IDS format: {e}")
    sys.exit(1)

try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError:
    logger.critical("FATAL: CHANNEL_ID must be a valid integer")
    sys.exit(1)
