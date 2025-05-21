import os
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sqlalchemy import create_engine, Column, Integer, String, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

from twilio.rest import Client as TwilioClient
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv

# Load settings
load_dotenv()
API_TOKEN = os.getenv('TELEGRAM_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
DATABASE_URL = os.getenv('DATABASE_URL')

# Logging
logging.basicConfig(level=logging.INFO)

# Bot and dispatcher
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Database setup
BASE_DIR = Path(__file__).resolve().parent
if DATABASE_URL:
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    db_path = BASE_DIR / 'bot.db'
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)

class Vacancy(Base):
    __tablename__ = 'vacancies'
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    city = Column(String, nullable=False)

class Toggle(Base):
    __tablename__ = 'toggles'
    name = Column(String, primary_key=True)
    enabled = Column(Boolean, default=True)

class Blacklist(Base):
    __tablename__ = 'blacklist'
    user_id = Column(Integer, primary_key=True)

Base.metadata.create_all(engine)

# SMS and Email with error handling
TWILIO_SID = os.getenv('TWILIO_SID')
TWILIO_TOKEN = os.getenv('TWILIO_TOKEN')
def send_sms(to_number: str, body: str):
    if TWILIO_SID and TWILIO_TOKEN:
        try:
            client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
            client.messages.create(body=body, to=to_number, from_=os.getenv('TWILIO_FROM'))
        except Exception as e:
            logging.error(f"Error sending SMS notification: {e}")

SMTP_HOST = os.getenv('SMTP_HOST')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USER = os.getenv('SMTP_USER')
SMTP_PASS = os.getenv('SMTP_PASS')
def send_email(to_email: str, subject: str, body: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        logging.warning("SMTP not fully configured, skipping email notification")
        return
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SMTP_USER
        msg['To'] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    except Exception as e:
        logging.error(f"Error sending email to {to_email}: {e}")

# Keyboards
def build_main_menu() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='Центр Карьеры', callback_data='evt_career'),
        types.InlineKeyboardButton(text='Практика', callback_data='evt_practice'),
    )
    builder.row(
        types.InlineKeyboardButton(text='Вакансии', callback_data='all_vacancies'),
        types.InlineKeyboardButton(text='Откликнуться', callback_data='noop'),
    )
    return builder.as_markup()

async def build_event_menu(key: str, description: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='Откликнуться', callback_data=f'respond_evt_{key}'),
        types.InlineKeyboardButton(text='Главное меню', callback_data='main'),
    )
    return description, builder.as_markup()

async def build_vacancy_list(page: int):
    per_page = 5
    with SessionLocal() as db:
        total = db.query(Vacancy).count()
        vacs = db.query(Vacancy).offset(page * per_page).limit(per_page).all()

    builder = InlineKeyboardBuilder()
    for v in vacs:
        builder.button(text=v.title, callback_data=f'vac_{v.id}')
    builder.adjust(1)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton(text='Назад', callback_data=f'vac_page_{page-1}'))
    if (page + 1) * per_page < total:
        nav_buttons.append(types.InlineKeyboardButton(text='Далее', callback_data=f'vac_page_{page+1}'))
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(types.InlineKeyboardButton(text='Главное меню', callback_data='main'))

    return 'Список вакансий:', builder.as_markup()

async def build_vacancy_detail(vac: Vacancy):
    text = f"{vac.title}\n{vac.description}\nГород: {vac.city}"
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text='Откликнуться', callback_data=f'respond_vac_{vac.id}'),
        types.InlineKeyboardButton(text='Главное меню', callback_data='main'),
    )
    return text, builder.as_markup()

# Handlers
async def cmd_start(message: types.Message):
    with SessionLocal() as db:
        if db.query(Blacklist).filter_by(user_id=message.from_user.id).first():
            return
    await message.answer('Добро пожаловать! Выберите опцию:', reply_markup=build_main_menu())

async def evt_handler(callback: types.CallbackQuery):
    key = 'career' if callback.data == 'evt_career' else 'practice'
    with SessionLocal() as db:
        tog = db.query(Toggle).filter_by(name=key).first()
        if not tog or not tog.enabled:
            await callback.answer('Временно недоступно', show_alert=True)
            return
    description = f"Описание для {'Центр Карьеры' if key == 'career' else 'Практика'}"
    text, markup = await build_event_menu(key, description)
    await callback.message.edit_text(text, reply_markup=markup)

