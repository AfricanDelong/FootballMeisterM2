import asyncio
import logging
import random
import uuid
import json
import os
import re
import html
from datetime import datetime
from typing import Dict, List, Optional
from enum import Enum

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    CallbackQuery, Message, FSInputFile,
    BotCommand, BotCommandScopeDefault,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# ================ КОНФИГУРАЦИЯ ================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN env var is not set.")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)



logger = logging.getLogger("FootballCollector")

async def safe_edit_or_send(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    """Safely edits message text/caption or sends a new one if Telegram doesn't allow editing."""
    try:
        # If message has text (regular message), edit_text works
        if getattr(message, "text", None) is not None:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
            return
        # If this is a photo/caption message
        if getattr(message, "caption", None) is not None:
            await message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode="HTML")
            return
        # Fallback: can't edit -> send new
        await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        # Common: 'there is no text in the message to edit' / 'message is not modified' / etc.
        try:
            if getattr(message, "caption", None) is not None:
                await message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode="HTML")
                return
        except Exception:
            pass
        await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")

async def send_page(
    message: Message,
    *,
    image_basename: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """Отправляет страницу как фото+caption (если есть фон) или обычным сообщением."""
    img_path = get_existing_image_path(image_basename) if "get_existing_image_path" in globals() else None
    if img_path and os.path.exists(img_path):
        await message.answer_photo(
            photo=FSInputFile(img_path),
            caption=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    else:
        await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")



# file_id анимированных стикеров/анимаций для мини-игр (укажи свои)
# Можно задать общий MINIGAME_STICKER_FILE_ID, а можно отдельные для каждой игры.
MINIGAME_STICKER_FILE_ID = os.getenv("MINIGAME_STICKER_FILE_ID", "")
MINIGAME_STICKER_BASKETBALL_FILE_ID = os.getenv("MINIGAME_STICKER_BASKETBALL_FILE_ID", MINIGAME_STICKER_FILE_ID)
MINIGAME_STICKER_DARTS_FILE_ID = os.getenv("MINIGAME_STICKER_DARTS_FILE_ID", MINIGAME_STICKER_FILE_ID)
MINIGAME_STICKER_BOWLING_FILE_ID = os.getenv("MINIGAME_STICKER_BOWLING_FILE_ID", MINIGAME_STICKER_FILE_ID)

# ================ ПУТИ К КАРТИНКАМ ================
IMAGES_PATH = "images"
BACKGROUND_IMAGE_FILENAME = "backgrauond.png"
PROFILE_IMAGE_BASENAME = "profile"
os.makedirs(IMAGES_PATH, exist_ok=True)

def get_existing_image_path(basename: str) -> str | None:
    """Ищет файл изображения по базовому имени в папке images.
    Поддерживаемые расширения: png, jpg, jpeg, webp.
    """
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = os.path.join(IMAGES_PATH, f"{basename}.{ext}")
        if os.path.exists(p):
            return p
    p2 = os.path.join(IMAGES_PATH, basename)
    if os.path.exists(p2):
        return p2
    return None


async def render_page(
    callback: CallbackQuery,
    *,
    image_basename: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    force_new_message: bool = False,
):
    """Показывает страницу как:
    - фото + подпись (если images/<basename>.* существует)
    - иначе как обычный текст.
    Умеет безопасно работать с edit_text/edit_caption, чтобы не падать на сообщениях без текста.
    """
    img_path = get_existing_image_path(image_basename)
    if img_path:
        # Для простоты и надёжности: удаляем исходное сообщение и шлём новое с фоном
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer_photo(
            FSInputFile(img_path),
            caption=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return

    # Без фона — редактируем текущий месседж, где возможно
    if force_new_message:
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        return

    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        # Частый кейс: сообщение с фото без текста — тогда пытаемся edit_caption
        if "no text in the message to edit" in str(e) and getattr(callback.message, "caption", None) is not None:
            try:
                await callback.message.edit_caption(text, reply_markup=reply_markup, parse_mode="HTML")
                return
            except TelegramBadRequest:
                pass
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")


CARD_LIFETIME_SECONDS = 5

# ================ НОРМАЛИЗАЦИЯ РЕДКОСТИ ================
RARITY_ALIASES = {
    "common": "common", "обычная": "common", "обыкновенная": "common", "обычный": "common",
    "rare": "rare", "редкая": "rare", "редкий": "rare",
    "epic": "epic", "эпическая": "epic", "эпик": "epic",
    "legendary": "legendary", "легендарная": "legendary", "лега": "legendary",
    "mythic": "mythic", "мифическая": "mythic", "мифик": "mythic",
    "candy": "candy", "конфетная": "candy", "конфетный": "candy", "🍬": "candy",
}

async def send_minigame_sticker(
    chat_id: int,
    *,
    file_id: str,
    reply_to_message_id: int | None = None,
) -> Message | None:
    """Отправляет анимированный стикер/анимацию для мини-игр и возвращает Message.
    Если file_id пустой, ничего не отправляет.
    """
    if not file_id:
        return None
    try:
        return await bot.send_sticker(chat_id, sticker=file_id, reply_to_message_id=reply_to_message_id)
    except Exception:
        return None
    except Exception:
        # если передали не sticker file_id, пробуем как animation
        try:
            return await bot.send_animation(chat_id, MINIGAME_STICKER_FILE_ID, reply_to_message_id=reply_to_message_id)
        except Exception:
            return None

async def delete_message_safely(msg: Message | None, delay: float = 0):
    if not msg:
        return
    if delay:
        await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass



def normalize_rarity(value: str) -> str:
    if not value:
        return "common"
    v = str(value).strip().lower()
    v = re.sub(r"[🟢🔵🟣👑🤍💎]+", "", v).strip()
    return RARITY_ALIASES.get(v, v)

# ================ ЗАГРУЗКА БАЗЫ ИГРОКОВ ================
def load_players():
    try:
        with open('characters.json', 'r', encoding='utf-8') as f:
            players = json.load(f)
        logging.info(f"✅ Загружено {len(players)} карточек игроков")
        return players
    except Exception as e:
        logging.error(f"❌ Ошибка загрузки characters.json: {e}")
        return [
            {
                "id": 0,
                "name_ru": "Тестовый игрок",
                "name_en": "Test Player",
                "rarity": "common",
                "rarity_name_ru": "Обычная",
                "rarity_name_en": "Common",
                "country_ru": "🌍",
                "country_en": "🌍",
                "position_ru": "Игрок",
                "position_en": "Player",
                "ovr": 70,
                "description_ru": "База данных не загружена",
                "description_en": "Database not loaded",
                "image": None
            }
        ]

FOOTBALL_PLAYERS = load_players()

# ================ КЕШ ИЗОБРАЖЕНИЙ ================
IMAGE_CACHE = {}
TG_FILE_ID_CACHE = {}

def get_card_media(card: dict) -> Optional[str | FSInputFile]:
    """Умная загрузка картинки с 3 уровнями кеша."""
    if not card.get("image"):
        return None
    
    filename = card["image"]
    
    if card.get("tg_file_id"):
        return card["tg_file_id"]
    
    if filename in TG_FILE_ID_CACHE:
        card["tg_file_id"] = TG_FILE_ID_CACHE[filename]
        return TG_FILE_ID_CACHE[filename]
    
    if filename in IMAGE_CACHE:
        return IMAGE_CACHE[filename]
    
    image_path = os.path.join(IMAGES_PATH, filename)
    if os.path.exists(image_path):
        media = FSInputFile(image_path)
        IMAGE_CACHE[filename] = media
        return media
    
    return None

async def save_tg_file_id(card: dict, message: types.Message):
    """Сохраняет file_id из отправленного сообщения."""
    try:
        if message.photo:
            file_id = message.photo[-1].file_id
            card["tg_file_id"] = file_id
            if card.get("image"):
                TG_FILE_ID_CACHE[card["image"]] = file_id
            return True
    except Exception:
        pass
    return False

# ================ ФУНКЦИЯ ДЛЯ ОТОБРАЖЕНИЯ ИМЕНИ ================
def get_user_display_name(user) -> str:
    """Возвращает @username или ID, если username нет."""
    if user and hasattr(user, 'username') and user.username:
        return f"@{user.username}"
    elif user and hasattr(user, 'user_id'):
        return f"ID: {user.user_id}"
    return "Неизвестный игрок"

def build_profile_text(user: "UserData") -> str:
    """Строит текст профиля (используется и в /profile, и в кнопке)."""
    t = TRANSLATIONS[user.language]

    total = len(user.collection)
    common = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "common"])
    rare = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "rare"])
    epic = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "epic"])
    legendary = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "legendary"])
    mythic = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "mythic"])
    candy_count = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "candy"])

    display_name = get_user_display_name(user)

    if user.language == Language.RU:
        title = f"👤 <b>Профиль</b> {display_name}"
        balance = "Баланс"
        stats = "Статистика"
        collection_title = "Коллекция"
        text = (
            f"{title}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 <b>{balance}</b>\n"
            f"{t['coins']}: <b>{user.coins}</b> 🪙\n"
            f"{t['gems']}: <b>{user.gems}</b> 💎\n"            f"⭐ Stars: <b>{user.stars_balance}</b>\n"
            f"{t['candies']}: <b>{user.candies}</b> 🍬\n"
            f"━━━━━━━━━━━━━━\n"
            f"📈 <b>{stats}</b>\n"
            f"🏆 {t['elo']}: <b>{getattr(user, 'elo', 1000)}</b>\n"
            f"{t['packs_opened_total']}: <b>{getattr(user, 'packs_opened_total', 0)}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📚 <b>{collection_title}</b>\n"
            f"Всего: <b>{total}</b>\n"
            f"🟢 {t['sort_common']}: <b>{common}</b>\n"
            f"🔵 {t['sort_rare']}: <b>{rare}</b>\n"
            f"🟣 {t['sort_epic']}: <b>{epic}</b>\n"
            f"🟡 {t['sort_legendary']}: <b>{legendary}</b>\n"
            f"🔴 {t['sort_mythic']}: <b>{mythic}</b>\n"
        )
        # Конфетная редкость появляется только после получения
        if candy_count > 0:
            text += f"🍬 Конфетные: <b>{candy_count}</b>\n"
        return text
    else:
        title = f"👤 <b>Profile</b> {display_name}"
        balance = "Balance"
        stats = "Stats"
        collection_title = "Collection"
        text = (
            f"{title}\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 <b>{balance}</b>\n"
            f"{t['coins']}: <b>{user.coins}</b> 🪙\n"
            f"{t['gems']}: <b>{user.gems}</b> 💎\n"
            f"{t['candies']}: <b>{user.candies}</b> 🍬\n"
            f"━━━━━━━━━━━━━━\n"
            f"📈 <b>{stats}</b>\n"
            f"🏆 {t['elo']}: <b>{getattr(user, 'elo', 1000)}</b>\n"
            f"{t['packs_opened_total']}: <b>{getattr(user, 'packs_opened_total', 0)}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📚 <b>{collection_title}</b>\n"
            f"Total: <b>{total}</b>\n"
            f"🟢 {t['sort_common']}: <b>{common}</b>\n"
            f"🔵 {t['sort_rare']}: <b>{rare}</b>\n"
            f"🟣 {t['sort_epic']}: <b>{epic}</b>\n"
            f"🟡 {t['sort_legendary']}: <b>{legendary}</b>\n"
            f"🔴 {t['sort_mythic']}: <b>{mythic}</b>\n"
        )
        if candy_count > 0:
            text += f"🍬 Candy: <b>{candy_count}</b>\n"
        return text



# ================ СПЛАВКА ДУБЛИКАТОВ ================
RARITY_UPGRADE_MAP = {
    "common": "rare",
    "rare": "epic",
    "epic": "legendary",
    "legendary": "mythic",
}

# 🍬 Награда за сплавку (за 5 дубликатов)
# Рандом зависит от редкости. Максимум за одну сплавку — 60 🍬.
CANDY_REWARD_RANGES = {
    "common": (2, 6),
    "rare": (5, 12),
    "epic": (10, 22),
    "legendary": (18, 35),
    "mythic": (28, 60),
    "candy": (35, 60),
}

def get_candies_for_fuse(rarity: str) -> int:
    r = normalize_rarity(rarity)
    low, high = CANDY_REWARD_RANGES.get(r, (2, 6))
    gained = random.randint(low, high)
    return min(60, max(0, int(gained)))

def card_identity_key(card: dict):
    name = card.get("name_en") or card.get("name_ru") or card.get("name") or ""
    rarity = card.get("rarity", "common")
    return (name.strip().lower(), rarity)

def count_duplicates(collection: list, target_card: dict) -> int:
    key = card_identity_key(target_card)
    return sum(1 for c in collection if card_identity_key(c) == key)

# ================ FSM ДЛЯ ПОИСКА ================
class SearchStates(StatesGroup):
    waiting_for_query = State()



class StarsTopUpStates(StatesGroup):
    waiting_amount = State()


class ClanStates(StatesGroup):
    creating_name = State()
    creating_description = State()
    creating_privacy = State()
    inviting_username = State()
    setrole_username = State()
    setrole_role = State()

# ================ ЯЗЫКОВЫЕ НАСТРОЙКИ ================
class Language(Enum):
    RU = "ru"
    EN = "en"

