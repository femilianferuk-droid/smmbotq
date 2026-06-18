import os
import logging
import asyncio
import math
import time
import psycopg2
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    LabeledPrice, 
    PreCheckoutQuery, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from werkzeug.security import generate_password_hash

# =====================================================================
# КОНФИГУРАЦИЯ
# =====================================================================
TG_BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_URL = os.environ.get("DATABASE_URL")
STARS_RATE_RUB = 0.7
WEBSITE_URL = "https://vestsmm.shop"
ADMIN_ID = 7973988177

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()

# =====================================================================
# СОСТОЯНИЯ FSM
# =====================================================================
class PasswordResetStates(StatesGroup):
    waiting_for_new_password = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast_msg = State()
    waiting_for_media_file = State()

class UserStates(StatesGroup):
    waiting_for_topup_amount = State()

# =====================================================================
# ВЗАИМОДЕЙСТВИЕ С БАЗОЙ ДАННЫХ
# =====================================================================
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_bot_db():
    """Создает таблицу динамических настроек оформления бота."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS bot_settings (
                        key VARCHAR(50) PRIMARY KEY,
                        value TEXT
                    );
                """)
                conn.commit()
    except Exception as e:
        logging.error(f"Ошибка создания таблицы bot_settings: {e}")

def get_setting(key: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT value FROM bot_settings WHERE key = %s", (key,))
                res = cursor.fetchone()
                return res[0] if res else None
    except Exception:
        return None

def set_setting(key: str, value: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO bot_settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (key, value))
                conn.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка записи настройки {key}: {e}")
        return False

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
                    return True
                return False
    except Exception as e:
        logging.error(f"Ошибка при обновлении баланса {invoice_id}: {e}")
        return False

def get_user_by_tg_id(tg_id: int):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, username, balance, tg_notifications FROM users WHERE telegram_id = %s", (tg_id,))
                return cursor.fetchone()
    except Exception:
        return None

# =====================================================================
# ШАБЛОНИЗАТОР ДИНАМИЧЕСКИХ МЕДИА-СООБЩЕНИЙ
# =====================================================================
async def send_templated_msg(chat_id: int, text: str, reply_markup, setting_key: str):
    """Отправляет текст с прикрепленным фото/видео из админ-настроек, либо обычным текстом."""
    media_val = get_setting(setting_key)
    if media_val and ":" in media_val:
        try:
            m_type, file_id = media_val.split(":", 1)
            if m_type == "photo":
                await bot.send_photo(chat_id, photo=file_id, caption=text, reply_markup=reply_markup, parse_mode="HTML")
                return
            elif m_type == "video":
                await bot.send_video(chat_id, video=file_id, caption=text, reply_markup=reply_markup, parse_mode="HTML")
                return
        except Exception as e:
            logging.error(f"Сбой отправки медиа шаблона {setting_key}: {e}")
            
    await bot.send_message(chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")

# =====================================================================
# КЛАВИАТУРЫ
# =====================================================================
def get_main_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🌐 На сайт")],
            [KeyboardButton(text="🔑 Сбросить пароль")]
        ],
        resize_keyboard=True
    )

