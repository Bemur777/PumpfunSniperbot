import os
import asyncio
import aiohttp
import sqlite3
import numpy as np
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import TransferParams, transfer
from dotenv import load_dotenv

# Загрузка переменных окружения
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
        self.client = AsyncClient(SOLANA_RPC)
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
        self.init_db()

    async def __aenter__(self):
        await self.client.__aenter__()
        self.http_session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)
        await self.http_session.close()

    def init_db(self):
        with sqlite3.connect(DATABASE) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS users
                         (user_id INT PRIMARY KEY, paid_until DATE)''')

    async def check_payment(self, user_id: int) -> bool:
        async with self.client.get_signatures_for_address(PAYMENT_WALLET) as resp:
            signatures = resp.value
        
        for sig in signatures[-50:]:
            tx = await self.client.get_transaction(sig.signature)
            if str(user_id) in tx.transaction.meta.log_messages:
                return True
        return False

    async def analyze_token(self, token_address: str) -> dict:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with self.http_session.get(url) as resp:
            data = await resp.json()
        
        holders = await self.get_holders(token_address)
        prices = [float(x['priceUsd']) for x in data['pairs'][0]['priceHistory']['5m']]
        
        return {
            'liquidity': float(data['pairs'][0]['liquidity']['usd']),
            'holders': len(holders),
            'concentration': self.calculate_concentration(holders),
            'volatility': np.std(prices)/np.mean(prices),
            'volume_change': self.calculate_volume_change(data)
        }

    def calculate_concentration(self, holders):
        if not holders:
            return 1.0
        top5 = sum(h['amount'] for h in holders[:5])
        total = sum(h['amount'] for h in holders)
        return top5 / total

    async def execute_trade(self, token_address: str, action: str):
        analysis = await self.analyze_token(token_address)
        
        if not self.is_safe(analysis):
            return "High risk token"
        
        try:
            tx = Transaction().add(transfer(TransferParams(
                from_pubkey=self.keypair.pubkey(),
                to_pubkey=Pubkey.from_string(token_address),
                lamports=int(0.01 * 10**9)
            )))
            await self.client.send_transaction(tx, self.keypair)
            return "Trade executed successfully"
        except Exception as e:
            return f"Error: {str(e)}"

    async def monitor_position(self, token_address: str):
        entry_price = await self.get_price(token_address)
        while True:
            current_price = await self.get_price(token_address)
            change = (current_price - entry_price) / entry_price
            
            if change >= self.risk_params['take_profit'] or change <= self.risk_params['stop_loss']:
                await self.execute_trade(token_address, 'sell')
                break
            await asyncio.sleep(10)

    async def get_price(self, token_address: str):
        async with self.http_session.get(f"https://api.pump.fun/price/{token_address}") as resp:
            return float(await resp.text())

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
            asyncio.create_task(run_sniper(user_id, context))
        else:
            await query.answer("❌ Оплата не найдена")

async def run_sniper(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data['bot']
    while True:
        try:
            tokens = await get_new_tokens()
            for token in tokens:
                await bot.execute_trade(token)
        except Exception as e:
            print(f"Sniper error: {str(e)}")
        await asyncio.sleep(30)

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