TRANSLATIONS = {
    Language.RU: {
        "main_menu": "⚽ Футбольный Коллекционер",
        "packs": "📦 Паки",
        "collection": "📚 Коллекция",
        "mini_game": "🎲 Мини Игры",
        "settings": "⚙️ Настройки",
        "profile": "👤 Профиль",
        "battle_mode": "⚔️ Режим сражения",
        "coins": "💰 Монеты",
        "gems": "💎 Алмазы",
        "candies": "🍬 Конфеты",
        "stars": "⭐ Stars",
        "stars_balance": "⭐ Баланс Stars",
        "stars_shop": "⭐ Магазин Stars",
        "topup_stars": "➕ Пополнить Stars",
        "buy_diamonds_stars": "💎 Купить алмазы за Stars",
        "stars_topup_title": "⭐ Пополнение Stars",
        "stars_spend_title": "💎 Алмазы за Stars",
        "packs_opened_total": "📦 Открыто паков",
        "elo": "🏆 Elo",
        "candy_shop": "🍬 Конфетная лавка",
        "candy_shop_title": "🍬 Конфетная лавка",
        "clans": "🏟️ Кланы",
        "clans_title": "🏟️ Кланы",
        "rating": "🏆 Рейтинг",
        "rating_title": "🏆 Рейтинг",
        "rating_players": "🏅 Рейтинг игроков",
        "rating_clans": "🏆 Рейтинг кланов",
        "create_clan": "➕ Создать клан (100💎)",
        "join_open_clan": "🔎 Вступить в открытый",
        "clan_rating": "📋 Рейтинг кланов",
        "clan_invites": "📨 Приглашения",
        "clan_leave": "🚪 Покинуть клан",
        "clan_invite_member": "➕ Пригласить",
        "clan_set_role": "🎭 Выдать роль",
        "buy_candy_random": "🍬 Купить конфетную карточку",
        "candy_random_desc": "Особая карточка Конфетной редкости. Покупается за 🍬 конфеты!",
        "not_enough_candies": "❌ Недостаточно конфет!",
        "free_packs": "🎁 Бесплатные паки",
        "basic_pack": "📦 Обычный пак",
        "premium_pack": "💎 Премиум пак",
        "basic_pack_desc": "⚡ Шанс на редких, эпических и легендарных игроков!",
        "premium_pack_desc": "👑 Повышенный шанс на эпических и легендарных игроков!",
        "free_pack": "🎁 Бесплатный пак",
        "free_pack_desc": "Каждые 4 часа — 5 бесплатных паков!",
        "buy": "Купить",
        "not_enough_coins": "❌ Недостаточно монет!",
        "not_enough_gems": "❌ Недостаточно алмазов!",
        "new_card": "✨ НОВАЯ КАРТОЧКА! ✨",
        "card_received": "✨ Карточка добавлена в коллекцию! ✨",
        "rarity": "🌟 Редкость",
        "country": "🌍 Страна",
        "position": "⚽ Позиция",
        "ovr": "⚡ OVR",
        "acquired": "📅 Получена",
        "description": "📝 Описание",
        "back": "◀️ Назад",
        "back_to_menu": "◀️ В главное меню",
        "reset_progress": "🔄 Сбросить прогресс",
        "change_language": "🌐 Смена языка",
        "confirm_reset": "⚠️ Вы уверены, что хотите сбросить весь прогресс?",
        "yes": "✅ Да",
        "no": "❌ Нет",
        "progress_reset": "🔄 Прогресс сброшен!",
        "language_changed": "🌐 Язык изменен на русский",
        "empty_collection": "📭 Ваша коллекция пуста\nКупите паки, чтобы получить карточки!",
        "your_collection": "📋 ТВОЯ КОЛЛЕКЦИЯ",
        "card_number": "Карточка",
        "of": "из",
        "view_card": "👆 Просмотреть карточку",
        "search_card": "🔍 Поиск карточки",
        "search_prompt": "🔍 Введите имя или фамилию игрока для поиска:",
        "search_no_results": "❌ Карточки с таким именем не найдены",
        "search_too_many": "⚠️ Найдено более 50 карточек. Показаны первые 50.",
        "search_results": "🔍 Результаты поиска по запросу «{query}»:\n\n{results}\n\nНажмите на номер карточки, чтобы посмотреть её.",
        "search_cancel": "❌ Отмена поиска",
        "card_not_found": "❌ Карточка не найдена",
        "close": "❌ Закрыть",
        "fuse": "Сплавить",
        "sort_all": "Все",
        "sort_common": "Обычные",
        "sort_rare": "Редкие",
        "sort_epic": "Эпические",
        "sort_legendary": "Легендарные",
        "sort_mythic": "Мифические",
        "sort_candy": "Конфетные",
        "wins": "Победы",
        "losses": "Поражения",
        "total_games": "Всего игр",
        "free_packs_available": "🎁 Доступные бесплатные паки",
        "free_packs_count": "У вас есть {count} бесплатных паков",
        "free_packs_time": "Следующие паки через: {time}",
        "open_free_pack": "🎲 Открыть бесплатный пак",
        "free_pack_opened": "✅ Вы открыли бесплатный пак! Осталось: {remaining}",
        "no_free_packs": "❌ У вас нет бесплатных паков! Вернитесь через: {time}",
        "free_pack_timer": "⏰ Бесплатные паки обновятся через: {time}",
        "back_to_free_packs": "◀️ Назад к бесплатным пакам",
        "roll_dice": "🎲 Крутить кубик",
        "roll_again": "🎲 Крутить ещё",
        "dice_animation": "🎲 Кубик вращается...",
        "dice_result": "🎲 Выпало число: {result}",
        "dice_win": "🎉 ПОБЕДА! +500 монет и +10 💎!",
        "dice_lose": "😔 ПРОИГРЫШ! -100 монет!",
        "dice_cost": "Стоимость: 100 монет",
        "not_enough_coins_dice": "❌ У вас недостаточно монет для игры!",
        "play_casino": "🎮 Мини-игры",
        "mg_volleyball": "🏐 Влейбол-кольцо",
        "mg_darts": "🎯 Дартс",
        "mg_bowling": "🎳 Боулинг",
        "mg_anim": "✨ Играем...",
        "mg_result": "{title}\n{detail}\n\n💰 Награда: +{coins} монет",

        "dice_rules": "Правила игры:\n🎲 Кубик 1-6\n💎 4,5,6 → +500 монет, +10 алмазов\n💔 1,2,3 → -100 монет",
        "back_to_casino": "◀️ Назад в мини-игры",
        "battle_mode": "⚔️ Режим сражения",
        "battle_vs_player": "👤 Против игрока",
        "battle_vs_ai": "🤖 Против ИИ",
        "battle_ai_level": "🎮 Выбери уровень ИИ",
        "battle_ai_novice": "🟢 Новичок (200 OVR)",
        "battle_ai_amateur": "🔵 Любитель (250 OVR)",
        "battle_ai_pro": "🟣 Профи (300 OVR)",
        "battle_ai_star": "👑 Звезда (350 OVR)",
        "battle_no_goalkeeper": "🧤 Вратарь",
        "battle_no_defender": "🛡️ Защитник",
        "battle_no_midfielder": "🎯 Полузащитник",
        "battle_no_forward": "⚽ Нападающий",
        "battle_missing_position": "❌ У вас неукомплектована позиция: {position}",
        "battle_team_ready": "✅ Ваш состав готов!\nСуммарный OVR: {total}\n\n{team}",
        "battle_search_start": "🔍 Поиск соперника...\nВаш ник: {name}\nНажмите «Отмена», чтобы выйти из очереди.",
        "battle_cancel_search": "❌ Отмена поиска",
        "battle_search_cancelled": "⏹️ Поиск отменён.",
        "battle_found": "🎮 Соперник найден!",
        "battle_your_team": "Ваш состав",
        "battle_opponent_team": "Состав противника",
        "battle_result_win": "🎉 ПОБЕДА! +{reward} монет",
        "battle_result_lose": "😔 ПОРАЖЕНИЕ! -{penalty} монет",
        "card_will_disappear": "\n\n⏳ Карточка исчезнет через {seconds} сек.",
    },
    Language.EN: {
        "main_menu": "⚽ Football Collector",
        "packs": "📦 Packs",
        "collection": "📚 Collection",
        "mini_game": "🎲 Casino",
        "settings": "⚙️ Settings",
        "profile": "👤 Profile",
        "battle_mode": "⚔️ Battle mode",
        "coins": "💰 Coins",
        "gems": "💎 Gems",
        "candies": "🍬 Candies",
        "stars": "⭐ Stars",
        "stars_balance": "⭐ Stars balance",
        "stars_shop": "⭐ Stars shop",
        "topup_stars": "➕ Top up Stars",
        "buy_diamonds_stars": "💎 Buy diamonds with Stars",
        "stars_topup_title": "⭐ Stars top-up",
        "stars_spend_title": "💎 Diamonds for Stars",
        "packs_opened_total": "📦 Packs opened",
        "elo": "🏆 Elo",
        "candy_shop": "🍬 Candy Shop",
        "candy_shop_title": "🍬 Candy Shop",
        "clans": "🏟️ Clans",
        "clans_title": "🏟️ Clans",
        "create_clan": "➕ Create clan (100💎)",
        "join_open_clan": "🔎 Join open",
        "clan_rating": "📋 Clan рейтинги",
        "clan_invites": "📨 Invites",
        "clan_leave": "🚪 Leave clan",
        "clan_invite_member": "➕ Invite",
        "clan_set_role": "🎭 Set role",
        "buy_candy_random": "🍬 Buy candy-rarity card",
        "candy_random_desc": "A special Candy-rarity card. Purchased with 🍬 candies!",
        "not_enough_candies": "❌ Not enough candies!",
        "free_packs": "🎁 Free packs",
        "basic_pack": "📦 Basic Pack",
        "premium_pack": "💎 Premium Pack",
        "basic_pack_desc": "⚡ Chance for rare, epic and legendary players!",
        "premium_pack_desc": "👑 Increased chance for epic and legendary players!",
        "free_pack": "🎁 Free Pack",
        "free_pack_desc": "Every 4 hours — 5 free packs!",
        "buy": "Buy",
        "not_enough_coins": "❌ Not enough coins!",
        "not_enough_gems": "❌ Not enough gems!",
        "new_card": "✨ NEW CARD! ✨",
        "card_received": "✨ Card added to collection! ✨",
        "rarity": "🌟 Rarity",
        "country": "🌍 Country",
        "position": "⚽ Position",
        "ovr": "⚡ OVR",
        "acquired": "📅 Acquired",
        "description": "📝 Description",
        "back": "◀️ Back",
        "back_to_menu": "◀️ Back to menu",
        "reset_progress": "🔄 Reset progress",
        "change_language": "🌐 Change language",
        "confirm_reset": "⚠️ Are you sure you want to reset all progress?",
        "yes": "✅ Yes",
        "no": "❌ No",
        "progress_reset": "🔄 Progress reset!",
        "language_changed": "🌐 Language changed to English",
        "empty_collection": "📭 Your collection is empty\nBuy packs to get cards!",
        "your_collection": "📋 YOUR COLLECTION",
        "card_number": "Card",
        "of": "of",
        "view_card": "👆 View card",
        "search_card": "🔍 Search card",
        "search_prompt": "🔍 Enter player's first or last name to search:",
        "search_no_results": "❌ No cards found with that name",
        "search_too_many": "⚠️ More than 50 cards found. Showing first 50.",
        "search_results": "🔍 Search results for «{query}»:\n\n{results}\n\nClick on the card number to view it.",
        "search_cancel": "❌ Cancel search",
        "card_not_found": "❌ Card not found",
        "close": "❌ Close",
        "fuse": "Fuse",
        "sort_all": "All",
        "sort_common": "Common",
        "sort_rare": "Rare",
        "sort_epic": "Epic",
        "sort_legendary": "Legendary",
        "sort_mythic": "Mythic",
        "sort_candy": "Candy",
        "wins": "Wins",
        "losses": "Losses",
        "total_games": "Total games",
        "free_packs_available": "🎁 Available free packs",
        "free_packs_count": "You have {count} free packs",
        "free_packs_time": "Next packs in: {time}",
        "open_free_pack": "🎲 Open free pack",
        "free_pack_opened": "✅ You opened a free pack! Remaining: {remaining}",
        "no_free_packs": "❌ You have no free packs! Come back in: {time}",
        "free_pack_timer": "⏰ Free packs refresh in: {time}",
        "back_to_free_packs": "◀️ Back to free packs",
        "roll_dice": "🎲 Roll dice",
        "roll_again": "🎲 Roll again",
        "dice_animation": "🎲 Dice rolling...",
        "dice_result": "🎲 Result: {result}",
        "dice_win": "🎉 WIN! +500 coins and +10 💎!",
        "dice_lose": "😔 LOSE! -100 coins!",
        "dice_cost": "Cost: 100 coins",
        "not_enough_coins_dice": "❌ You don't have enough coins to play!",
        "play_casino": "🎮 Mini-games",
        "mg_volleyball": "🏐 Volleyball hoop",
        "mg_darts": "🎯 Darts",
        "mg_bowling": "🎳 Bowling",
        "mg_anim": "✨ Playing...",
        "mg_result": "{title}\n{detail}\n\n💰 Reward: +{coins} coins",

        "dice_rules": "Game rules:\n🎲 Dice 1-6\n💎 4,5,6 → +500 coins, +10 gems\n💔 1,2,3 → -100 coins",
        "back_to_casino": "◀️ Back to mini-games",
        "battle_mode": "⚔️ Battle mode",
        "battle_vs_player": "👤 vs Player",
        "battle_vs_ai": "🤖 vs AI",
        "battle_ai_level": "🎮 Choose AI level",
        "battle_ai_novice": "🟢 Novice (200 OVR)",
        "battle_ai_amateur": "🔵 Amateur (250 OVR)",
        "battle_ai_pro": "🟣 Pro (300 OVR)",
        "battle_ai_star": "👑 Star (350 OVR)",
        "battle_no_goalkeeper": "🧤 Goalkeeper",
        "battle_no_defender": "🛡️ Defender",
        "battle_no_midfielder": "🎯 Midfielder",
        "battle_no_forward": "⚽ Forward",
        "battle_missing_position": "❌ You are missing position: {position}",
        "battle_team_ready": "✅ Your team is ready!\nTotal OVR: {total}\n\n{team}",
        "battle_search_start": "🔍 Searching for opponent...\nYour nickname: {name}\nPress «Cancel» to leave queue.",
        "battle_cancel_search": "❌ Cancel search",
        "battle_search_cancelled": "⏹️ Search cancelled.",
        "battle_found": "🎮 Opponent found!",
        "battle_your_team": "Your team",
        "battle_opponent_team": "Opponent's team",
        "battle_result_win": "🎉 VICTORY! +{reward} coins",
        "battle_result_lose": "😔 DEFEAT! -{penalty} coins",
        "card_will_disappear": "\n\n⏳ Card will disappear in {seconds} sec.",
    }
}

# ================ ВЕРОЯТНОСТИ ВЫПАДЕНИЯ ================
PACK_PROBABILITIES = {
    "basic": {
        "common": 60,
        "rare": 35,
        "epic": 4,
        "legendary": 0.9,
        "mythic": 0.1
    },
    "premium": {
        "rare": 55,
        "epic": 30,
        "legendary": 13,
        "mythic": 2
    }
,
    "ultra": {
        "legendary": 90,
        "mythic": 10
    }
}


# ======= Stars (внутренний баланс) и покупка алмазов =======
STARS_TOPUP_OPTIONS = [250, 450, 800]  # сколько ⭐ пополнить (столько же списывает Telegram Stars)
DIAMONDS_FOR_STARS = {
    "d500": {"diamonds": 500, "cost_stars": 250},
    "d1000": {"diamonds": 1000, "cost_stars": 450},
    "d2500": {"diamonds": 2500, "cost_stars": 800},
}

PACK_PRICES = {
    "basic": {"coins": 100, "gems": 0},
    "premium": {"coins": 0, "gems": 50},
    "free": {"coins": 0, "gems": 0},
    "ultra": {"coins": 0, "gems": 500}
}

# ================ КОНФЕТНАЯ ЛАВКА ================
CANDY_SHOP_PRICE_RANDOM = 50

def get_candy_pool() -> List[dict]:
    """Карточки 'Конфетной' редкости берутся из characters.json (rarity == 'candy')."""
    pool = [c for c in FOOTBALL_PLAYERS if normalize_rarity(c.get('rarity')) == 'candy']
    if pool:
        return pool
    # Фолбэк, если в базе ещё нет конфетных карточек
    return [{
        "id": 9999,
        "name_ru": "Сладкий Джокер",
        "name_en": "Sweet Joker",
        "rarity": "candy",
        "rarity_name_ru": "Конфетная",
        "rarity_name_en": "Candy",
        "country_ru": "🍬",
        "country_en": "🍬",
        "position_ru": "Игрок",
        "position_en": "Player",
        "ovr": 88,
        "description_ru": "Лимитированная конфетная карточка — появляется, если база ещё не обновлена.",
        "description_en": "Limited candy card — appears if the database isn't updated yet.",
        "image": None,
    }]