# =====================================================================
# ГЛАВНЫЕ ХЕНДЛЕРЫ КЛИЕНТСКОЙ ЧАСТИ
# =====================================================================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    args = command.args
    site_inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть сайт", url=WEBSITE_URL)]
    ])

    if args:
        start_param = args.strip()
        
        # Сценарий 1: Регистрационная связка аккаунтов
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
                                f"🎉 <b>Аккаунт успешно привязан!</b>\n\n"
                                f"👤 Ваш логин на сайте: {web_username}\n"
                                f"🆔 Telegram ID: {message.from_user.id}\n"
                                f"📛 Имя: {message.from_user.full_name}\n"
                                f"🌐 Юзернейм: @{message.from_user.username or 'отсутствует'}\n\n"
                                f"Теперь вам доступны пуш-уведомления, а баланс отображается прямо в меню!"
                            )
                            await message.answer(success_msg, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
                            return
                        else:
                            await message.answer("❌ Ссылка для привязки устарела. Сгенерируйте новую в профиле на сайте.", reply_markup=get_main_menu_keyboard())
                            return
            except Exception as e:
                logging.error(f"Ошибка привязки аккаунта: {e}")
                await message.answer("❌ Произошла ошибка при попытке привязать аккаунт.", reply_markup=get_main_menu_keyboard())
                return

        # Сценарий 2: Оплата инвойса в Звездах, сгенерированного сайтом
        else:
            invoice_id = start_param
            invoice = get_invoice_info(invoice_id)
            if not invoice:
                await message.answer("❌ Данный счет на оплату не найден в базе данных.", reply_markup=get_main_menu_keyboard())
                return
                
            user_id, amount_rub, status = invoice
            if status == 'paid':
                await message.answer("✅ Этот инвойс уже был успешно оплачен.", reply_markup=site_inline_kb)
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
                await message.answer("❌ Не удалось сгенерировать счет в Telegram Stars.", reply_markup=get_main_menu_keyboard())
            return

    welcome_text = (
        f"👋 Приветствуем в автоматическом сервисе продвижения Vest SMM!\n\n"
        f"Здесь вы можете проверять баланс личного кабинета, оплачивать счета и управлять безопасностью профиля.\n\n"
        f"Используйте нижнее меню для навигации по системе."
    )
    await send_templated_msg(message.chat.id, welcome_text, get_main_menu_keyboard(), "media_main_menu")

@dp.message(F.text == "👤 Мой профиль")
@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_data = get_user_by_tg_id(message.from_user.id)
    
    if not user_data:
        await message.answer(
            f"❌ Ваш Telegram-аккаунт еще не привязан к профилю на сайте.\n\n"
            f"Чтобы привязать его, зайдите на сайт, перейдите в раздел Профиль и нажмите кнопку «Привязать аккаунт Telegram»."
        )
        return
        
    user_id, username, balance, notifications = user_data
    notif_status = "✅ Включены" if notifications else "❌ Выключены"
    
    profile_text = (
        f"👤 <b>Ваш профиль на Vest SMM</b>\n\n"
        f"🔑 Логин: {username}\n"
        f"🆔 ID аккаунта: #{user_id}\n"
        f"💰 <b>Баланс на сайте: {balance:.2f} ₽</b>\n"
        f"🔔 Уведомления о заказах: {notif_status}\n\n"
        f"Вы можете пополнить баланс звездами прямо здесь, нажав на кнопку ниже."
    )
    
    profile_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить баланс звёздами", callback_data="user_topup_stars")],
        [InlineKeyboardButton(text="🌐 Перейти в панель накрутки", url=WEBSITE_URL)]
    ])
    await send_templated_msg(message.chat.id, profile_text, profile_kb, "media_profile")

@dp.message(F.text == "🌐 На сайт")
async def msg_to_site(message: types.Message):
    site_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Перейти", url=WEBSITE_URL)]
    ])
    await send_templated_msg(message.chat.id, "Перейти к полному каталогу услуг и заказам на сайте:", site_kb, "media_site")

# =====================================================================
# ПОПОЛНЕНИЕ БАЛАНСА ВНУТРИ БОТА
# =====================================================================
@dp.callback_query(F.data == "user_topup_stars")
async def callback_user_topup(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserStates.waiting_for_topup_amount)
    
    prompt_text = (
        "💳 <b>Пополнение баланса Telegram Stars</b>\n\n"
        "Введите желаемую сумму пополнения в <b>рублях</b> (минимум 10 ₽).\n"
        "Система автоматически пересчитает её в Звёзды при выставлении счета."
    )
    await send_templated_msg(callback.message.chat.id, prompt_text, None, "media_topup")

@dp.message(UserStates.waiting_for_topup_amount)
async def process_user_topup_invoice(message: types.Message, state: FSMContext):
    amount_text = message.text.strip()
    if not amount_text.isdigit() or float(amount_text) < 10:
        await message.answer("❌ Некорректная сумма. Введите целое число рублей от 10 ₽:")
        return
        
    amount_rub = float(amount_text)
    await state.clear()
    
    user_data = get_user_by_tg_id(message.from_user.id)
    if not user_data:
        await message.answer("❌ Ошибка профиля. Аккаунт не привязан.")
        return
        
    user_id = user_data[0]
    invoice_id_str = f"bot{int(time.time())}{user_id}"
    
    # Регестрируем инвойс в общей базе данных
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO invoices (user_id, invoice_id, gateway, amount_rub, status) VALUES (%s, %s, %s, %s, 'active')",
                    (user_id, invoice_id_str, "stars", amount_rub)
                )
                conn.commit()
    except Exception as e:
        logging.error(f"Ошибка логирования инвойса в боте: {e}")
        await message.answer("❌ Ошибка генерации счета в базе данных.")
        return

    stars_amount = math.ceil(amount_rub / STARS_RATE_RUB)
    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="Пополнение Vest SMM",
            description=f"Прямое пополнение баланса личного кабинета на сумму {amount_rub} ₽",
            payload=invoice_id_str,
            provider_token="",  
            currency="XTR",     
            prices=[LabeledPrice(label="Telegram Stars", amount=stars_amount)]
        )
    except Exception as e:
        logging.error(f"Ошибка отправки инвойса: {e}")
        await message.answer("❌ Критическая ошибка платежного шлюза Telegram.")

