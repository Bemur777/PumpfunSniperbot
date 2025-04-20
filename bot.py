import os
import asyncio
import aiohttp
import sqlite3
import logging
import numpy as np
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
from dotenv import load_dotenv

# Настройка логгирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# Конфигурация
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
TG_TOKEN = os.getenv("TG_TOKEN")
PAYMENT_WALLET = Pubkey.from_string(os.getenv("PAYMENT_WALLET"))
BOT_PRIVATE_KEY = os.getenv("BOT_PRIVATE_KEY")
DATABASE = "users.db"
MIN_PAYMENT = 4 * 10**9  # 4 SOL в лампортах

class PumpFunSniper:
    def __init__(self):
        self.client = None
        self.http_session = None
        self.keypair = Keypair.from_bytes(bytes.fromhex(BOT_PRIVATE_KEY))
        self.risk_params = {
            'max_volume_drop': 0.4,
            'min_holders': 100,
            'max_concentration': 0.3,
            'stop_loss': -0.15,
            'take_profit': 0.25,
            'max_position': 0.1
        }
        self.active_tasks = {}
        self.init_db()

    async def __aenter__(self):
        self.client = await AsyncClient(SOLANA_RPC).__aenter__()
        self.http_session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)
        await self.http_session.close()

    def init_db(self):
        with sqlite3.connect(DATABASE) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS users
                         (user_id INT PRIMARY KEY, paid_until DATE)''')

    async def update_subscription(self, user_id: int, days: int = 30):
    with sqlite3.connect(DATABASE) as conn:    
        paid_until = datetime.now() + timedelta(days=days)
        conn.execute(
            "INSERT OR REPLACE INTO users VALUES (?, ?)",
            (user_id, paid_until.strftime("%Y-%m-%d"))  # Закрывающая скобка
        )
        conn.commit()    
            
    async def check_payment(self, user_id: int) -> bool:
        try:
            resp = await self.client.get_signatures_for_address(PAYMENT_WALLET)
            for sig in resp.value[-50:]:
                tx = await self.client.get_transaction(sig.signature)
                if any(str(user_id) in msg for msg in tx.transaction.meta.log_messages):
                    await self.update_subscription(user_id)
                    return True
            return False
        except Exception as e:
            logger.error(f"Payment check error: {str(e)}")
            return False

    async def get_holders(self, token_address: str) -> list:
        try:
            url = f"https://api.solscan.io/token/holders?token={token_address}&offset=0&limit=10"
            async with self.http_session.get(url) as resp:
                data = await resp.json()
                return data['data']['result'] if data['success'] else []
        except Exception as e:
            logger.error(f"Holders fetch error: {str(e)}")
            return []

    def calculate_volume_change(self, data: dict) -> float:
        try:
            current_volume = data['pairs'][0]['volume']['h24']
            prev_volume = data['pairs'][0]['volume']['h6']
            return (current_volume - prev_volume) / prev_volume if prev_volume else 0
        except:
            return 0

    def is_safe(self, analysis: dict) -> bool:
        return all([
            analysis['liquidity'] > 1000,
            analysis['holders'] >= self.risk_params['min_holders'],
            analysis['concentration'] <= self.risk_params['max_concentration'],
            analysis['volatility'] < 0.5,
            analysis['volume_change'] >= -self.risk_params['max_volume_drop']
        ])

    async def execute_trade(self, token_address: str, action: str = 'buy'):
        try:
            analysis = await self.analyze_token(token_address)
            
            if not self.is_safe(analysis):
                return "High risk token - trade canceled"
            
            # Реальная логика торговли через Raydium API
            trade_url = "https://api.raydium.io/v2/sdk/swap/"
            payload = {
                "token": token_address,
                "amount": 0.01 if action == 'buy' else -0.01,
                "slippage": 0.5
            }
            
            async with self.http_session.post(trade_url, json=payload) as resp:
                result = await resp.json()
                
            if result['status'] == 'success':
                logger.info(f"Trade {action} executed for {token_address}")
                return "Trade executed successfully"
            return "Trade failed"
            
        except Exception as e:
            logger.error(f"Trade error: {str(e)}")
            return f"Error: {str(e)}"

    async def monitor_position(self, token_address: str):
        try:
            entry_price = await self.get_price(token_address)
            while True:
                current_price = await self.get_price(token_address)
                change = (current_price - entry_price) / entry_price
                
                if change >= self.risk_params['take_profit']:
                    await self.execute_trade(token_address, 'sell')
                    break
                    
                if change <= self.risk_params['stop_loss']:
                    await self.execute_trade(token_address, 'sell')
                    break
                    
                await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Monitoring error: {str(e)}")

    async def get_price(self, token_address: str) -> float:
        try:
            async with self.http_session.get(
                f"https://api.pump.fun/price/{token_address}"
            ) as resp:
                return float(await resp.text())
        except:
            return 0.0

async def get_new_tokens():
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.pump.fun/tokens/new"
            async with session.get(url) as resp:
                data = await resp.json()
                return [t['address'] for t in data['tokens'][:5]]
    except Exception as e:
        logger.error(f"New tokens fetch error: {str(e)}")
        return []

# Telegram Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛒 Купить доступ (4 SOL)", callback_data='buy')],
        [InlineKeyboardButton("🔓 Проверить подписку", callback_data='verify')]
    ]
    await update.message.reply_text(
        "🔐 *Premium Pump.fun Sniper*\n\n"
        "Доступ включает:\n"
        "- Авто-снайпинг новых токенов\n"
        "- Риск-менеджмент\n"
        "- 24/7 мониторинг\n",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data['bot']
    query = update.callback_query
    user_id = query.from_user.id
    
    if query.data == 'buy':
        msg = f"Отправьте 4 SOL на адрес:\n`{PAYMENT_WALLET}`\nс memo: `{user_id}`"
        await query.message.reply_text(msg, parse_mode='Markdown')
    
    elif query.data == 'verify':
        if await bot.check_payment(user_id):
            await query.answer("✅ Доступ активирован!")
            bot.active_tasks[user_id] = asyncio.create_task(
                run_sniper(user_id, context)
            )
        else:
            await query.answer("❌ Оплата не найдена")

async def run_sniper(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data['bot']
    logger.info(f"Starting sniper for user {user_id}")
    while True:
        try:
            tokens = await get_new_tokens()
            for token in tokens:
                result = await bot.execute_trade(token)
                if "success" in result:
                    await context.bot.send_message(
                        user_id,
                        f"✅ Куплен токен: `{token}`",
                        parse_mode='Markdown'
                    )
                    asyncio.create_task(bot.monitor_position(token))
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Sniper error: {str(e)}")
            await asyncio.sleep(60)

async def main():
    app = Application.builder().token(TG_TOKEN).build()
    
    async with PumpFunSniper() as bot:
        app.bot_data['bot'] = bot
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CallbackQueryHandler(handle_callback))
        
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await app.stop()
            await app.updater.stop()

if __name__ == "__main__":
    asyncio.run(main())