# ================ КЛАССЫ ДЛЯ УПРАВЛЕНИЯ ДАННЫМИ ================
class UserData:
    def __init__(self, user_id: int, username: str = None):
        self.user_id = user_id
        self.username = username
        self.coins = 1000
        self.gems = 0
        self.candies = 0
        self.stars_balance = 0  # ⭐ внутренний баланс Stars
        self.collection = []
        self.language = Language.RU
        self.card_id_counter = 1
        self.free_packs = 5
        self.last_free_pack_time = datetime.now()
        self.dice_wins = 0
        self.dice_losses = 0
        self.dice_total = 0

        self.elo = 1000
        self.packs_opened_total = 0
        self.clan_id = None
    def to_dict(self):
        return {
            "user_id": self.user_id,
            "username": self.username,
            "coins": self.coins,
            "gems": self.gems,
            "candies": self.candies,
            "stars_balance": self.stars_balance,
            "collection": self.collection,
            "language": self.language.value,
            "card_id_counter": self.card_id_counter,
            "free_packs": self.free_packs,
            "last_free_pack_time": self.last_free_pack_time.isoformat() if self.last_free_pack_time else None,
            "dice_wins": self.dice_wins,
            "dice_losses": self.dice_losses,
            "dice_total": self.dice_total,
            "elo": self.elo,
            "packs_opened_total": self.packs_opened_total,
            "clan_id": self.clan_id}

    @classmethod
    def from_dict(cls, data):
        user = cls(data["user_id"])
        user.username = data.get("username")
        user.coins = data.get("coins", 1000)
        user.gems = data.get("gems", 0)
        user.candies = data.get("candies", 0)
        user.stars_balance = data.get("stars_balance", 0)
        user.collection = data.get("collection", [])
        lang_value = data.get("language", "ru")
        user.language = Language.RU if lang_value == "ru" else Language.EN
        user.card_id_counter = data.get("card_id_counter", 1)
        user.free_packs = data.get("free_packs", 5)
        last_time_str = data.get("last_free_pack_time")
        if last_time_str:
            try:
                user.last_free_pack_time = datetime.fromisoformat(last_time_str)
            except:
                user.last_free_pack_time = datetime.now()
        else:
            user.last_free_pack_time = datetime.now()
        user.dice_wins = data.get("dice_wins", 0)
        user.dice_losses = data.get("dice_losses", 0)
        user.dice_total = data.get("dice_total", 0)
        user.elo = data.get("elo", 1000)
        user.packs_opened_total = data.get("packs_opened_total", 0)
        user.clan_id = data.get("clan_id")
        return user

    def check_free_packs_refresh(self):
        now = datetime.now()
        time_diff = now - self.last_free_pack_time
        if time_diff.total_seconds() >= 4 * 3600:
            self.free_packs = 5
            self.last_free_pack_time = now
            return True
        return False

    def get_free_packs_time_left(self):
        now = datetime.now()
        time_diff = now - self.last_free_pack_time
        seconds_left = max(0, 4 * 3600 - time_diff.total_seconds())
        hours = int(seconds_left // 3600)
        minutes = int((seconds_left % 3600) // 60)
        return f"{hours}ч {minutes}м" if self.language == Language.RU else f"{hours}h {minutes}m"

class UserManager:
    def __init__(self):
        self.users = {}
        self.data_file = "user_data.json"
        self.load_data()

    def get_user(self, user_id: int, username: str = None) -> UserData:
        if user_id not in self.users:
            self.users[user_id] = UserData(user_id, username)
        else:
            if username and self.users[user_id].username != username:
                self.users[user_id].username = username
                self.save_user(self.users[user_id])
            self.users[user_id].check_free_packs_refresh()
        return self.users[user_id]

    def save_user(self, user: UserData):
        self.users[user.user_id] = user
        self.save_data()

    def save_data(self):
        data = {str(uid): user.to_dict() for uid, user in self.users.items()}
        with open(self.data_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_data(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                for uid_str, user_data in data.items():
                    self.users[int(uid_str)] = UserData.from_dict(user_data)
            except Exception as e:
                print(f"Ошибка загрузки данных: {e}")


# ================ КЛАНЫ ================
class ClanData:
    def __init__(self, clan_id: str, name: str, description: str, is_open: bool, owner_id: int):
        self.clan_id = clan_id
        self.name = name
        self.description = description
        self.is_open = is_open
        self.owner_id = owner_id
        # members: user_id -> role ("owner"|"coach"|"player")
        self.members = {str(owner_id): "owner"}
        # invites by username (lowercase, without @)
        self.invites = []

    def to_dict(self):
        return {
            "clan_id": self.clan_id,
            "name": self.name,
            "description": self.description,
            "is_open": self.is_open,
            "owner_id": self.owner_id,
            "members": self.members,
            "invites": self.invites,
        }

    @classmethod
    def from_dict(cls, data: dict):
        clan = cls(
            clan_id=data["clan_id"],
            name=data.get("name", "Clan"),
            description=data.get("description", ""),
            is_open=bool(data.get("is_open", True)),
            owner_id=int(data.get("owner_id", 0)),
        )
        clan.members = data.get("members", {}) or {}
        clan.invites = data.get("invites", []) or []
        # ensure owner role
        if str(clan.owner_id) in clan.members:
            clan.members[str(clan.owner_id)] = "owner"
        return clan


class ClanManager:
    def __init__(self):
        self.clans = {}  # clan_id -> ClanData
        self.data_file = "clans_data.json"
        self.load_data()

    def load_data(self):
        if not os.path.exists(self.data_file):
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            for cid, cdata in raw.items():
                try:
                    self.clans[cid] = ClanData.from_dict(cdata)
                except Exception as e:
                    print(f"Ошибка загрузки клана {cid}: {e}")
        except Exception as e:
            print(f"Ошибка загрузки clans_data.json: {e}")

    def save_data(self):
        raw = {cid: clan.to_dict() for cid, clan in self.clans.items()}
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    def create_clan(self, name: str, description: str, is_open: bool, owner_id: int) -> ClanData:
        clan_id = uuid.uuid4().hex[:10]
        clan = ClanData(clan_id, name, description, is_open, owner_id)
        self.clans[clan_id] = clan
        self.save_data()
        return clan

    def get_clan(self, clan_id: str) -> ClanData | None:
        return self.clans.get(clan_id)

    def delete_clan_if_empty(self, clan: ClanData):
        if len(clan.members) == 0:
            self.clans.pop(clan.clan_id, None)
            self.save_data()

    def clan_rating(self, clan: ClanData) -> int:
        total = 0
        for uid_str in clan.members.keys():
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            user = user_manager.users.get(uid)
            if user:
                total += int(getattr(user, "elo", 0))
        return total

    def top_clans(self, limit: int = 20):
        items = list(self.clans.values())
        items.sort(key=lambda c: self.clan_rating(c), reverse=True)
        return items[:limit]

clan_manager = ClanManager()

user_manager = UserManager()

# --- Compatibility helpers (stars/shop additions) ---
def get_user_data(user_id: int, username: str | None = None) -> 'UserData':
    """Wrapper to keep handler code readable."""
    return user_manager.get_user(user_id, username=username)

def save_user_data(_: 'UserData' | None = None):
    """Persist all user data."""
    user_manager.save_data()


# ================ ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ================
battle_queue = []
battle_lock = asyncio.Lock()

# ================ ФУНКЦИИ ДЛЯ РАБОТЫ С КАРТОЧКАМИ ================
def get_random_card(pack_type: str) -> dict:
    probabilities = PACK_PROBABILITIES.get(pack_type, PACK_PROBABILITIES["basic"])
    rand = random.random() * 100
    cumulative = 0.0
    selected_rarity = "common"
    for rarity, prob in probabilities.items():
        cumulative += float(prob)
        if rand < cumulative:
            selected_rarity = rarity
            break
    rarity_fallback_order = ["mythic", "legendary", "epic", "rare", "common"]
    start_idx = rarity_fallback_order.index(selected_rarity) if selected_rarity in rarity_fallback_order else (len(rarity_fallback_order) - 1)
    chosen = None
    for r in rarity_fallback_order[start_idx:]:
        pool = [c for c in FOOTBALL_PLAYERS if c.get("rarity") == r]
        if pool:
            chosen = random.choice(pool).copy()
            break
    if chosen is None:
        chosen = random.choice(FOOTBALL_PLAYERS).copy()
    chosen["acquired_date"] = datetime.now().strftime("%d.%m.%Y")
    chosen["user_card_id"] = None
    return chosen

def get_card_word(count: int, lang: Language) -> str:
    if lang == Language.RU:
        if count % 10 == 1 and count % 100 != 11:
            return "карточка"
        elif 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
            return "карточки"
        else:
            return "карточек"
    else:
        return "cards" if count != 1 else "card"

# ================ ФУНКЦИИ ДЛЯ КОЛЛЕКЦИИ ================
def get_sorted_collection(collection: list) -> list:
    return sorted(collection, key=lambda x: x.get("user_card_id", 0), reverse=True)

# ================ ФУНКЦИИ ДЛЯ БИТВ ================
def get_best_team(collection: list, lang: Language):
    pos_map = {
        "вратарь": "goalkeeper", "goalkeeper": "goalkeeper",
        "защитник": "defender", "defender": "defender",
        "полузащитник": "midfielder", "midfielder": "midfielder",
        "нападающий": "forward", "forward": "forward"
    }

    best = {"goalkeeper": None, "defender": None, "midfielder": None, "forward": None}
    missing_pos = []

    for card in collection:
        pos_ru = card.get("position_ru", "").lower().strip()
        pos_en = card.get("position_en", "").lower().strip()
        pos = None
        if pos_ru in pos_map:
            pos = pos_map[pos_ru]
        elif pos_en in pos_map:
            pos = pos_map[pos_en]
        else:
            continue

        ovr = card.get("ovr", 0)
        current_best = best[pos]
        if current_best is None or ovr > current_best.get("ovr", 0):
            best[pos] = card

    t = TRANSLATIONS[lang]
    for pos, key in [("goalkeeper", "battle_no_goalkeeper"),
                     ("defender", "battle_no_defender"),
                     ("midfielder", "battle_no_midfielder"),
                     ("forward", "battle_no_forward")]:
        if best[pos] is None:
            missing_pos.append(t[key])

    if missing_pos:
        return None, ", ".join(missing_pos)

    total_ovr = sum(best[p].get("ovr", 0) for p in best)
    return best, total_ovr

def format_team_display(team: dict, lang: Language) -> str:
    lines = []
    t = TRANSLATIONS[lang]
    for pos, key in [("goalkeeper", "battle_no_goalkeeper"),
                     ("defender", "battle_no_defender"),
                     ("midfielder", "battle_no_midfielder"),
                     ("forward", "battle_no_forward")]:
        card = team[pos]
        name = card["name_ru"] if lang == Language.RU else card["name_en"]
        ovr = card.get("ovr", 0)
        emoji = {"goalkeeper": "🧤", "defender": "🛡️", "midfielder": "🎯", "forward": "⚽"}.get(pos, "")
        lines.append(f"{emoji} {t[key]}: {name} (OVR {ovr})")
    return "\n".join(lines)

# ================ ФУНКЦИИ ДЛЯ ПОИСКА ================
def search_cards_in_collection(collection: list, query: str) -> List[dict]:
    query = query.lower().strip()
    results = []
    for card in collection:
        name_ru = card.get("name_ru", "").lower()
        name_en = card.get("name_en", "").lower()
        if query in name_ru or query in name_en:
            results.append(card)
    return results

def format_search_results(results: List[dict], lang: Language) -> str:
    lines = []
    limit = 50
    for i, card in enumerate(results[:limit], 1):
        name = card["name_ru"] if lang == Language.RU else card["name_en"]
        rarity_emoji = {
            "common": "🟢", "rare": "🔵", "epic": "🟣",
            "legendary": "👑", "mythic": "🤍💎"
        }.get(card.get("rarity", "common"), "✨")
        ovr = card.get("ovr", "?")
        lines.append(f"{i}. {rarity_emoji} <b>{name}</b> (OVR {ovr})")
    return "\n".join(lines)

# ================ КЛАВИАТУРЫ ================
def get_main_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()

    # 2 колонки для более "живого" главного меню
    builder.button(text=t["packs"], callback_data="packs")
    builder.button(text=t["collection"], callback_data="collection_start")

    builder.button(text=t["profile"], callback_data="profile")
    builder.button(text=t["mini_game"], callback_data="packs")

    builder.button(text=t["battle_mode"], callback_data="battle_mode")
    builder.button(text=t["candy_shop"], callback_data="candy_shop")

    builder.button(text=t["clans"], callback_data="clans")
    builder.button(text="💵 Магазин $", callback_data="shop")

    builder.button(text=t["rating"], callback_data="rating")
    builder.button(text=t["settings"], callback_data="settings")

    builder.adjust(2, 2, 2, 2, 2)
    return builder.as_markup()


def get_clans_menu_keyboard(user: UserData):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()

    if user.clan_id:
        builder.button(text=t["clan_leave"], callback_data="clan_leave")
        # только глава может приглашать/выдавать роли
        clan = clan_manager.get_clan(user.clan_id)
        if clan and clan.owner_id == user.user_id:
            builder.button(text=t["clan_invite_member"], callback_data="clan_invite")
            builder.button(text=t["clan_set_role"], callback_data="clan_set_role")
        builder.button(text=t["back"], callback_data="main_menu")
        builder.adjust(2, 2, 1)
        return builder.as_markup()

    # не в клане
    builder.button(text=t["create_clan"], callback_data="clan_create")
    builder.button(text=t["join_open_clan"], callback_data="clan_join_list")
# приглашения (показываем кнопку только если есть)
    username = (user.username or "").lstrip("@").lower()
    has_invites = False
    if username:
        for clan in clan_manager.clans.values():
            if username in [u.lower() for u in clan.invites]:
                has_invites = True
                break
    if has_invites:
        builder.button(text=t["clan_invites"], callback_data="clan_invites")

    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def get_clans_join_list_keyboard(user: UserData, limit: int = 10):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    shown = 0
    for clan in clan_manager.top_clans(limit=50):
        if not clan.is_open:
            continue
        if len(clan.members) >= 11:
            continue
        builder.button(text=f"✅ {clan.name}", callback_data=f"clan_join:{clan.clan_id}")
        shown += 1
        if shown >= limit:
            break
    builder.button(text=t["back"], callback_data="clans")
    builder.adjust(1)
    return builder.as_markup()


def get_clans_rating_keyboard(user: UserData):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["back"], callback_data="rating")
    builder.adjust(1)
    return builder.as_markup()


def get_rating_menu_keyboard(user: UserData):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["rating_players"], callback_data="rating_players")
    builder.button(text=t["rating_clans"], callback_data="clans_rating")
    builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_players_rating_keyboard(user: UserData):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="rating")
    builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def get_clan_invites_keyboard(user: UserData):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    username = (user.username or "").lstrip("@").lower()
    for clan in clan_manager.top_clans(limit=50):
        if username and username in [u.lower() for u in clan.invites]:
            builder.button(text=f"✅ Вступить в {clan.name}", callback_data=f"clan_accept:{clan.clan_id}")
    builder.button(text=t["back"], callback_data="clans")
    builder.adjust(1)
    return builder.as_markup()


def get_clan_privacy_keyboard(user: UserData):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔓 Открытый", callback_data="clan_privacy_open")
    builder.button(text="🔒 По приглашению", callback_data="clan_privacy_invite")
    builder.button(text=TRANSLATIONS[user.language]["back"], callback_data="clans")
    builder.adjust(2, 1)
    return builder.as_markup()


def get_role_select_keyboard(user: UserData):
    builder = InlineKeyboardBuilder()
    builder.button(text="🧑‍🏫 Тренер", callback_data="clan_role:coach")
    builder.button(text="👤 Игрок", callback_data="clan_role:player")
    builder.button(text=TRANSLATIONS[user.language]["back"], callback_data="clans")
    builder.adjust(2, 1)
    return builder.as_markup()


def format_clan_members(clan: ClanData) -> str:
    # роль -> эмодзи/название
    role_map = {
        "owner": "👑 Владелец",
        "coach": "🧑‍🏫 Тренер",
        "player": "👤 Игрок",
    }
    lines = []
    for uid_str, role in clan.members.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        user = user_manager.users.get(uid)
        uname = None
        if user and user.username:
            uname = "@" + user.username.lstrip("@")
        else:
            uname = f"ID:{uid}"
        lines.append(f"{uname} — {role_map.get(role, role)}")
    return "\n".join(lines) if lines else "—"

def get_profile_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_packs_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{t['basic_pack']} - {PACK_PRICES['basic']['coins']} {t['coins']}",
        callback_data="buy_basic"
    )
    builder.button(
        text=f"{t['premium_pack']} - {PACK_PRICES['premium']['gems']} {t['gems']}",
        callback_data="buy_premium"
    )
    builder.button(
        text=f"🔥 Ультра‑Пак - {PACK_PRICES['ultra']['gems']} {t['gems']}",
        callback_data="buy_ultra"
    )
    builder.button(text=t["free_pack"], callback_data="free_pack_menu")
    builder.button(text=t.get("stars_shop", "⭐ Stars"), callback_data="stars_shop")
    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_mini_game_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["play_casino"], callback_data="play_casino")
    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_casino_keyboard(lang: Language, show_back: bool = True):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    # Мини-игры
    builder.button(text=t["mg_volleyball"], callback_data="mg_volleyball")
    builder.button(text=t["mg_darts"], callback_data="mg_darts")
    builder.button(text=t["mg_bowling"], callback_data="mg_bowling")
    builder.button(text=t["roll_dice"], callback_data="roll_dice")
    if show_back:
        builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(2, 2, 1)
    return builder.as_markup()

