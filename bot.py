import aiohttp
import sqlite3
import numpy as np
import logging
import os
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.instruction import Instruction, AccountMeta
from solders.message import Message
from solders.system_program import TransferParams, transfer
from solders.keypair import Keypair
from solders.signature import Signature
from base58 import b58encode, b58decode
from dotenv import load_dotenv
from cryptography.fernet import Fernet

# Конфигурация
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
TG_TOKEN = os.getenv("TG_TOKEN")
FEE_WALLET = Pubkey.from_string(os.getenv("FEE_WALLET"))  # Добавлен кошелек для комиссий
DATABASE = "users.db"
FERNET_KEY = os.getenv("FERNET_KEY")

# Константы
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
TRADING_FEE = 0.005  # 0.5% комиссия

class PumpFunSniper:
    async def __aenter__(self):
        self.client = AsyncClient(SOLANA_RPC)
        self.http_session = aiohttp.ClientSession()
        self.cipher = Fernet(FERNET_KEY)
        self.risk_params = {
            'take_profit': 0.3,
            'stop_loss': -0.2,
            'max_position': 0.1
        }
        self.active_tasks = {}
        self.init_db()
        return self

    async def __aexit__(self, *args):
        await self.client.close()
        await self.http_session.close()

    def init_db(self):
        with sqlite3.connect(DATABASE) as conn:
            # Удалена таблица users
            conn.execute('''CREATE TABLE IF NOT EXISTS wallets
                         (user_id INT, encrypted_key TEXT, PRIMARY KEY(user_id))''')

    # Шифрование кошельков
    def encrypt_key(self, key: bytes) -> str:
        return self.cipher.encrypt(key).decode()

    def decrypt_key(self, encrypted_key: str) -> Keypair:
        return Keypair.from_bytes(self.cipher.decrypt(encrypted_key.encode()))

    # Торговые операции с комиссией
    async def buy_token(self, user_id: int, token_address: Pubkey, amount: float):
        try:
            wallet = self.get_user_wallet(user_id)
            
            # Расчет комиссии
            fee = amount * TRADING_FEE
            amount_after_fee = amount - fee
            
            recent_blockhash = (await self.client.get_latest_blockhash()).value.blockhash
            
            # Инструкция для покупки токена
            buy_ix = Instruction(
                program_id=PUMP_FUN_PROGRAM_ID,
                data=bytes.fromhex("02"),
                keys=[
                    AccountMeta(pubkey=wallet.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=token_address, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=Pubkey.from_string("So11111111111111111111111111111111111111112"),
                               is_signer=False, is_writable=True)
                ]
            )
            
            # Инструкция для отправки комиссии
            fee_ix = transfer(TransferParams(
                from_pubkey=wallet.pubkey(),
                to_pubkey=FEE_WALLET,
                lamports=int(fee)
            ))

            tx = Transaction().add(buy_ix).add(fee_ix)
            tx.recent_blockhash = recent_blockhash
            tx.sign([wallet])
            
            result = await self.client.send_transaction(tx)
            return result.value
        except Exception as e:
            logger.error(f"Buy error: {str(e)}")
            return None

    async def sell_token(self, user_id: int, token_address: Pubkey, amount: float):
        try:
            wallet = self.get_user_wallet(user_id)
            
            # Расчет комиссии
            fee = amount * TRADING_FEE
            amount_after_fee = amount - fee
            
            recent_blockhash = (await self.client.get_latest_blockhash()).value.blockhash
            
            # Инструкция для продажи (примерная реализация)
            sell_ix = Instruction(
                program_id=PUMP_FUN_PROGRAM_ID,
                data=bytes.fromhex("03"),
                keys=[
                    AccountMeta(pubkey=wallet.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=token_address, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=Pubkey.from_string("So11111111111111111111111111111111111111112"),
                               is_signer=False, is_writable=True)
                ]
            )
            
            # Инструкция для комиссии
            fee_ix = transfer(TransferParams(
                from_pubkey=wallet.pubkey(),
                to_pubkey=FEE_WALLET,
                lamports=int(fee)
            ))

            tx = Transaction().add(sell_ix).add(fee_ix)
            tx.recent_blockhash = recent_blockhash
            tx.sign([wallet])
            
            result = await self.client.send_transaction(tx)
            return result.value
        except Exception as e:
            logger.error(f"Sell error: {str(e)}")
            return None

    # Работа с кошельками
    def add_wallet(self, user_id: int, private_key: str):
        encrypted = self.encrypt_key(b58decode(private_key))
        with sqlite3.connect(DATABASE) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO wallets VALUES (?, ?)", 
                (user_id, encrypted))
            conn.commit()

    def get_user_w