async def list_vacancies(callback: types.CallbackQuery):
    text, markup = await build_vacancy_list(page=0)
    await callback.message.edit_text(text, reply_markup=markup)

async def vac_page_handler(callback: types.CallbackQuery):
    page = int(callback.data.split('_')[-1])
    text, markup = await build_vacancy_list(page=page)
    await callback.message.edit_text(text, reply_markup=markup)

async def vac_detail(callback: types.CallbackQuery):
    vid = int(callback.data.split('_')[1])
    with SessionLocal() as db:
        vac = db.get(Vacancy, vid)
    text, markup = await build_vacancy_detail(vac)
    await callback.message.edit_text(text, reply_markup=markup)

async def respond_handler(callback: types.CallbackQuery):
    # Acknowledge the callback to allow UI update
    await callback.answer()
    parts = callback.data.split('_')
    mode, key = parts[1], parts[2]
    with SessionLocal() as db:
        if db.query(Blacklist).filter_by(user_id=callback.from_user.id).first():
            return
        title = key if mode == 'evt' else db.get(Vacancy, int(key)).title

    user = callback.from_user
    notif = (
        f"Новый отклик:\n"
        f"username: @{user.username}\n"
        f"Вакансия: {title}\n"
        f"Имя: {user.full_name}"
    )
    # Send notifications
    await bot.send_message(ADMIN_ID, notif)
    send_sms(os.getenv('ADMIN_PHONE'), notif)
    send_email(os.getenv('ADMIN_EMAIL'), 'Новый отклик', notif)

    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text='Главное меню', callback_data='main'))
    await callback.message.edit_text('Спасибо! Ваш отклик отправлен.', reply_markup=builder.as_markup())

async def to_main(callback: types.CallbackQuery):
    await callback.message.edit_text('Главное меню:', reply_markup=build_main_menu())

async def noop_handler(callback: types.CallbackQuery):
    await callback.answer(
        "Пожалуйста, сначала выберите вакансию или ивент, а потом нажмите «Откликнуться».",
        show_alert=True
    )

# Admin handlers
async def cmd_addvac(message: types.Message):
    try:
        _, data = message.text.split(' ', 1)
        title, desc, city = [x.strip() for x in data.split('|')]
        with SessionLocal() as db:
            db.add(Vacancy(title=title, description=desc, city=city))
            db.commit()
        await message.reply('Вакансия добавлена.')
    except:
        await message.reply('Использование: /addvac Название|Описание|Город')

async def cmd_toggle(message: types.Message):
    try:
        _, name = message.text.split(' ', 1)
        with SessionLocal() as db:
            tog = db.get(Toggle, name)
            if not tog:
                tog = Toggle(name=name, enabled=False)
                db.add(tog)
            tog.enabled = not tog.enabled
            db.commit()
        await message.reply(f"{name} = {tog.enabled}")
    except:
        await message.reply('Использование: /toggle career или practice')

async def cmd_blacklist(message: types.Message):
    try:
        _, uid = message.text.split(' ', 1)
        uid = int(uid)
        with SessionLocal() as db:
            db.add(Blacklist(user_id=uid))
            db.commit()
        await message.reply(f"Пользователь {uid} в чёрном списке")
    except:
        await message.reply('Использование: /blacklist USER_ID')

# Register handlers
dp.message.register(cmd_start, Command("start"))
dp.callback_query.register(evt_handler, lambda c: c.data in ("evt_career", "evt_practice"))
dp.callback_query.register(list_vacancies, lambda c: c.data == "all_vacancies")
dp.callback_query.register(vac_page_handler, lambda c: c.data.startswith("vac_page_"))
dp.callback_query.register(vac_detail, lambda c: c.data.startswith("vac_") and not c.data.startswith("vac_page_"))
dp.callback_query.register(respond_handler, lambda c: c.data.startswith("respond_"))
dp.callback_query.register(to_main, lambda c: c.data == "main")
dp.callback_query.register(noop_handler, lambda c: c.data == "noop")
dp.message.register(cmd_addvac, lambda m: m.from_user.id == ADMIN_ID and m.text.startswith("/addvac"))
dp.message.register(cmd_toggle, lambda m: m.from_user.id == ADMIN_ID and m.text.startswith("/toggle"))
dp.message.register(cmd_blacklist, lambda m: m.from_user.id == ADMIN_ID and m.text.startswith("/blacklist"))

# Start polling
if __name__ == '__main__':
    dp.run_polling(bot, skip_updates=True)