def get_dice_result_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["roll_again"], callback_data="roll_dice")
    builder.button(text=t["back_to_casino"], callback_data="play_casino")
    builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_free_pack_keyboard(lang: Language, has_free_packs: bool):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    if has_free_packs:
        builder.button(text=t["open_free_pack"], callback_data="open_free_pack")
    builder.button(text=t["back"], callback_data="packs")
    builder.adjust(1)
    return builder.as_markup()

def get_free_pack_result_keyboard(lang: Language, has_more_packs: bool):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    if has_more_packs:
        builder.button(text=t["open_free_pack"], callback_data="open_free_pack")
    builder.button(text=t["back_to_free_packs"], callback_data="free_pack_menu")
    builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_settings_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["reset_progress"], callback_data="reset_confirm")
    builder.button(text=t["change_language"], callback_data="change_lang")
    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_candy_shop_keyboard(lang: Language, price: int):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=f"{t['buy_candy_random']} — {price} {t['candies']}", callback_data="buy_candy_random")
    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_reset_confirm_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["yes"], callback_data="reset_yes")
    builder.button(text=t["no"], callback_data="reset_no")
    builder.adjust(2)
    return builder.as_markup()

def get_collection_navigation_keyboard(lang: Language, current_index: int, total_cards: int):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    
    builder.row(
        InlineKeyboardButton(text=t["search_card"], callback_data="search_card_start"),
        InlineKeyboardButton(text=t["view_card"], callback_data=f"collection_view_{current_index}")
    )
    
    nav_row = []
    if current_index > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"collection_prev_{current_index}"))
    nav_row.append(InlineKeyboardButton(
        text=f"{t['card_number']} {current_index + 1}/{total_cards}",
        callback_data="noop"
    ))
    if current_index < total_cards - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"collection_next_{current_index}"))
    builder.row(*nav_row)
    
    builder.row(InlineKeyboardButton(text=t["back"], callback_data="main_menu"))
    
    return builder.as_markup()

def get_collection_sections_keyboard(user: UserData):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📚 {t['sort_all']}", callback_data="collection_section_all")
    builder.button(text=f"🟢 {t['sort_common']}", callback_data="collection_section_common")
    builder.button(text=f"🔵 {t['sort_rare']}", callback_data="collection_section_rare")
    builder.button(text=f"🟣 {t['sort_epic']}", callback_data="collection_section_epic")
    builder.button(text=f"👑 {t['sort_legendary']}", callback_data="collection_section_legendary")
    builder.button(text=f"🤍💎 {t['sort_mythic']}", callback_data="collection_section_mythic")
    has_candy = any(normalize_rarity(c.get("rarity")) == "candy" for c in user.collection)
    if has_candy:
        builder.button(text=f"🍬 {t['sort_candy']}", callback_data="collection_section_candy")
    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()

def filter_collection_by_rarity(user: UserData, rarity: str) -> list:
    if rarity == "all":
        return get_sorted_collection(user.collection)
    r = normalize_rarity(rarity)
    return get_sorted_collection([c for c in user.collection if normalize_rarity(c.get("rarity")) == r])

def get_collection_navigation_keyboard_with_section(lang: Language, section: str, current_index: int, total_cards: int):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(text=t["search_card"], callback_data="search_card_start"),
        InlineKeyboardButton(text=t["view_card"], callback_data=f"collection_view_{section}_{current_index}")
    )

    nav_row = []
    if current_index > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"collection_prev_{section}_{current_index}"))
    nav_row.append(InlineKeyboardButton(
        text=f"{t['card_number']} {current_index + 1}/{total_cards}",
        callback_data="noop"
    ))
    if current_index < total_cards - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"collection_next_{section}_{current_index}"))
    builder.row(*nav_row)

    builder.row(
        InlineKeyboardButton(text="📂 " + (t["collection"] if lang == Language.RU else "Collection"), callback_data="collection_start"),
        InlineKeyboardButton(text=t["back"], callback_data="main_menu")
    )
    builder.adjust(1, 1, 2, 1)
    return builder.as_markup()

def get_card_detail_keyboard(user: UserData, card: dict, from_collection: bool = False, from_search: str = "", current_index: int = 0):
    t = TRANSLATIONS[user.language]
    builder = InlineKeyboardBuilder()
    
    rarity = card.get("rarity", "common")
    next_rarity = RARITY_UPGRADE_MAP.get(rarity)
    dup_count = count_duplicates(user.collection, card)
    
    if next_rarity and dup_count >= 5:
        builder.button(
            text=f"♻️ {t['fuse']} (5× → {next_rarity})",
            callback_data=f"fuse_{card.get('user_card_id')}_{'col' if from_collection else from_search}_{current_index}"
        )
    
    if from_collection:
        builder.button(text=t["close"], callback_data=f"collection_return_{from_search if from_search else 'all'}_{current_index}")
    else:
        builder.button(text=t["close"], callback_data=f"back_to_search_{from_search}")
    
    builder.adjust(1)
    return builder.as_markup()

def get_search_results_keyboard(results: List[dict], lang: Language, query: str):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    limit = min(50, len(results))
    for i in range(limit):
        card = results[i]
        name = card["name_ru"] if lang == Language.RU else card["name_en"]
        short_name = name[:20] + "..." if len(name) > 20 else name
        builder.button(
            text=f"{i+1}. {short_name}",
            callback_data=f"search_view_{card['user_card_id']}_{query}"
        )
    builder.row(InlineKeyboardButton(text=t["back"], callback_data="collection_start"))
    builder.adjust(1)
    return builder.as_markup()

# ================ КЛАВИАТУРЫ ДЛЯ БИТВ ================
def get_battle_mode_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["battle_vs_player"], callback_data="battle_pvp")
    builder.button(text=t["battle_vs_ai"], callback_data="battle_ai")
    builder.button(text=t["back"], callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_ai_level_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["battle_ai_novice"], callback_data="battle_ai_level_novice")
    builder.button(text=t["battle_ai_amateur"], callback_data="battle_ai_level_amateur")
    builder.button(text=t["battle_ai_pro"], callback_data="battle_ai_level_pro")
    builder.button(text=t["battle_ai_star"], callback_data="battle_ai_level_star")
    builder.button(text=t["back"], callback_data="battle_mode")
    builder.adjust(1)
    return builder.as_markup()

def get_battle_search_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["battle_cancel_search"], callback_data="battle_cancel_search")
    builder.adjust(1)
    return builder.as_markup()

def get_battle_result_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["back"], callback_data="battle_mode")
    builder.adjust(1)
    return builder.as_markup()

# ================ ФУНКЦИИ ДЛЯ ФОРМАТИРОВАНИЯ ТЕКСТА ================
def get_text_main_menu(user: UserData) -> str:
    t = TRANSLATIONS[user.language]
    if user.language == Language.RU:
        return "<b>Главное меню</b>\n⚽️Футбольный Коллекционер⚽️\n\n⭐️ Собери свою, лучшую коллекцию ⭐️"
    subtitle = "Choose a section below 👇"
    return f"⚽ <b>{t['main_menu']}</b>\n<i>{subtitle}</i>"


def build_packs_page_text(user: UserData) -> str:
    """Текст страницы паков (информативно, без публичных шансов)."""
    t = TRANSLATIONS[user.language]
    stars = getattr(user, "stars_balance", 0)

    if user.language == Language.RU:
        return (
            f"🧩 <b>{t['packs']}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 <b>Ваш баланс</b>\n"
            f"{t['coins']}: <b>{user.coins}</b>   {t['gems']}: <b>{user.gems}</b>   ⭐ Stars: <b>{stars}</b>\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"📦 <b>{t['basic_pack']}</b> — 1 карточка\n"
            f"• Цена: <b>{PACK_PRICES['basic']['coins']} {t['coins']}</b>\n"
            f"• Хороший выбор для постоянного открытия и сплавки дубликатов.\n\n"
            f"💎 <b>{t['premium_pack']}</b> — 1 карточка\n"
            f"• Цена: <b>{PACK_PRICES['premium']['gems']} {t['gems']}</b>\n"
            f"• Шансы на высокие редкости здесь заметно повышены.\n\n"
            f"🔥 <b>Ультра‑Пак</b> — 1 карточка\n"
            f"• Цена: <b>{PACK_PRICES['ultra']['gems']} {t['gems']}</b>\n"
            f"• Гарант: <b>Легендарная</b> или <b>Мифическая</b> (внутри шанс повышен).\n\n"
            f"♻️ <b>Сплавка</b>\n"
            f"• Сплавляйте дубликаты и получайте 🍬 конфеты.\n"
            f"• Конфеты тратятся в 🍬 Конфетной лавке на особые карточки.\n\n"
            f"⭐ <b>Stars</b>\n"
            f"• Пополняйте ⭐ баланс в боте и тратьте на покупки (например, на 💎 алмазы).\n"
        )
    else:
        return (
            f"🧩 <b>{t['packs']}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"💰 <b>Your balance</b>\n"
            f"{t['coins']}: <b>{user.coins}</b>   {t['gems']}: <b>{user.gems}</b>   ⭐ Stars: <b>{stars}</b>\n"
            f"━━━━━━━━━━━━━━\n\n"
            f"📦 <b>{t['basic_pack']}</b> — 1 card\n"
            f"• Price: <b>{PACK_PRICES['basic']['coins']} {t['coins']}</b>\n\n"
            f"💎 <b>{t['premium_pack']}</b> — 1 card\n"
            f"• Price: <b>{PACK_PRICES['premium']['gems']} {t['gems']}</b>\n"
            f"• Better odds for high rarities.\n\n"
            f"🔥 <b>Ultra Pack</b> — 1 card\n"
            f"• Price: <b>{PACK_PRICES['ultra']['gems']} {t['gems']}</b>\n"
            f"• Guaranteed <b>Legendary</b> or <b>Mythic</b>.\n\n"
            f"♻️ <b>Fusion</b>: get 🍬 candies from duplicates.\n"
            f"⭐ <b>Stars</b>: top up in-bot and spend on purchases.\n"
        )

def get_minigames_text(user: UserData) -> str:
    t = TRANSLATIONS[user.language]
    if user.language == Language.RU:
        return (
            f"🎮 <b>{t['mini_game']}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"Выберите мини-игру:"
        )
    return (
        f"🎮 <b>{t['mini_game']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Choose a mini-game:"
    )


def get_text_card_detail(card: dict, lang: Language):
    t = TRANSLATIONS[lang]
    rarity_emoji = {
        "common": "🟢", "rare": "🔵", "epic": "🟣",
        "legendary": "👑", "mythic": "🤍💎",
        "candy": "🍬"
    }
    name = card["name_ru"] if lang == Language.RU else card["name_en"]
    rarity_name = card["rarity_name_ru"] if lang == Language.RU else card["rarity_name_en"]
    country = card["country_ru"] if lang == Language.RU else card["country_en"]
    position = card["position_ru"] if lang == Language.RU else card["position_en"]
    description = card["description_ru"] if lang == Language.RU else card["description_en"]
    
    return (
        f"✨ <b>{name}</b> ✨\n"
        f"━━━━━━━━━━━━━━\n"
        f"{rarity_emoji.get(card['rarity'], '✨')} {rarity_name}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['country']}: {country}\n"
        f"{t['position']}: {position}\n"
        f"{t['ovr']}: <b>{card['ovr']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"<i>{description}</i>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['acquired']}: {card['acquired_date']}"
    )

def get_text_collection_card(card: dict, index: int, total: int, lang: Language) -> str:
    t = TRANSLATIONS[lang]
    rarity_emoji = {
        "common": "🟢", "rare": "🔵", "epic": "🟣",
        "legendary": "👑", "mythic": "🤍💎",
        "candy": "🍬"
    }
    name = card["name_ru"] if lang == Language.RU else card["name_en"]
    rarity_name = card["rarity_name_ru"] if lang == Language.RU else card["rarity_name_en"]
    
    return (
        f"<b>{t['your_collection']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{rarity_emoji.get(card['rarity'], '✨')} <b>{name}</b> — {rarity_name}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['card_number']} {index + 1}/{total}\n"
        f"{t['ovr']}: <b>{card['ovr']}</b>\n"
        f"{t['position']}: {card['position_ru'] if lang == Language.RU else card['position_en']}\n"
        f"{t['acquired']}: {card['acquired_date']}"
    )

