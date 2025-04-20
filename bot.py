import os
import asyncio
import aiohttp
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from solana.rpc.async_api import AsyncClient
from solana.keypair import Keypair
from solana.transaction import Transaction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
import numpy as np

# Конфигурация
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
TG_TOKEN = os.getenv("TG_TOKEN")
PAYMENT_WALLET = Pubkey.from_string(os.getenv("PAYMENT_WALLET"))
DATABASE = "users.db"
MIN_PAYMENT = 4 * 10**9  # 4 SOL в лампортах

class PumpFunSniper:
    def __init__(self):
        self.client = AsyncClient(SOLANA_RPC)
        self.http_session = aiohttp.ClientSession()
        self.risk_params = {
            'max_volume_drop': 0.4,
            'min_holders': 100,
            'max_concentration': 0.3,
            'stop_loss': -0.15,
            'take_profit': 0.25,
            'max_position': 0.1
        }
        self.init_db()

    def init_db(self):
        with sqlite3.connect(DATABASE) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS users
                         (user_id INT PRIMARY KEY, paid_until DATE)''')

    async def check_payment(self, user_id: int) -> bool:
        async with self.client.get_signatures_for_address(PAYMENT_WALLET) as resp:
            signatures = resp.value
        
        for sig in signatures[-50:]:  # Проверка последних 50 транзакций
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

    async def execute_trade(self, user_id: int, token_address: str):
        analysis = await self.analyze_token(token_address)
        
        if not self.is_safe(analysis):
            return "High risk token"
        
        try:
            # Логика покупки
            tx = Transaction().add(transfer(TransferParams(
                from_pubkey=PAYMENT_WALLET,
                to_pubkey=Pubkey.from_string(token_address),
                lamports=int(0.01 * 10**9)  # 0.01 SOL
            )))
            await self.client.send_transaction(tx, Keypair.from_bytes(os.getenv("BOT_KEY")))
            
            # Запуск мониторинга
            asyncio.create_task(self.monitor_position(token_address))
            return "Trade executed"
        except Exception as e:
            return f"Error: {str(e)}"

    async def monitor_position(self, token_address: str):
        entry_price = await self.get_price(token_address)
        while True:
            current_price = await self.get_price(token_address)
            change = (current_price - entry_price) / entry_price
            
            if change >= self.risk_params['take_profit'] or change <= self.risk_params['stop_loss']:
                await self.sell_token(token_address)
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
            # Запуск снайпера
            asyncio.create_task(run_sniper(user_id))
        else:
            await query.answer("❌ Оплата не найдена")

async def run_sniper(user_id: int):
    bot = context.bot_data['bot']
    while True:
        try:
            tokens = await get_new_tokens()
            for token in tokens:
                await bot.execute_trade(user_id, token)
        except Exception as e:
            print(f"Sniper error: {str(e)}")
        await asyncio.sleep(30)

if __name__ == "__main__":
    app = Application.builder().token(TG_TOKEN).build()
    app.bot_data['bot'] = PumpFunSniper()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    print("🟢 Бот запущен")
    app.run_polling()