# =====================================================================
# СБРОС ПАРОЛЯ
# =====================================================================
@dp.message(Command("resetpassword"))
@dp.message(F.text == "🔑 Сбросить пароль")
async def cmd_reset_password_tg(message: types.Message, state: FSMContext):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, username FROM users WHERE telegram_id = %s", (message.from_user.id,))
                user_data = cursor.fetchone()
                
                if not user_data:
                    await message.answer("❌ Этот Telegram-аккаунт не привязан к сайту.")
                    return
                
                await state.update_data(web_user_id=user_data[0], web_username=user_data[1])
                await state.set_state(PasswordResetStates.waiting_for_new_password)
                await message.answer(f"🔑 Найдена запись: {user_data[1]}.\n\nВведите новый пароль для входа на сайт (от 6 символов):")
    except Exception as e:
        await message.answer("❌ Ошибка связи с базой данных.")

@dp.message(PasswordResetStates.waiting_for_new_password)
async def process_new_password_tg(message: types.Message, state: FSMContext):
    new_pwd = message.text.strip()
    if len(new_pwd) < 6:
        await message.answer("❌ Пароль слишком короткий. Введите пароль от 6 символов:")
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
        await message.answer(f"✅ Пароль для аккаунта {web_username} успешно обновлен!")
    except Exception:
        await message.answer("❌ Ошибка записи в базу данных.")

# =====================================================================
# АДМИН-ПАНЕЛЬ И НАСТРОЙКА МЕДИА ОФОРМЛЕНИЯ
# =====================================================================
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
        
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика проекта", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Создать общую рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🖼 Настройка медиа разделов", callback_data="admin_media_setup")]
    ])
    await message.answer("Добро пожаловать в панель управления Vest SMM!", reply_markup=admin_kb)

@dp.callback_query(F.data == "admin_media_setup")
async def callback_media_setup(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    
    media_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Главное меню", callback_data="setmedia_media_main_menu")],
        [InlineKeyboardButton(text="Личный профиль", callback_data="setmedia_media_profile")],
        [InlineKeyboardButton(text="Вкладка На сайт", callback_data="setmedia_media_site")],
        [InlineKeyboardButton(text="Пополнение баланса", callback_data="setmedia_media_topup")],
        [InlineKeyboardButton(text="⬅ Назад в меню", callback_data="admin_back")]
    ])
    await callback.message.edit_text("Выберите раздел, для которого хотите настроить или сменить медиа-заставку:", reply_markup=media_kb)