def get_text_casino(user: UserData):
    t = TRANSLATIONS[user.language]
    return (
        f"🎰 <b>{t['mini_game']} - {t['play_casino']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['dice_rules']}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['coins']}: {user.coins} 🪙\n"
        f"{t['gems']}: {user.gems} 💎\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 Статистика:\n"
        f"🎲 Игр: {user.dice_total}\n"
        f"🎉 Побед: {user.dice_wins}\n"
        f"😔 Поражений: {user.dice_losses}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['dice_cost']}"
    )

def get_text_free_packs(user: UserData):
    t = TRANSLATIONS[user.language]
    user.check_free_packs_refresh()
    if user.free_packs > 0:
        text = (
            f"🎁 <b>{t['free_packs_available']}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"{t['free_packs_count'].format(count=user.free_packs)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{t['basic_pack_desc']}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🎲 У вас {user.free_packs} бесплатных паков!"
        )
    else:
        time_left = user.get_free_packs_time_left()
        text = (
            f"⏰ <b>{t['free_pack_timer']}</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"{t['no_free_packs'].format(time=time_left)}\n"
            f"━━━━━━━━━━━━━━\n"
            f"{t['free_pack_desc']}"
        )
    return text

def build_drop_caption(card: dict, lang: Language, seconds: int) -> str:
    name = card.get("name_ru") if lang == Language.RU else card.get("name_en")
    rarity = card.get("rarity", "common")
    ovr = card.get("ovr", "?")
    
    headers_ru = {
        "common": "🎴 Находка из пака!",
        "rare": "🔵 Редкий дроп!",
        "epic": "🟣 Эпический улов!",
        "legendary": "👑 ЛЕГЕНДАРНЫЙ ДРОП!",
        "mythic": "🤍💎 МИФИЧЕСКИЙ ДРОП! 💎🤍",
    }
    headers_en = {
        "common": "🎴 Pack pull!",
        "rare": "🔵 Rare pull!",
        "epic": "🟣 Epic pull!",
        "legendary": "👑 LEGENDARY PULL!",
        "mythic": "🤍💎 MYTHIC PULL! 💎🤍",
    }
    header = (headers_ru if lang == Language.RU else headers_en).get(rarity, "✨")
    divider = "⬜⬜⬜⬜⬜⬜⬜⬜" if rarity == "mythic" else "━━━━━━━━━━━━━━"
    added_ru = "\n✅ Карточка отправилась в твою коллекцию."
    added_en = "\n✅ Card has been added to your collection."
    disappear_ru = f"\n\n⏳ Карточка исчезнет через {seconds} сек."
    disappear_en = f"\n\n⏳ Card will disappear in {seconds} sec."
    
    return (
        f"<b>{header}</b>\n"
        f"{divider}\n"
        f"<b>{name}</b>\n"
        f"⚡ OVR: <b>{ovr}</b>\n"
        f"{divider}"
        f"{added_ru if lang == Language.RU else added_en}"
        f"{disappear_ru if lang == Language.RU else disappear_en}"
    )

async def send_pack_opening_animation(message: Message, lang: Language):
    status_msg = await message.answer(
        "🌀 Открываем пак… Подбираем игрока…" if lang == Language.RU else "🌀 Opening pack… Selecting player…"
    )
    return status_msg

# ================ ОБРАБОТЧИКИ КОМАНД ================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    full_name = message.from_user.full_name
    logger.info(f"[USER JOIN] @{username} | {full_name} | id={user_id}")
    user = user_manager.get_user(user_id, username)
    
    bg_path = os.path.join(IMAGES_PATH, BACKGROUND_IMAGE_FILENAME)
    caption = get_text_main_menu(user)
    
    if os.path.exists(bg_path):
        await message.answer_photo(
            photo=FSInputFile(bg_path),
            caption=caption,
            reply_markup=get_main_keyboard(user.language),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            caption,
            reply_markup=get_main_keyboard(user.language),
            parse_mode="HTML"
        )

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    user.coins = 1000
    user.gems = 0
    user.candies = 0
    user.collection = []
    user.card_id_counter = 1
    user.free_packs = 5
    user.last_free_pack_time = datetime.now()
    user.dice_wins = 0
    user.dice_losses = 0
    user.dice_total = 0
    user_manager.save_user(user)
    
    await message.answer(t["progress_reset"])

# ================ ОБРАБОТЧИКИ КОЛЛБЭКОВ ================


# =============== КОМАНДЫ (Меню рядом с текстовой строкой) ===============
@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "<b>Команды бота</b>\n"
        "/start — открыть главное меню\n"
        "/menu — главное меню\n"
        "/profile — профиль\n"
        "/packs — пакеты\n"
        "/minigames — мини-игры\n"
        "/clans — кланы\n"
        "/settings — настройки\n"
        "/help — список команд"
    )
    await message.answer(text, parse_mode="HTML")


async def _send_main_menu(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)

    caption = get_text_main_menu(user)
    await send_page(
        message,
        image_basename="backgrauond",  # ваш фон главного меню
        text=caption,
        reply_markup=get_main_keyboard(user.language),
    )


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await _send_main_menu(message)


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)

    caption = build_profile_text(user)
    await send_page(
        message,
        image_basename="profile",
        text=caption,
        reply_markup=get_profile_keyboard(user.language),
    )


@dp.message(Command("packs"))
async def cmd_packs(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)

    await message.answer(
        build_packs_page_text(user),
        reply_markup=get_packs_keyboard(user.language),
        parse_mode="HTML",
    )


@dp.message(Command("minigames"))
async def cmd_minigames(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)

    await send_page(
        message,
        image_basename="minigames",
        text=get_minigames_text(user),
        reply_markup=get_mini_game_keyboard(user.language),
    )


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)

    lang = user.language
    t = TRANSLATIONS[lang]
    text = (
        f"⚙️ <b>{t['settings']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['choose_lang']}"
    )
    await send_page(
        message,
        image_basename="settings",
        text=text,
        reply_markup=get_settings_keyboard(lang),
    )

@dp.message(Command("clans"))
async def cmd_clans(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)
    text = build_clans_page_text(user)
    await send_page(
        message,
        image_basename="clans",
        text=text,
        reply_markup=get_clans_menu_keyboard(user),
    )

@dp.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    bg_path = os.path.join(IMAGES_PATH, BACKGROUND_IMAGE_FILENAME)
    caption = get_text_main_menu(user)
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    if os.path.exists(bg_path):
        await callback.message.answer_photo(
            photo=FSInputFile(bg_path),
            caption=caption,
            reply_markup=get_main_keyboard(user.language),
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            caption,
            reply_markup=get_main_keyboard(user.language),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return

    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)

    caption = build_profile_text(user)

    # фон профиля
    bg_path = get_existing_image_path("profile")
    if bg_path:
        # Надёжнее слать новое сообщение, чтобы не ловить ограничения edit_text/edit_caption
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer_photo(
            photo=FSInputFile(bg_path),
            caption=caption,
            reply_markup=get_profile_keyboard(user.language),
            parse_mode="HTML",
        )
    else:
        await safe_edit_or_send(callback.message, caption, reply_markup=get_profile_keyboard(user.language))

