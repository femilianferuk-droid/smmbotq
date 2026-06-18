import os
import logging
import asyncio
import math
import psycopg2
from aiogram import Bot, Dispatcher, F, types
# ИСПРАВЛЕНО: Теперь импортируются все три нужных класса фильтров
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import LabeledPrice, PreCheckoutQuery, InlineKeyboardButton, InlineKeyboardMarkup
from werkzeug.security import generate_password_hash

# =====================================================================
# КОНФИГУРАЦИЯ (Токен и БД из окружения, сайт — в коде)
# =====================================================================
TG_BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_URL = os.environ.get("DATABASE_URL")
STARS_RATE_RUB = 0.7
WEBSITE_URL = "https://vestsmm.shop"

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()

# Состояния для сброса забытого пароля
class PasswordResetStates(StatesGroup):
    waiting_for_new_password = State()

# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БАЗЫ ДАННЫХ
# =====================================================================
def get_db_connection():
    return psycopg2.connect(DB_URL)

def get_invoice_info(invoice_id: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT user_id, amount_rub, status FROM invoices WHERE invoice_id = %s", (invoice_id,))
                return cursor.fetchone()
    except Exception as e:
        logging.error(f"Ошибка получения инвойса {invoice_id}: {e}")
        return None

def credit_user_balance(invoice_id: str, user_id: int, amount_rub: float):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE invoices SET status = 'paid' WHERE invoice_id = %s AND status = 'active'", (invoice_id,))
                if cursor.rowcount > 0:
                    cursor.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (amount_rub, user_id))
                    conn.commit()
                    logging.info(f"Успешно зачислено {amount_rub} руб пользователю ID {user_id}")
                    return True
                return False
    except Exception as e:
        logging.error(f"Ошибка при обновлении баланса для инвойса {invoice_id}: {e}")
        return False

# =====================================================================
# ХЕНДЛЕРЫ КОМАНД И ГЛУБОКИХ ССЫЛОК (DEEP LINKING)
# =====================================================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    args = command.args
    main_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Перейти на сайт", url=WEBSITE_URL)]
    ])

    if args:
        start_param = args.strip()
        
        # СЦЕНАРИЙ 1: ПРИВЯЗКА АККАУНТА TELEGRAM (lnk_xxxxx)
        if start_param.startswith("lnk_"):
            code = start_param.replace("lnk_", "")
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT id, username FROM users WHERE link_code = %s", (code,))
                        user_data = cursor.fetchone()
                        
                        if user_data:
                            user_id, web_username = user_data
                            cursor.execute("""
                                UPDATE users 
                                SET telegram_id = %s, tg_username = %s, tg_name = %s, link_code = NULL 
                                WHERE id = %s
                            """, (message.from_user.id, message.from_user.username, message.from_user.full_name, user_id))
                            conn.commit()
                            
                            success_msg = (
                                f"🎉 **Аккаунт успешно привязан!**\n\n"
                                f"👤 Ваш логин на сайте: `{web_username}`\n"
                                f"🆔 Telegram ID: `{message.from_user.id}`\n"
                                f"📛 Имя: {message.from_user.full_name}\n"
                                f"🌐 Юзернейм: @{message.from_user.username or 'отсутствует'}\n\n"
                                f"Теперь вы будете получать уведомления о заказах и сможете сбросить пароль при необходимости."
                            )
                            await message.answer(success_msg, parse_mode="Markdown", reply_markup=main_kb)
                            return
                        else:
                            await message.answer("❌ Ссылка для привязки устарела или недействительна. Сгенерируйте новую в профиле на сайте.")
                            return
            except Exception as e:
                logging.error(f"Ошибка привязки аккаунта: {e}")
                await message.answer("❌ Произошла ошибка при попытке привязать аккаунт.")
                return

        # СЦЕНАРИЙ 2: ОПЛАТА ТЕЛЕГРАМ ЗВЕЗДАМИ
        else:
            invoice_id = start_param
            invoice = get_invoice_info(invoice_id)
            if not invoice:
                await message.answer("❌ Данный счет на оплату не найден в базе данных.")
                return
                
            user_id, amount_rub, status = invoice
            if status == 'paid':
                await message.answer("✅ Этот инвойс уже был успешно оплачен.", reply_markup=main_kb)
                return
                
            stars_amount = math.ceil(amount_rub / STARS_RATE_RUB)
            try:
                await bot.send_invoice(
                    chat_id=message.chat.id,
                    title="Пополнение баланса Vest SMM",
                    description=f"Оплата инвойса #{invoice_id} на сайте. Сумма: {amount_rub} ₽",
                    payload=invoice_id,
                    provider_token="",  
                    currency="XTR",     
                    prices=[LabeledPrice(label="Telegram Stars", amount=stars_amount)]
                )
            except Exception as e:
                logging.error(f"Ошибка выставления счета в Stars: {e}")
                await message.answer("❌ Не удалось сгенерировать счет в Telegram Stars. Обратитесь в поддержку.")
            return

    welcome_text = (
        f"👋 Приветствуем в Vest SMM\n\n"
        f"Мы рады видеть вас в нашем боте поддержки и платежей.\n"
        f"Управление заказами, просмотр каталога и авторизация проходят на сайте.\n\n"
        f"🔗 Наш сайт: {WEBSITE_URL}"
    )
    await message.answer(welcome_text, reply_markup=main_kb)