@dp.callback_query(F.data.startswith("setmedia_"))
async def callback_choose_media_target(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    
    target_key = callback.data.replace("setmedia_", "")
    await state.update_data(target_key=target_key)
    await state.set_state(AdminStates.waiting_for_media_file)
    
    await callback.message.edit_text(
        f"Отправьте мне <b>Фотографию</b> или <b>Видеоролик</b>, который закрепится за этим разделом.\n\n"
        f"Чтобы сбросить оформление и вернуть стандартный текстовый режим, отправьте сообщение с текстом: <code>удалить</code>"
    , parse_mode="HTML")

@dp.message(AdminStates.waiting_for_media_file)
async def process_save_media_file(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
        
    state_data = await state.get_data()
    target_key = state_data['target_key']
    await state.clear()
    
    if message.text and message.text.strip().lower() == "удалить":
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("DELETE FROM bot_settings WHERE key = %s", (target_key,))
                    conn.commit()
            await message.answer("✅ Медиа-оформление для раздела успешно удалено. Возвращен текстовый режим.")
        except Exception:
            await message.answer("Ошибка при удалении настройки.")
        return

    media_value = None
    if message.photo:
        media_value = f"photo:{message.photo[-1].file_id}"
    elif message.video:
        media_value = f"video:{message.video.file_id}"
        
    if not media_value:
        await message.answer("❌ Ошибка: Вы должны отправить именно фото или видео. Процесс отменен.")
        return
        
    if set_setting(target_key, media_value):
        await message.answer("✅ Новое медиа-оформление раздела успешно сохранено и применено!")
    else:
        await message.answer("❌ Ошибка записи конфигурации.")

@dp.callback_query(F.data == "admin_stats")
async def callback_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
        
    with get_db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE telegram_id IS NOT NULL")
            linked_users = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount_rub), 0) FROM invoices WHERE status = 'paid'")
            paid_invoices, total_earned = cursor.fetchone()
            cursor.execute("SELECT COUNT(*), COALESCE(SUM(charge), 0) FROM orders")
            total_orders, total_spent = cursor.fetchone()
            
    stats_text = (
        f"📊 Статистика Vest SMM\n\n"
        f"Пользователей зарегистрировано: {total_users} чел.\n"
        f"Привязали Telegram-аккаунт: {linked_users} чел.\n\n"
        f"Всего создано заказов: {total_orders} шт.\n"
        f"Оборот по заказам провайдера: {total_spent:.2f} ₽\n\n"
        f"Успешных пополнений баланса: {paid_invoices} шт.\n"
        f"Сумма чистых поступлений: {total_earned:.2f} ₽"
    )
    
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅ Назад в меню", callback_data="admin_back")]])
    await callback.message.edit_text(stats_text, reply_markup=back_kb)

@dp.callback_query(F.data == "admin_broadcast")
async def callback_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
        
    await state.set_state(AdminStates.waiting_for_broadcast_msg)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_back")]])
    await callback.message.edit_text(
        "Отправьте сообщение для рассылки. Поддерживаются:\n"
        "- Одиночный текст, форматированный HTML-тегами\n"
        "- Фотография с описанием (HTML)\n"
        "- Видеоролик с описанием (HTML)",
        reply_markup=back_kb
    )

@dp.callback_query(F.data == "admin_back")
async def callback_admin_back(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
        
    await state.clear()
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика проекта", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Создать общую рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🖼 Настройка медиа разделов", callback_data="admin_media_setup")]
    ])
    await callback.message.edit_text("Добро пожаловать в панель управления Vest SMM!", reply_markup=admin_kb)

# ИСПРАВЛЕНО: Полная поддержка HTML, Фото, Видео и шрифтов в рассылке
@dp.message(AdminStates.waiting_for_broadcast_msg)
async def process_admin_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
        
    broadcast_text = message.html_text  # Сохраняет встроенный HTML стиль текста/описания
    media_photo = message.photo[-1].file_id if message.photo else None
    media_video = message.video.file_id if message.video else None
    
    await state.clear()
    
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DISTINCT telegram_id FROM users WHERE telegram_id IS NOT NULL")
                rows = cursor.fetchall()
    except Exception as e:
        await message.answer("Ошибка базы данных.")
        return

    if not rows:
        await message.answer("Нет пользователей для отправки.")
        return

    await message.answer(f"Запуск медиа-рассылки на {len(rows)} аккаунтов...")
    success_count, fail_count = 0, 0
    
    for row in rows:
        tg_id = row[0]
        try:
            if media_photo:
                await bot.send_photo(chat_id=tg_id, photo=media_photo, caption=broadcast_text, parse_mode="HTML")
            elif media_video:
                await bot.send_video(chat_id=tg_id, video=media_video, caption=broadcast_text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=tg_id, text=broadcast_text, parse_mode="HTML")
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1
            
    await message.answer(
        f"Рассылка полностью завершена!\n\n"
        f"Успешно доставлено: {success_count}\n"
        f"Не удалось доставить: {fail_count}"
    )

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
        await message.answer("⚠ Критическая ошибка зачисления средств.")
        return
        
    user_id, amount_rub, _ = invoice
    if credit_user_balance(invoice_id, user_id, amount_rub):
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Вернуться на сайт", url=WEBSITE_URL)]])
        await message.answer(f"🎉 Баланс успешно пополнен на {amount_rub} ₽!", reply_markup=back_kb)
    else:
        await message.answer("⚠ Транзакция прошла, но баланс уже был пополнен ранее.")

# =====================================================================
# ЗАПУСК
# =====================================================================
async def main():
    init_bot_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