@dp.callback_query(F.data == "packs")
async def callback_packs(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    text = build_packs_page_text(user)
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=get_packs_keyboard(user.language), parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(text, reply_markup=get_packs_keyboard(user.language), parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=get_packs_keyboard(user.language), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.in_(["buy_basic", "buy_premium", "buy_free", "buy_ultra"]))
async def callback_buy_pack(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    try:
        await callback.answer("📦 Открываю пак…")
    except TelegramBadRequest:
        return
    
    pack_type = callback.data.split("_", 1)[1]
    price = PACK_PRICES.get(pack_type)
    if not price:
        await callback.answer("Неизвестный пак.", show_alert=True)
        return

    # Универсальная проверка валют
    need_coins = int(price.get("coins", 0) or 0)
    need_gems = int(price.get("gems", 0) or 0)

    if need_coins and user.coins < need_coins:
        await callback.answer(t["not_enough_coins"], show_alert=True)
        return
    if need_gems and user.gems < need_gems:
        await callback.answer(t["not_enough_gems"], show_alert=True)
        return

    if need_coins:
        user.coins -= need_coins
    if need_gems:
        user.gems -= need_gems
    
    status_msg = await send_pack_opening_animation(callback.message, user.language)
    card = get_random_card(pack_type)
    card["user_card_id"] = user.card_id_counter
    user.card_id_counter += 1
    user.collection.append(card)
    user_manager.save_user(user)
    
    caption = build_drop_caption(card, user.language, CARD_LIFETIME_SECONDS)
    media = get_card_media(card)
    card_msg = None
    
    try:
        await bot.send_chat_action(callback.message.chat.id, "upload_photo")
    except:
        pass
    
    try:
        if media:
            card_msg = await callback.message.answer_photo(media, caption=caption, parse_mode="HTML")
            await save_tg_file_id(card, card_msg)
        else:
            card_msg = await callback.message.answer(caption, parse_mode="HTML")
    finally:
        await asyncio.sleep(CARD_LIFETIME_SECONDS)
        for m in (card_msg, status_msg):
            try:
                if m:
                    await m.delete()
            except:
                pass

@dp.callback_query(F.data == "mini_game")
async def callback_mini_game(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]

    text = (
        f"🎮 <b>{t['mini_game']}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Выберите мини-игру:"
    )
    await render_page(
        callback,
        image_basename="minigames",
        text=text,
        reply_markup=get_mini_game_keyboard(user.language),
    )
    await callback.answer()

@dp.callback_query(F.data == "play_casino")
async def callback_play_casino(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)

    text = get_text_casino(user)
    await render_page(
        callback,
        image_basename="minigames",
        text=text,
        reply_markup=get_casino_keyboard(user.language, show_back=True),
    )
    await callback.answer()

@dp.callback_query(F.data == "roll_dice")
async def callback_roll_dice(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    if user.coins < 100:
        await callback.answer(t["not_enough_coins_dice"], show_alert=True)
        return
    
    dice_animation = await callback.message.answer_dice(emoji="🎲")
    await asyncio.sleep(2)
    dice_value = dice_animation.dice.value
    user.coins -= 100
    
    if dice_value >= 4:
        user.coins += 500
        user.gems += 10
        user.dice_wins += 1
        result_text = t["dice_win"]
    else:
        user.dice_losses += 1
        result_text = t["dice_lose"]
    
    user.dice_total += 1
    user_manager.save_user(user)
    
    result_message = (
        f"{t['dice_result'].format(result=dice_value)}\n"
        f"{result_text}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t['coins']}: {user.coins} 🪙\n"
        f"{t['gems']}: {user.gems} 💎"
    )
    await callback.message.answer(
        result_message,
        reply_markup=get_dice_result_keyboard(user.language),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "free_pack_menu")
async def callback_free_pack_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    user.check_free_packs_refresh()
    text = get_text_free_packs(user)
    has_free_packs = user.free_packs > 0
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        text,
        reply_markup=get_free_pack_keyboard(user.language, has_free_packs),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.in_({"mg_volleyball", "mg_darts", "mg_bowling"}))
async def callback_minigame_play(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]

    game = callback.data

    # показываем анимированный стикер (и удаляем через 5 секунд после результата)
    sticker_file_id = (
        MINIGAME_STICKER_BASKETBALL_FILE_ID if game == "mg_volleyball" else
        MINIGAME_STICKER_DARTS_FILE_ID if game == "mg_darts" else
        MINIGAME_STICKER_BOWLING_FILE_ID
    )
    sticker_msg = await send_minigame_sticker(
        callback.message.chat.id,
        file_id=sticker_file_id,
        reply_to_message_id=callback.message.message_id,
    )

    # рассчитываем результат и награду
    if game == "mg_volleyball":
        title = t["mg_volleyball"]
        # 0-3 попаданий
        hits = random.choices([0, 1, 2, 3], weights=[40, 35, 18, 7], k=1)[0]
        if hits == 0:
            coins = 0
            detail = "❌ Мимо кольца!" if user.language == Language.RU else "❌ Miss!"
        elif hits == 1:
            coins = 50
            detail = "✅ Точное попадание!" if user.language == Language.RU else "✅ Nice hit!"
        elif hits == 2:
            coins = 120
            detail = "🔥 Двойной успех!" if user.language == Language.RU else "🔥 Double hit!"
        else:
            coins = 250
            detail = "🏆 ИДЕАЛЬНО! Три подряд!" if user.language == Language.RU else "🏆 PERFECT! Three in a row!"
    elif game == "mg_darts":
        title = t["mg_darts"]
        # 0-100 очков
        score = random.choices(
            [0, 10, 25, 50, 100],
            weights=[20, 30, 25, 18, 7],
            k=1
        )[0]
        if score == 0:
            coins = 0
            detail = "😵 Промах..." if user.language == Language.RU else "😵 Miss..."
        elif score == 10:
            coins = 40
            detail = "🎯 Внешнее кольцо (10)" if user.language == Language.RU else "🎯 Outer ring (10)"
        elif score == 25:
            coins = 90
            detail = "🎯 Среднее кольцо (25)" if user.language == Language.RU else "🎯 Middle ring (25)"
        elif score == 50:
            coins = 160
            detail = "🎯 Почти центр! (50)" if user.language == Language.RU else "🎯 Near center! (50)"
        else:
            coins = 300
            detail = "🎯 БУЛЛЗАЙ! (100)" if user.language == Language.RU else "🎯 BULLSEYE! (100)"
    else:  # mg_bowling
        title = t["mg_bowling"]
        pins = random.choices(list(range(0, 11)), weights=[6,6,6,7,7,8,9,10,12,14,15], k=1)[0]
        if pins == 10:
            coins = 320
            detail = "🎳 СТРАЙК! (10/10)" if user.language == Language.RU else "🎳 STRIKE! (10/10)"
        elif pins >= 7:
            coins = 180
            detail = f"🎳 Отлично! Сбито кегель: {pins}/10" if user.language == Language.RU else f"🎳 Great! Pins: {pins}/10"
        elif pins >= 4:
            coins = 90
            detail = f"🎳 Неплохо. Сбито кегель: {pins}/10" if user.language == Language.RU else f"🎳 Not bad. Pins: {pins}/10"
        elif pins >= 1:
            coins = 30
            detail = f"🎳 Слабо. Сбито кегель: {pins}/10" if user.language == Language.RU else f"🎳 Weak. Pins: {pins}/10"
        else:
            coins = 0
            detail = "😬 Гаттер! (0/10)" if user.language == Language.RU else "😬 Gutter! (0/10)"

    # выдаём награду
    if coins > 0:
        user.coins += coins
        user_manager.save_user(user)

    # показываем результат (обновляем ту же страницу мини-игр)
    result_text = t["mg_result"].format(title=title, detail=detail, coins=coins)
    await render_page(
        callback,
        image_basename="minigames",
        text=result_text,
        reply_markup=get_casino_keyboard(user.language, show_back=True),
        force_new_message=True,
    )
    await callback.answer()

    # удаляем стикер через 5 секунд после показа результата
    await delete_message_safely(sticker_msg, delay=5)


@dp.callback_query(F.data == "open_free_pack")
async def callback_open_free_pack(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    try:
        await callback.answer("🎁 Открываю бесплатный пак…" if user.language == Language.RU else "🎁 Opening free pack…")
    except TelegramBadRequest:
        return
    
    user.check_free_packs_refresh()
    
    if user.free_packs <= 0:
        time_left = user.get_free_packs_time_left()
        await callback.answer(t["no_free_packs"].format(time=time_left), show_alert=True)
        return
    
    status_msg = await send_pack_opening_animation(callback.message, user.language)
    card = get_random_card("basic")
    card["user_card_id"] = user.card_id_counter
    user.card_id_counter += 1
    user.collection.append(card)
    user.free_packs -= 1
    user.packs_opened_total = user.packs_opened_total + 1
    user_manager.save_user(user)
    
    caption = build_drop_caption(card, user.language, CARD_LIFETIME_SECONDS)
    media = get_card_media(card)
    card_msg = None
    
    try:
        await bot.send_chat_action(callback.message.chat.id, "upload_photo")
    except:
        pass
    
    try:
        if media:
            card_msg = await callback.message.answer_photo(
                media,
                caption=caption,
                reply_markup=get_free_pack_result_keyboard(user.language, user.free_packs > 0),
                parse_mode="HTML"
            )
            await save_tg_file_id(card, card_msg)
        else:
            card_msg = await callback.message.answer(
                caption,
                reply_markup=get_free_pack_result_keyboard(user.language, user.free_packs > 0),
                parse_mode="HTML"
            )
    finally:
        await asyncio.sleep(CARD_LIFETIME_SECONDS)
        for m in (card_msg, status_msg):
            try:
                if m:
                    await m.delete()
            except:
                pass
    
    text = get_text_free_packs(user)
    has_free_packs = user.free_packs > 0
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        text,
        reply_markup=get_free_pack_keyboard(user.language, has_free_packs),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]

    text = f"⚙️ <b>{t['settings']}</b>"
    await render_page(
        callback,
        image_basename="settings",
        text=text,
        reply_markup=get_settings_keyboard(user.language),
    )
    await callback.answer()


# ================ КОНФЕТНАЯ ЛАВКА ================
@dp.callback_query(F.data == "candy_shop")
async def callback_candy_shop(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return

    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]

    text = (
        f"🍬 <b>{t['candy_shop_title']}</b>\n"
        f"{t['candies']}: <b>{user.candies}</b> 🍬\n\n"
        f"{t['candy_random_desc']}\n"
        f"Цена: <b>{CANDY_SHOP_PRICE_RANDOM}</b> 🍬"
    )
    kb = get_candy_shop_keyboard(user.language, CANDY_SHOP_PRICE_RANDOM)

    await render_page(
        callback,
        image_basename="candy_shop",
        text=text,
        reply_markup=kb,
    )


@dp.callback_query(F.data == "buy_candy_random")
async def callback_buy_candy_random(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]

    try:
        await callback.answer("🍬 Покупка…")
    except TelegramBadRequest:
        return

    price = CANDY_SHOP_PRICE_RANDOM
    if user.candies < price:
        await callback.answer(t["not_enough_candies"], show_alert=True)
        return

    user.candies -= price

    pool = get_candy_pool()
    chosen = random.choice(pool).copy()

    chosen["acquired_date"] = datetime.now().strftime("%d.%m.%Y")
    chosen["user_card_id"] = user.card_id_counter
    user.card_id_counter += 1
    user.collection.append(chosen)
    user_manager.save_user(user)

    msg_text = (
        f"✅ <b>Покупка успешна!</b>\n"
        f"-{price} 🍬\n"
        f"Теперь у тебя: <b>{user.candies}</b> 🍬\n\n"
        f"{get_text_card_detail(chosen, user.language)}"
    )

    media = get_card_media(chosen)
    card_msg = None
    if media:
        card_msg = await callback.message.answer_photo(media, caption=msg_text, parse_mode="HTML")
        await save_tg_file_id(chosen, card_msg)
    else:
        card_msg = await callback.message.answer(msg_text, parse_mode="HTML")


    # Обновляем текст лавки
    shop_text = (
        f"🍬 <b>{t['candy_shop_title']}</b>\n"
        f"{t['candies']}: <b>{user.candies}</b> 🍬\n\n"
        f"{t['candy_random_desc']}\n"
        f"Цена: <b>{CANDY_SHOP_PRICE_RANDOM}</b> 🍬"
    )
    kb = get_candy_shop_keyboard(user.language, CANDY_SHOP_PRICE_RANDOM)

    await render_page(
        callback,
        image_basename="candy_shop",
        text=shop_text,
        reply_markup=kb,
    )

    # Удаляем карточку через 5 секунд (как и в паках)
    await asyncio.sleep(CARD_LIFETIME_SECONDS)
    try:
        if card_msg:
            await card_msg.delete()
    except Exception:
        pass

@dp.callback_query(F.data == "reset_confirm")
async def callback_reset_confirm(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(t["confirm_reset"], reply_markup=get_reset_confirm_keyboard(user.language))
    else:
        try:
            await callback.message.edit_text(t["confirm_reset"], reply_markup=get_reset_confirm_keyboard(user.language))
        except TelegramBadRequest:
            await callback.message.answer(t["confirm_reset"], reply_markup=get_reset_confirm_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "reset_yes")
async def callback_reset_yes(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    user.coins = 1000
    user.gems = 0
    user.candies = 0
    user.collection = []
    user.card_id_counter = 1
    user.free_packs = 5
    user.last_free_pack_time = datetime.now()
    user.dice_wins = 0
    user.dice_losses = 0
    user.dice_total = 0
    user_manager.save_user(user)
    
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(t["progress_reset"], reply_markup=get_main_keyboard(user.language))
    else:
        try:
            await callback.message.edit_text(t["progress_reset"], reply_markup=get_main_keyboard(user.language))
        except TelegramBadRequest:
            await callback.message.answer(t["progress_reset"], reply_markup=get_main_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "reset_no")
async def callback_reset_no(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    text = f"⚙️ <b>{t['settings']}</b>"
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "change_lang")
async def callback_change_lang(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    if user.language == Language.RU:
        user.language = Language.EN
    else:
        user.language = Language.RU
    
    user_manager.save_user(user)
    t = TRANSLATIONS[user.language]
    
    text = f"⚙️ <b>{t['settings']}</b>"
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
    await callback.answer(t["language_changed"])

# ================ КОЛЛЕКЦИЯ - НАВИГАЦИЯ ================
@dp.callback_query(F.data == "collection_start")
async def callback_collection_start(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return

    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]

    if not user.collection:
        text = t["empty_collection"]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t["back"], callback_data="main_menu")]]
        )
        try:
            await callback.message.delete()
        except:
            pass
        await callback.message.answer(text, reply_markup=keyboard)
        return

    text = "<b>Выберите раздел</b>" if user.language == Language.RU else "<b>Choose a section</b>"
    kb = get_collection_sections_keyboard(user)

    await render_page(callback, image_basename="collection", text=text, reply_markup=kb)
    await callback.answer()
    return

@dp.callback_query(F.data.startswith("collection_section_"))
async def callback_collection_section(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return


    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)

    section = callback.data.split("_", 2)[2]  # all/common/...
    filtered = filter_collection_by_rarity(user, section)

    if not filtered:
        t = TRANSLATIONS[user.language]
        await callback.message.answer(
            ("В разделе нет карточек." if user.language == Language.RU else "No cards in this section."),
            reply_markup=get_collection_sections_keyboard(user),
            parse_mode="HTML"
        )
        return

    await show_collection_card_section(callback.message, user, filtered, 0, section)

async def show_collection_card_section(message: Message, user: UserData, collection: list, index: int, section: str):
    card = collection[index]
    caption = get_text_collection_card(card, index, len(collection), user.language)
    keyboard = get_collection_navigation_keyboard_with_section(user.language, section, index, len(collection))
    media = get_card_media(card)

    try:
        if media:
            if message.photo:
                await message.edit_media(
                    types.InputMediaPhoto(media=media, caption=caption, parse_mode="HTML"),
                    reply_markup=keyboard
                )
            else:
                try:
                    await message.delete()
                except:
                    pass
                sent = await message.answer_photo(
                    media,
                    caption=caption,
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                await save_tg_file_id(card, sent)
        else:
            if message.text:
                await message.edit_text(caption, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.answer(caption, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest:
        try:
            await message.answer_photo(media, caption=caption, reply_markup=keyboard, parse_mode="HTML")
        except:
            await message.answer(caption, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data.startswith("collection_prev_"))
async def callback_collection_prev(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    parts = callback.data.split("_")
    if len(parts) >= 4:
        section = parts[2]
        current_index = int(parts[3])
        sorted_collection = filter_collection_by_rarity(user, section)
    else:
        section = "all"
        sorted_collection = get_sorted_collection(user.collection)
        current_index = int(parts[2])
    
    if current_index > 0:
        await show_collection_card_section(callback.message, user, sorted_collection, current_index - 1, section)
    else:
        await callback.answer()

@dp.callback_query(F.data.startswith("collection_next_"))
async def callback_collection_next(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    parts = callback.data.split("_")
    if len(parts) >= 4:
        section = parts[2]
        current_index = int(parts[3])
        sorted_collection = filter_collection_by_rarity(user, section)
    else:
        section = "all"
        sorted_collection = get_sorted_collection(user.collection)
        current_index = int(parts[2])
    
    if current_index < len(sorted_collection) - 1:
        await show_collection_card_section(callback.message, user, sorted_collection, current_index + 1, section)
    else:
        await callback.answer()

@dp.callback_query(F.data.startswith("collection_view_"))
async def callback_collection_view(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    parts = callback.data.split("_")
    if len(parts) >= 4:
        section = parts[2]
        current_index = int(parts[3])
        sorted_collection = filter_collection_by_rarity(user, section)
    else:
        section = "all"
        sorted_collection = get_sorted_collection(user.collection)
        current_index = int(parts[2])
    card = sorted_collection[current_index]
    
    media = get_card_media(card)
    
    if media:
        msg = await callback.message.answer_photo(
            media,
            caption=get_text_card_detail(card, user.language),
            reply_markup=get_card_detail_keyboard(user, card, from_collection=True, from_search=section, current_index=current_index),
            parse_mode="HTML"
        )
        await save_tg_file_id(card, msg)
    else:
        await callback.message.answer(
            get_text_card_detail(card, user.language),
            reply_markup=get_card_detail_keyboard(user, card, from_collection=True, from_search=section, current_index=current_index),
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("collection_return_"))
async def callback_collection_return(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    parts = callback.data.split("_")
    if len(parts) >= 4:
        section = parts[2]
        current_index = int(parts[3])
        sorted_collection = filter_collection_by_rarity(user, section)
    else:
        section = "all"
        sorted_collection = get_sorted_collection(user.collection)
        current_index = int(parts[2])
    
    await callback.message.delete()
    await show_collection_card(callback.message, user, sorted_collection, current_index)

async def show_collection_card(message: Message, user: UserData, collection: list, index: int):
    card = collection[index]
    caption = get_text_collection_card(card, index, len(collection), user.language)
    keyboard = get_collection_navigation_keyboard(user.language, index, len(collection))
    
    media = get_card_media(card)
    
    try:
        if media:
            if isinstance(media, str):
                if message.photo:
                    await message.edit_media(
                        types.InputMediaPhoto(media=media, caption=caption, parse_mode="HTML"),
                        reply_markup=keyboard
                    )
                else:
                    await message.delete()
                    sent = await message.answer_photo(
                        media,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                    await save_tg_file_id(card, sent)
            else:
                if message.photo:
                    await message.edit_media(
                        types.InputMediaPhoto(media=media, caption=caption, parse_mode="HTML"),
                        reply_markup=keyboard
                    )
                else:
                    await message.delete()
                    sent = await message.answer_photo(
                        media,
                        caption=caption,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                    await save_tg_file_id(card, sent)
        else:
            if message.photo:
                await message.delete()
                await message.answer(caption, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.edit_text(caption, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        print(f"Error showing card: {e}")
        await message.answer(caption, reply_markup=keyboard, parse_mode="HTML")

# ================ ПОИСК ПО КОЛЛЕКЦИИ ================
@dp.callback_query(F.data == "search_card_start")
async def callback_search_card_start(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    await callback.message.delete()
    await callback.message.answer(t["search_prompt"], parse_mode="HTML")
    await state.set_state(SearchStates.waiting_for_query)

@dp.message(SearchStates.waiting_for_query)
async def process_search_query(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    query = message.text.strip()
    
    if not query:
        await message.answer(t["search_no_results"])
        await state.clear()
        return
    
    results = search_cards_in_collection(user.collection, query)
    
    if not results:
        await message.answer(t["search_no_results"])
        await state.clear()
        return
    
    results_text = format_search_results(results, user.language)
    if len(results) > 50:
        results_text += f"\n\n<i>{t['search_too_many']}</i>"
    
    full_text = t["search_results"].format(query=query, results=results_text)
    keyboard = get_search_results_keyboard(results, user.language, query)
    
    await message.answer(full_text, reply_markup=keyboard, parse_mode="HTML")
    await state.clear()

@dp.callback_query(F.data.startswith("search_view_"))
async def callback_search_view_card(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    parts = callback.data.split("_")
    card_id = int(parts[2])
    query = "_".join(parts[3:])
    
    card = None
    for c in user.collection:
        if c.get("user_card_id") == card_id:
            card = c
            break
    
    if not card:
        await callback.answer(t["card_not_found"], show_alert=True)
        return
    
    media = get_card_media(card)
    
    if media:
        msg = await callback.message.answer_photo(
            media,
            caption=get_text_card_detail(card, user.language),
            reply_markup=get_card_detail_keyboard(user, card, from_collection=False, from_search=query),
            parse_mode="HTML"
        )
        await save_tg_file_id(card, msg)
    else:
        await callback.message.answer(
            get_text_card_detail(card, user.language),
            reply_markup=get_card_detail_keyboard(user, card, from_collection=False, from_search=query),
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("back_to_search_"))
async def callback_back_to_search(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    query = callback.data.replace("back_to_search_", "")
    results = search_cards_in_collection(user.collection, query)
    
    if not results:
        await callback.message.delete()
        await callback.message.answer(t["search_no_results"])
        return
    
    results_text = format_search_results(results, user.language)
    if len(results) > 50:
        results_text += f"\n\n<i>{t['search_too_many']}</i>"
    
    full_text = t["search_results"].format(query=query, results=results_text)
    keyboard = get_search_results_keyboard(results, user.language, query)
    
    await callback.message.delete()
    await callback.message.answer(full_text, reply_markup=keyboard, parse_mode="HTML")

# ================ СПЛАВКА ДУБЛИКАТОВ ================
@dp.callback_query(F.data.startswith("fuse_"))
async def callback_fuse_duplicates(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    try:
        await callback.answer("♻️ Сплавляю…")
    except TelegramBadRequest:
        return
    
    parts = callback.data.split("_")
    card_id = int(parts[1])
    source = parts[2]
    current_index = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    
    target = None
    for c in user.collection:
        if c.get("user_card_id") == card_id:
            target = c
            break
    
    if not target:
        await callback.message.answer("Карточка не найдена")
        return
    
    rarity = target.get("rarity", "common")
    next_rarity = RARITY_UPGRADE_MAP.get(rarity)
    
    if not next_rarity:
        await callback.message.answer("Эту редкость нельзя сплавить выше.")
        return
    
    dup_count = count_duplicates(user.collection, target)
    if dup_count < 5:
        await callback.message.answer(f"Недостаточно дубликатов для сплавки: нужно 5, есть {dup_count}.")
        return
    
    key = card_identity_key(target)
    removed = 0
    new_collection = []
    for c in user.collection:
        if removed < 5 and card_identity_key(c) == key:
            removed += 1
            continue
        new_collection.append(c)
    user.collection = new_collection

    candies_gained = get_candies_for_fuse(rarity)
    user.candies += max(0, int(candies_gained))
    
    pool = [c for c in FOOTBALL_PLAYERS if c.get("rarity") == next_rarity]
    if not pool:
        fallback_order = ["mythic", "legendary", "epic", "rare", "common"]
        start_idx = fallback_order.index(next_rarity) if next_rarity in fallback_order else 0
        chosen = None
        for r in fallback_order[start_idx:]:
            p2 = [c for c in FOOTBALL_PLAYERS if c.get("rarity") == r]
            if p2:
                chosen = random.choice(p2).copy()
                break
        if chosen is None:
            chosen = random.choice(FOOTBALL_PLAYERS).copy()
    else:
        chosen = random.choice(pool).copy()
    
    chosen["acquired_date"] = datetime.now().strftime("%d.%m.%Y")
    chosen["user_card_id"] = user.card_id_counter
    user.card_id_counter += 1
    user.collection.append(chosen)
    user_manager.save_user(user)
    
    try:
        await bot.send_chat_action(callback.message.chat.id, "upload_photo")
    except:
        pass
    
    name_from = (target.get("name_ru") or target.get("name_en") or target.get("name") or "Игрок")
    text = (
        f"♻️ <b>Сплавка завершена!</b>\n"
        f"Ты сплавил 5× <b>{html.escape(str(name_from))}</b> ({rarity})\n"
        f"+{candies_gained} 🍬\n"
        f"и получил:\n"
        f"{get_text_card_detail(chosen, user.language)}"
    )
    
    media = get_card_media(chosen)
    
    if media:
        msg = await callback.message.answer_photo(media, caption=text, parse_mode="HTML")
        await save_tg_file_id(chosen, msg)
    else:
        await callback.message.answer(text, parse_mode="HTML")
    
    try:
        await callback.answer()
    except:
        pass

# ================ БИТВЫ ================
@dp.callback_query(F.data == "battle_mode")
async def callback_battle_mode(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    await callback.message.delete()
    await callback.message.answer(
        "⚔️ <b>Режим сражения</b>\nВыберите тип битвы:",
        reply_markup=get_battle_mode_keyboard(user.language),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "battle_ai")
async def callback_battle_ai(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    await callback.message.edit_text(
        TRANSLATIONS[user.language]["battle_ai_level"],
        reply_markup=get_ai_level_keyboard(user.language),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("battle_ai_level_"))
async def callback_battle_ai_level(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    lang = user.language
    t = TRANSLATIONS[lang]
    
    level = callback.data.replace("battle_ai_level_", "")
    levels = {
        "novice":   {"ovr": 200, "win": 25,  "lose": 10},
        "amateur":  {"ovr": 250, "win": 50,  "lose": 15},
        "pro":      {"ovr": 300, "win": 75,  "lose": 25},
        "star":     {"ovr": 350, "win": 100, "lose": 50},
    }
    if level not in levels:
        await callback.message.answer("❌ Неизвестный уровень")
        return
    
    ai_ovr = levels[level]["ovr"]
    reward_win = levels[level]["win"]
    penalty_lose = levels[level]["lose"]

    best_team, total_ovr = get_best_team(user.collection, lang)
    if best_team is None:
        await callback.message.answer(
            t["battle_missing_position"].format(position=total_ovr),
            parse_mode="HTML"
        )
        return

    if total_ovr > ai_ovr:
        win_chance = 0.84
    else:
        win_chance = 0.16

    if random.random() < win_chance:
        user.coins += reward_win
        result_key = "battle_result_win"
        reward_text = reward_win
        penalty_text = 0
    else:
        user.coins = max(0, user.coins - penalty_lose)
        result_key = "battle_result_lose"
        reward_text = 0
        penalty_text = penalty_lose

    user_manager.save_user(user)

    team_text = format_team_display(best_team, lang)
    caption = (
        f"{t['battle_team_ready'].format(total=total_ovr, team=team_text)}\n\n"
        f"🤖 <b>AI ({level.capitalize()})</b> — OVR {ai_ovr}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t[result_key].format(reward=reward_text, penalty=penalty_text)}\n"
        f"💰 {t['coins']}: {user.coins}"
    )

    await callback.message.delete()
    await callback.message.answer(caption, reply_markup=get_battle_result_keyboard(lang), parse_mode="HTML")

@dp.callback_query(F.data == "battle_pvp")
async def callback_battle_pvp(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    lang = user.language
    t = TRANSLATIONS[lang]

    best_team, total_ovr = get_best_team(user.collection, lang)
    if best_team is None:
        await callback.message.answer(
            t["battle_missing_position"].format(position=total_ovr),
            parse_mode="HTML"
        )
        return

    display_name = get_user_display_name(user)

    async with battle_lock:
        global battle_queue
        battle_queue = [q for q in battle_queue if q["user_id"] != user_id]
        opponent_entry = None
        for entry in battle_queue:
            if entry["user_id"] != user_id:
                opponent_entry = entry
                break
        if opponent_entry:
            battle_queue.remove(opponent_entry)
            opponent = opponent_entry["user"]
            opponent_team = opponent_entry["team"]
            opponent_total = opponent_entry["total_ovr"]
            await conduct_pvp_battle(callback.message, user, opponent, best_team, opponent_team, total_ovr, opponent_total)
            return
        
        queue_msg = await callback.message.answer(
            t["battle_search_start"].format(name=display_name),
            reply_markup=get_battle_search_keyboard(lang),
            parse_mode="HTML"
        )
        battle_queue.append({
            "user_id": user_id,
            "user": user,
            "team": best_team,
            "total_ovr": total_ovr,
            "message": queue_msg,
            "chat_id": callback.message.chat.id,
        })
        await callback.message.delete()

@dp.callback_query(F.data == "battle_cancel_search")
async def callback_battle_cancel_search(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    lang = user_manager.get_user(user_id, username).language
    t = TRANSLATIONS[lang]
    
    async with battle_lock:
        global battle_queue
        removed = False
        for entry in battle_queue:
            if entry["user_id"] == user_id:
                try:
                    await entry["message"].delete()
                except:
                    pass
                battle_queue.remove(entry)
                removed = True
                break
    if removed:
        await safe_edit_or_send(callback.message, t["battle_search_cancelled"])
    else:
        await callback.answer("❌ Вы не в очереди", show_alert=True)

async def conduct_pvp_battle(message: Message, player1: UserData, player2: UserData,
                             team1: dict, team2: dict, ovr1: int, ovr2: int):
    lang1 = player1.language
    lang2 = player2.language
    t1 = TRANSLATIONS[lang1]
    t2 = TRANSLATIONS[lang2]
    
    p1_name = get_user_display_name(player1)
    p2_name = get_user_display_name(player2)

    if ovr1 > ovr2:
        win_chance_p1 = 0.84
    else:
        win_chance_p1 = 0.16

    winner = player1 if random.random() < win_chance_p1 else player2
    loser = player2 if winner == player1 else player1
    
    winner_name = get_user_display_name(winner)
    loser_name = get_user_display_name(loser)

    winner.coins += 100
    loser.coins = max(0, loser.coins - 50)

    # ELO только за битвы с реальными игроками (PVP)
    winner.elo = (winner.elo if hasattr(winner, "elo") else 1000) + 30
    loser.elo = max(0, (loser.elo if hasattr(loser, "elo") else 1000) - 25)
    user_manager.save_user(winner)
    user_manager.save_user(loser)

    team1_str = format_team_display(team1, lang1)
    team2_str = format_team_display(team2, lang2)

    text_win = (
        f"🎮 <b>{t1['battle_found']}</b>\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"🟢 <b>{p1_name}</b> (OVR {ovr1}):\n{team1_str}\n\n"
        f"🔴 <b>{p2_name}</b> (OVR {ovr2}):\n{team2_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t1['battle_result_win'].format(reward=100, penalty=0)}\n"
        f"💰 {t1['coins']}: {winner.coins}"
    )
    await bot.send_message(winner.user_id, text_win, reply_markup=get_battle_result_keyboard(lang1), parse_mode="HTML")

    text_lose = (
        f"🎮 <b>{t2['battle_found']}</b>\n\n"
        f"━━━━━━━━━━━━━━\n"
        f"🟢 <b>{p2_name}</b> (OVR {ovr2}):\n{team2_str}\n\n"
        f"🔴 <b>{p1_name}</b> (OVR {ovr1}):\n{team1_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{t2['battle_result_lose'].format(reward=0, penalty=50)}\n"
        f"💰 {t2['coins']}: {loser.coins}"
    )
    await bot.send_message(loser.user_id, text_lose, reply_markup=get_battle_result_keyboard(lang2), parse_mode="HTML")

    try:
        await message.delete()
    except:
        pass

@dp.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery):
    await callback.answer()

# ================ ЗАПУСК БОТА ================

# ================ КЛАНЫ: ОБРАБОТЧИКИ ================
def build_clans_page_text(user: UserData) -> str:
    t = TRANSLATIONS[user.language]
    if user.clan_id:
        clan = clan_manager.get_clan(user.clan_id)
        if not clan:
            user.clan_id = None
            user_manager.save_user(user)
            return "Вы не состоите в клане."
        privacy = "Открытый" if clan.is_open else "По приглашению"
        rating = clan_manager.clan_rating(clan)
        members_text = format_clan_members(clan)
        return (
            f"<b>{t['clans_title']}</b>\n\n"
            f"🏷️ <b>{clan.name}</b>\n"
            f"📝 {clan.description or '—'}\n"
            f"🔐 {privacy}\n"
            f"👥 Участники: {len(clan.members)}/11\n"
            f"🏆 Рейтинг клана: <b>{rating}</b>\n\n"
            f"<b>Состав:</b>\n{members_text}"
        )

    # не в клане
    return (
        f"<b>{t['clans_title']}</b>\n\n"
        "Создай свой клан или вступи в существующий.\n"
        "Стоимость создания: <b>100 💎</b>\n\n"
        "🔓 Открытый клан — можно вступить сразу.\n"
        "🔒 По приглашению — вступление только по приглашению главы."
    )


@dp.callback_query(F.data == "clans")
async def callback_clans(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    await state.clear()
    text = build_clans_page_text(user)
    await render_page(
        callback,
        image_basename="clans",
        text=text,
        reply_markup=get_clans_menu_keyboard(user),
    )


@dp.callback_query(F.data == "rating")
async def callback_rating(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    text = "<b>🏆 Рейтинг</b>\n\nВыберите таблицу ниже 👇"
    await render_page(
        callback,
        image_basename="rating",
        text=text,
        reply_markup=get_rating_menu_keyboard(user),
    )


@dp.callback_query(F.data == "rating_players")
async def callback_rating_players(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    # топ по Elo среди всех сохранённых пользователей
    users_sorted = sorted(user_manager.users.values(), key=lambda u: getattr(u, "elo", 0), reverse=True)
    top = users_sorted[:10]

    if not top:
        text = "Пока нет игроков в рейтинге."
    else:
        lines = ["<b>🏅 Рейтинг игроков</b>\n"]
        for i, u in enumerate(top, start=1):
            name = f"@{u.username}" if u.username else f"ID {u.user_id}"
            lines.append(f"{i}. <b>{html.escape(name)}</b> — 🏆 {u.elo}")
        text = "\n".join(lines)

    await render_page(
        callback,
        image_basename="rating",
        text=text,
        reply_markup=get_players_rating_keyboard(user),
    )


@dp.callback_query(F.data == "clan_create")
async def callback_clan_create(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    if user.clan_id:
        await callback.message.answer("Вы уже состоите в клане.")
        return
    if user.gems < 100:
        await callback.message.answer("Недостаточно алмазов. Нужно 100 💎 для создания клана.")
        return
    await state.set_state(ClanStates.creating_name)
    await callback.message.answer("Введите название клана (3–20 символов):")


@dp.message(ClanStates.creating_name)
async def clan_creating_name(message: Message, state: FSMContext):
    user = user_manager.get_user(message.from_user.id, message.from_user.username)
    name = (message.text or "").strip()
    if len(name) < 3 or len(name) > 20:
        await message.answer("Название должно быть 3–20 символов. Попробуй ещё раз:")
        return
    # уникальность
    for c in clan_manager.clans.values():
        if c.name.strip().lower() == name.lower():
            await message.answer("Клан с таким названием уже существует. Придумай другое:")
            return
    await state.update_data(name=name)
    await state.set_state(ClanStates.creating_description)
    await message.answer("Введите описание клана (до 150 символов):")


@dp.message(ClanStates.creating_description)
async def clan_creating_description(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if len(desc) > 150:
        await message.answer("Слишком длинно. До 150 символов. Попробуй ещё раз:")
        return
    await state.update_data(description=desc)
    await state.set_state(ClanStates.creating_privacy)
    user = user_manager.get_user(message.from_user.id, message.from_user.username)
    await message.answer("Выберите тип клана:", reply_markup=get_clan_privacy_keyboard(user))


@dp.callback_query(F.data.in_({"clan_privacy_open", "clan_privacy_invite"}))
async def callback_clan_privacy(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    if user.clan_id:
        await state.clear()
        await callback.message.answer("Вы уже состоите в клане.")
        return
    if user.gems < 100:
        await state.clear()
        await callback.message.answer("Недостаточно алмазов. Нужно 100 💎 для создания клана.")
        return
    data = await state.get_data()
    name = data.get("name", "Клан")
    description = data.get("description", "")
    is_open = callback.data == "clan_privacy_open"
    clan = clan_manager.create_clan(name=name, description=description, is_open=is_open, owner_id=user.user_id)
    user.gems -= 100
    user.clan_id = clan.clan_id
    user_manager.save_user(user)
    await state.clear()

    text = build_clans_page_text(user)
    await render_page(callback, image_basename="clans", text=text, reply_markup=get_clans_menu_keyboard(user), force_new_message=True)


@dp.callback_query(F.data == "clan_join_list")
async def callback_clan_join_list(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    lines = ["<b>Открытые кланы (доступные места):</b>\n"]
    shown = 0
    for clan in clan_manager.top_clans(limit=50):
        if not clan.is_open:
            continue
        if len(clan.members) >= 11:
            continue
        rating = clan_manager.clan_rating(clan)
        lines.append(f"✅ <b>{clan.name}</b> — 👥 {len(clan.members)}/11 — 🏆 {rating}")
        shown += 1
        if shown >= 10:
            break
    if shown == 0:
        lines = ["Сейчас нет открытых кланов с местами."]
    await render_page(
        callback,
        image_basename="clans",
        text="\n".join(lines),
        reply_markup=get_clans_join_list_keyboard(user),
    )


@dp.callback_query(F.data.startswith("clan_join:"))
async def callback_clan_join(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    if user.clan_id:
        await callback.message.answer("Сначала покинь текущий клан.")
        return
    clan_id = callback.data.split(":", 1)[1]
    clan = clan_manager.get_clan(clan_id)
    if not clan:
        await callback.message.answer("Клан не найден.")
        return
    if not clan.is_open:
        await callback.message.answer("В этот клан можно вступить только по приглашению.")
        return
    if len(clan.members) >= 11:
        await callback.message.answer("В клане нет свободных мест.")
        return
    clan.members[str(user.user_id)] = "player"
    user.clan_id = clan.clan_id
    user_manager.save_user(user)
    clan_manager.save_data()
    await callback.message.answer(f"Вы вступили в клан <b>{clan.name}</b>!", parse_mode="HTML")


@dp.callback_query(F.data == "clan_invites")
async def callback_clan_invites(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    await render_page(
        callback,
        image_basename="clans",
        text="<b>Ваши приглашения:</b>",
        reply_markup=get_clan_invites_keyboard(user),
    )


@dp.callback_query(F.data.startswith("clan_accept:"))
async def callback_clan_accept(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    if user.clan_id:
        await callback.message.answer("Сначала покинь текущий клан.")
        return
    clan_id = callback.data.split(":", 1)[1]
    clan = clan_manager.get_clan(clan_id)
    if not clan:
        await callback.message.answer("Клан не найден.")
        return
    if len(clan.members) >= 11:
        await callback.message.answer("В клане нет свободных мест.")
        return
    username = (user.username or "").lstrip("@").lower()
    if not username or username not in [u.lower() for u in clan.invites]:
        await callback.message.answer("Приглашение не найдено.")
        return
    clan.invites = [u for u in clan.invites if u.lower() != username]
    clan.members[str(user.user_id)] = "player"
    user.clan_id = clan.clan_id
    user_manager.save_user(user)
    clan_manager.save_data()
    await callback.message.answer(f"Вы вступили в клан <b>{clan.name}</b>!", parse_mode="HTML")


@dp.callback_query(F.data == "clan_invite")
async def callback_clan_invite(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    if not user.clan_id:
        await callback.message.answer("Вы не в клане.")
        return
    clan = clan_manager.get_clan(user.clan_id)
    if not clan or clan.owner_id != user.user_id:
        await callback.message.answer("Приглашать может только владелец клана.")
        return
    await state.set_state(ClanStates.inviting_username)
    await callback.message.answer("Введи ник пользователя (например @username), которого хочешь пригласить:")


@dp.message(ClanStates.inviting_username)
async def clan_inviting_username(message: Message, state: FSMContext):
    user = user_manager.get_user(message.from_user.id, message.from_user.username)
    clan = clan_manager.get_clan(user.clan_id) if user.clan_id else None
    if not clan or clan.owner_id != user.user_id:
        await state.clear()
        await message.answer("Приглашение отменено.")
        return
    nick = (message.text or "").strip().lstrip("@").lower()
    if not nick:
        await message.answer("Ник не распознан. Попробуй ещё раз:")
        return
    if nick in [u.lower() for u in clan.invites]:
        await state.clear()
        await message.answer("Этот ник уже приглашён.")
        return
    if len(clan.members) >= 11:
        await state.clear()
        await message.answer("В клане уже 11 участников. Нет мест.")
        return
    clan.invites.append(nick)
    clan_manager.save_data()
    await state.clear()
    await message.answer(f"Приглашение отправлено для @{nick}.")


@dp.callback_query(F.data == "clan_set_role")
async def callback_clan_set_role(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    clan = clan_manager.get_clan(user.clan_id) if user.clan_id else None
    if not clan or clan.owner_id != user.user_id:
        await callback.message.answer("Выдавать роли может только владелец клана.")
        return
    await state.set_state(ClanStates.setrole_username)
    await callback.message.answer("Введи ник участника (например @username), кому выдать роль:")


@dp.message(ClanStates.setrole_username)
async def clan_setrole_username(message: Message, state: FSMContext):
    owner = user_manager.get_user(message.from_user.id, message.from_user.username)
    clan = clan_manager.get_clan(owner.clan_id) if owner.clan_id else None
    if not clan or clan.owner_id != owner.user_id:
        await state.clear()
        await message.answer("Выдача роли отменена.")
        return
    nick = (message.text or "").strip().lstrip("@").lower()
    if not nick:
        await message.answer("Ник не распознан. Попробуй ещё раз:")
        return
    target_uid = None
    for uid_str in clan.members.keys():
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        u = user_manager.users.get(uid)
        if u and (u.username or "").lstrip("@").lower() == nick:
            target_uid = uid
            break
    if not target_uid:
        await message.answer("Участник с таким ником не найден в клане. Попробуй ещё раз:")
        return
    if target_uid == clan.owner_id:
        await state.clear()
        await message.answer("Нельзя изменить роль владельца.")
        return
    await state.update_data(target_uid=target_uid)
    await state.set_state(ClanStates.setrole_role)
    await message.answer("Выберите роль:", reply_markup=get_role_select_keyboard(owner))


@dp.callback_query(F.data.startswith("clan_role:"))
async def callback_clan_role(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    owner = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    clan = clan_manager.get_clan(owner.clan_id) if owner.clan_id else None
    if not clan or clan.owner_id != owner.user_id:
        await state.clear()
        await callback.message.answer("Выдача роли отменена.")
        return
    data = await state.get_data()
    target_uid = data.get("target_uid")
    role = callback.data.split(":", 1)[1]
    if role not in ("coach", "player"):
        await callback.message.answer("Неизвестная роль.")
        return
    clan.members[str(target_uid)] = role
    clan_manager.save_data()
    await state.clear()
    await callback.message.answer("Роль обновлена!")


@dp.callback_query(F.data == "clan_leave")
async def callback_clan_leave(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    if not user.clan_id:
        await callback.message.answer("Вы не состоите в клане.")
        return
    clan = clan_manager.get_clan(user.clan_id)
    if not clan:
        user.clan_id = None
        user_manager.save_user(user)
        await callback.message.answer("Клан не найден. Вы вышли из клана.")
        return

    is_owner = clan.owner_id == user.user_id
    # удалить участника
    clan.members.pop(str(user.user_id), None)
    user.clan_id = None
    user_manager.save_user(user)

    # если глава ушёл — передать владельца
    if is_owner and len(clan.members) > 0:
        # приоритет: тренер, потом игрок
        new_owner_id = None
        for uid_str, role in clan.members.items():
            if role == "coach":
                new_owner_id = int(uid_str)
                break
        if new_owner_id is None:
            new_owner_id = int(next(iter(clan.members.keys())))
        clan.owner_id = new_owner_id
        clan.members[str(new_owner_id)] = "owner"

    # если клан пустой — удалить
    if len(clan.members) == 0:
        clan_manager.clans.pop(clan.clan_id, None)
    clan_manager.save_data()

    await callback.message.answer("Вы покинули клан.")


@dp.callback_query(F.data == "clans_rating")
async def callback_clans_rating(callback: CallbackQuery):
    await callback.answer()
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    top = clan_manager.top_clans(limit=10)
    if not top:
        text = "Пока нет кланов."
    else:
        lines = ["<b>🏆 Рейтинг кланов</b>\n"]
        for i, clan in enumerate(top, start=1):
            rating = clan_manager.clan_rating(clan)
            lines.append(f"{i}. <b>{clan.name}</b> — 🏆 {rating} — 👥 {len(clan.members)}/11")
        text = "\n".join(lines)
    await render_page(
        callback,
        image_basename="clans",
        text=text,
        reply_markup=get_clans_rating_keyboard(user),
    )

async def main():
    logging.basicConfig(level=logging.INFO)
    print("🤖 Футбольный Коллекционер Бот запущен...")
    print(f"📁 Папка с изображениями: {IMAGES_PATH}")
    print(f"💾 Файл с данными: {user_manager.data_file}")
    print(f"🎲 Казино: 100 монет за бросок, выигрыш 500+10💎 при 4+")
    print(f"🎁 Бесплатные паки: 5 паков каждые 4 часа")
    print(f"✅ Карточки автоматически удаляются через {CARD_LIFETIME_SECONDS} секунд после открытия!")
    print(f"✅ Загружено карточек игроков: {len(FOOTBALL_PLAYERS)}")
    print(f"⚔️ Режим битв активирован!")
    print(f"📚 Коллекция: навигация и поиск активированы!")
    print(f"🖼️ Кеш изображений: активирован (мгновенная загрузка после первого просмотра)")
    print(f"👤 Username: автоматическое сохранение и отображение")

    # Команды бота (панель команд рядом с полем ввода)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="Открыть главное меню"),
            BotCommand(command="profile", description="Профиль"),
            BotCommand(command="packs", description="Пакеты"),
            BotCommand(command="minigames", description="Мини-игры"),
            BotCommand(command="clans", description="Кланы"),
            BotCommand(command="settings", description="Настройки"),
            BotCommand(command="help", description="Список команд"),
        ],
        scope=BotCommandScopeDefault(),
    )

    await dp.start_polling(bot)

def get_stars_shop_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    b = InlineKeyboardBuilder()
    b.button(text=t.get("topup_stars", "➕ Пополнить Stars"), callback_data="stars_topup")
    b.button(text=t.get("buy_diamonds_stars", "💎 Купить алмазы за Stars"), callback_data="stars_buy_diamonds")
    b.button(text=t["back"], callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()

def get_stars_topup_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    b = InlineKeyboardBuilder()
    for amt in STARS_TOPUP_OPTIONS:
        b.button(text=f"➕ {amt}⭐", callback_data=f"stars_topup_{amt}")
    b.button(text=t["back"], callback_data="stars_shop")
    b.adjust(1)
    return b.as_markup()

def get_stars_buy_diamonds_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    b = InlineKeyboardBuilder()
    for key, pack in DIAMONDS_FOR_STARS.items():
        b.button(text=f"{pack['diamonds']}💎 — {pack['cost_stars']}⭐", callback_data=f"stars_buy_{key}")
    b.button(text=t["back"], callback_data="stars_shop")
    b.adjust(1)
    return b.as_markup()

@dp.callback_query(F.data.in_({"stars_shop","shop"}))
async def callback_stars_shop(callback: CallbackQuery):
    user = get_user_data(callback.from_user.id)
    t = TRANSLATIONS[user.language]
    stars = getattr(user, "stars_balance", 0)
    text = (
        f"💵 <b>Магазин $</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⭐ Баланс Stars: <b>{stars}</b>\n"
        f"💎 Алмазы: <b>{user.gems}</b>\n\n"
        f"Выберите действие:"
    )
    await render_page(callback, image_basename="diamonds", text=text, reply_markup=get_stars_shop_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "stars_topup")
async def callback_stars_topup(callback: CallbackQuery, state: FSMContext):
    user = get_user_data(callback.from_user.id)
    t = TRANSLATIONS[user.language]
    stars = getattr(user, "stars_balance", 0)

    text = (
        f"⭐ <b>Пополнение Stars</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"Ваш баланс: <b>{stars}</b>⭐\n\n"
        f"Введите сумму Stars, которую хотите зачислить в игру (числом).\n"
        f"Например: <b>250</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t["back"], callback_data="stars_shop")]])
    await render_page(callback, image_basename="diamonds", text=text, reply_markup=kb)
    await state.set_state(StarsTopUpStates.waiting_amount)
    await callback.answer()


@dp.message(StarsTopUpStates.waiting_amount)
async def message_stars_topup_amount(message: Message, state: FSMContext):
    user = get_user_data(message.from_user.id)
    txt = (message.text or "").strip()

    if not txt.isdigit():
        await message.answer("Введите сумму числом (например 250).")
        return

    amt = int(txt)
    if amt <= 0:
        await message.answer("Сумма должна быть больше 0.")
        return
    # Разумный верхний лимит, чтобы не улететь в космос
    if amt > 50000:
        await message.answer("Слишком большая сумма. Введите число до 50000.")
        return

    prices = [LabeledPrice(label=f"Пополнение {amt}⭐", amount=amt)]  # для Stars должен быть 1 item
    await message.bot.send_invoice(
        chat_id=message.from_user.id,
        title=f"Пополнение Stars: {amt}⭐",
        description="Stars зачислятся на ваш внутренний баланс в игре.",
        payload=f"stars_topup:{amt}:{message.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await state.clear()


@dp.callback_query(F.data.startswith("stars_topup_"))
async def callback_stars_topup_invoice(callback: CallbackQuery):
    try:
        amt = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка суммы", show_alert=True)
        return
    if amt not in STARS_TOPUP_OPTIONS:
        await callback.answer("Недоступно", show_alert=True)
        return

    prices = [LabeledPrice(label=f"Пополнение {amt}⭐", amount=amt)]  # для Stars должен быть 1 item
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Пополнение Stars: {amt}⭐",
        description="Stars зачислятся на ваш внутренний баланс в игре.",
        payload=f"stars_topup:{amt}:{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await callback.answer("Оплата открыта ⭐")

@dp.callback_query(F.data == "stars_buy_diamonds")
async def callback_stars_buy_diamonds(callback: CallbackQuery):
    user = get_user_data(callback.from_user.id)
    t = TRANSLATIONS[user.language]
    stars = getattr(user, "stars_balance", 0)
    text = (
        f"💎 <b>{t.get('stars_spend_title','Алмазы за Stars')}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"⭐ Баланс Stars: <b>{stars}</b>\n\n"
        f"Выберите пакет:"
    )
    await safe_edit_or_send(callback.message, text, reply_markup=get_stars_buy_diamonds_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data.startswith("stars_buy_"))
async def callback_stars_buy_diamonds_apply(callback: CallbackQuery):
    user = get_user_data(callback.from_user.id)
    key = callback.data.replace("stars_buy_", "")
    pack = DIAMONDS_FOR_STARS.get(key)
    if not pack:
        await callback.answer("Пакет не найден", show_alert=True)
        return
    cost = pack["cost_stars"]
    stars = getattr(user, "stars_balance", 0)
    if stars < cost:
        await callback.answer("❌ Недостаточно Stars", show_alert=True)
        return
    user.stars_balance = stars - cost
    user.gems += pack["diamonds"]
    save_user_data(user)
    await callback.answer("✅ Успешно!")
    await callback_stars_buy_diamonds(callback)

@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    parts = payload.split(":")
    # ожидаем: stars_topup:<amount>:<user_id>
    if len(parts) != 3 or parts[0] != "stars_topup":
        return
    try:
        amt = int(parts[1])
        uid = int(parts[2])
    except Exception:
        return
    if uid != message.from_user.id:
        return
    if sp.currency != "XTR" or sp.total_amount != amt:
        return
    user = get_user_data(uid)
    user.stars_balance = getattr(user, "stars_balance", 0) + amt
    save_user_data(user)
    logger.info(f"[STARS TOPUP] user_id={uid} +{amt}⭐ total={user.stars_balance}")
    await message.answer(f"✅ Stars зачислены: +{amt}⭐\nВаш баланс: {user.stars_balance}⭐")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")