# =====================================================================
# КОМАНДА СБРОСА ПАРОЛЯ (БЕЗ ВВОДА СТАРОГО)
# =====================================================================
@dp.message(Command("resetpassword"))
async def cmd_reset_password_tg(message: types.Message, state: FSMContext):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, username FROM users WHERE telegram_id = %s", (message.from_user.id,))
                user_data = cursor.fetchone()
                
                if not user_data:
                    await message.answer("❌ Этот Telegram-аккаунт не привязан ни к одному профилю на сайте.\nСначала выполните привязку в настройках личного кабинета.")
                    return
                
                await state.update_data(web_user_id=user_data[0], web_username=user_data[1])
                await state.set_state(PasswordResetStates.waiting_for_new_password)
                await message.answer(f"🔑 Найдена привязанная учетная запись: `{user_data[1]}`.\n\nВведите **Новый Пароль** для входа на сайт (не менее 6 символов):")
    except Exception as e:
        logging.error(f"Ошибка при вызове /resetpassword: {e}")
        await message.answer("❌ Ошибка связи с сервером базы данных.")

@dp.message(PasswordResetStates.waiting_for_new_password)
async def process_new_password_tg(message: types.Message, state: FSMContext):
    new_pwd = message.text.strip()
    if len(new_pwd) < 6:
        await message.answer("❌ Пароль слишком короткий (минимум 6 символов). Введите другой вариант:")
        return
        
    state_data = await state.get_data()
    user_id = state_data['web_user_id']
    web_username = state_data['web_username']
    
    hashed_password = generate_password_hash(new_pwd)
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hashed_password, user_id))
                conn.commit()
                
        await state.clear()
        await message.answer(f"✅ Пароль для аккаунта `{web_username}` успешно обновлен!\n\nИспользуйте его для входа на {WEBSITE_URL}.")
    except Exception as e:
        logging.error(f"Ошибка сохранения нового пароля через бот: {e}")
        await message.answer("❌ Ошибка обновления пароля. Попробуйте позже.")

# =====================================================================
# ПРОВЕРКА И ПРОВЕДЕНИЕ ТРАНЗАКЦИЙ С ТЕЛЕГРАМ ЗВЕЗДАМИ
# =====================================================================
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    invoice_id = pre_checkout_query.invoice_payload
    invoice = get_invoice_info(invoice_id)
    if invoice and invoice[2] == 'active':
        await pre_checkout_query.answer(ok=True)
    else:
        await pre_checkout_query.answer(ok=False, error_message="Этот инвойс уже недействителен.")

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    invoice_id = message.successful_payment.invoice_payload
    invoice = get_invoice_info(invoice_id)
    if not invoice:
        await message.answer("⚠ Ошибка: Инвойс оплачен, но не найден в системе. Обратитесь к администратору.")
        return
        
    user_id, amount_rub, _ = invoice
    if credit_user_balance(invoice_id, user_id, amount_rub):
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Вернуться на сайт", url=WEBSITE_URL)]])
        await message.answer(f"🎉 Баланс успешно пополнен на {amount_rub} ₽!\nСредства зачислены на ваш аккаунт.", reply_markup=back_kb)
    else:
        await message.answer("⚠ Транзакция прошла, но баланс уже был пополнен ранее.")

# =====================================================================
# ЗАПУСК СЛУЖБЫ БОТА
# =====================================================================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
