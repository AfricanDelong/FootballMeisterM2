import asyncio
import logging
import random
import json
import os
import re
import html
from datetime import datetime
from typing import Dict, List, Optional, Union
from enum import Enum

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    CallbackQuery, Message, FSInputFile
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

# ================ ПУТИ К КАРТИНКАМ ================
IMAGES_PATH = "images"
BACKGROUND_IMAGE_FILENAME = "backgrauond.png"
os.makedirs(IMAGES_PATH, exist_ok=True)

CARD_LIFETIME_SECONDS = 5

# ================ НОРМАЛИЗАЦИЯ РЕДКОСТИ ================
RARITY_ALIASES = {
    "common": "common", "обычная": "common", "обыкновенная": "common", "обычный": "common",
    "rare": "rare", "редкая": "rare", "редкий": "rare",
    "epic": "epic", "эпическая": "epic", "эпик": "epic",
    "legendary": "legendary", "легендарная": "legendary", "лега": "legendary",
    "mythic": "mythic", "мифическая": "mythic", "мифик": "mythic",
}

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


async def show_page_with_bg(target_message: Message, bg_filename: str, caption: str, reply_markup=None):
    """Показывает страницу с фоном (если файл существует), иначе обычным текстом.
    Работает и для Message, и для callback.message (передаём именно Message).
    """
    bg_path = os.path.join(IMAGES_PATH, bg_filename)
    try:
        # чтобы не словить 'message to edit not found' — проще удалить и прислать заново
        await target_message.delete()
    except TelegramBadRequest:
        pass

    if os.path.exists(bg_path):
        await target_message.answer_photo(
            photo=FSInputFile(bg_path),
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    else:
        await target_message.answer(caption, reply_markup=reply_markup, parse_mode="HTML")

def get_card_media(card: dict) -> Optional[Union[str, FSInputFile]]:
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

# ================ СПЛАВКА ДУБЛИКАТОВ ================
RARITY_UPGRADE_MAP = {
    "common": "rare",
    "rare": "epic",
    "epic": "legendary",
    "legendary": "mythic",
}

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

# ================ ЯЗЫКОВЫЕ НАСТРОЙКИ ================
class Language(Enum):
    RU = "ru"
    EN = "en"

TRANSLATIONS = {
    Language.RU: {
        "main_menu": "⚽ Футбольный Коллекционер",
        "packs": "📦 Паки",
        "collection": "📚 Коллекция",
        "mini_game": "🎲 Казино",
        "settings": "⚙️ Настройки",
        "rating": "🏆 Рейтинг",
        "rating_players": "🏅 Рейтинг игроков",
        "rating_clans": "🏆 Рейтинг кланов",
        "profile": "👤 Профиль",
        "battle_mode": "⚔️ Режим сражения",
        "coins": "💰 Монеты",
        "gems": "💎 Алмазы",
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
        "sort_common": "Обычные",
        "sort_rare": "Редкие",
        "sort_epic": "Эпические",
        "sort_legendary": "Легендарные",
        "sort_mythic": "Мифические",
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
        "play_casino": "🎲 Сыграть в казино",
        "dice_rules": "Правила игры:\n🎲 Кубик 1-6\n💎 4,5,6 → +500 монет, +10 алмазов\n💔 1,2,3 → -100 монет",
        "back_to_casino": "◀️ Назад в казино",
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
        "rating": "🏆 Рейтинг",
        "rating_players": "🏅 Рейтинг игроков",
        "rating_clans": "🏆 Рейтинг кланов",
        "profile": "👤 Profile",
        "battle_mode": "⚔️ Battle mode",
        "coins": "💰 Coins",
        "gems": "💎 Gems",
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
        "sort_common": "Common",
        "sort_rare": "Rare",
        "sort_epic": "Epic",
        "sort_legendary": "Legendary",
        "sort_mythic": "Mythic",
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
        "play_casino": "🎲 Play casino",
        "dice_rules": "Game rules:\n🎲 Dice 1-6\n💎 4,5,6 → +500 coins, +10 gems\n💔 1,2,3 → -100 coins",
        "back_to_casino": "◀️ Back to casino",
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
}

PACK_PRICES = {
    "basic": {"coins": 100, "gems": 0},
    "premium": {"coins": 0, "gems": 50},
    "free": {"coins": 0, "gems": 0}
}

# ================ КЛАССЫ ДЛЯ УПРАВЛЕНИЯ ДАННЫМИ ================
class UserData:
    def __init__(self, user_id: int, username: str = None):
        self.user_id = user_id
        self.username = username
        self.coins = 1000
        self.gems = 0
        self.collection = []
        self.language = Language.RU
        self.card_id_counter = 1
        self.free_packs = 5
        self.last_free_pack_time = datetime.now()
        self.dice_wins = 0
        self.dice_losses = 0
        self.dice_total = 0

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "username": self.username,
            "coins": self.coins,
            "gems": self.gems,
            "collection": self.collection,
            "language": self.language.value,
            "card_id_counter": self.card_id_counter,
            "free_packs": self.free_packs,
            "last_free_pack_time": self.last_free_pack_time.isoformat() if self.last_free_pack_time else None,
            "dice_wins": self.dice_wins,
            "dice_losses": self.dice_losses,
            "dice_total": self.dice_total
        }

    @classmethod
    def from_dict(cls, data):
        user = cls(data["user_id"])
        user.username = data.get("username")
        user.coins = data.get("coins", 1000)
        user.gems = data.get("gems", 0)
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

user_manager = UserManager()

# Совместимость с хостингами на Python 3.8/3.9 (и старым кодом)
def save_user_data(_: Optional['UserData'] = None):
    """Сохраняет user_data.json. Аргумент оставлен для совместимости."""
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
    builder.button(text=t["packs"], callback_data="packs")
    builder.button(text=t["collection"], callback_data="collection_start")
    builder.button(text=t["profile"], callback_data="profile")
    builder.button(text=t["mini_game"], callback_data="mini_game")
    builder.button(text=t["battle_mode"], callback_data="battle_mode")
    builder.button(text=t["rating"], callback_data="rating_menu")
    builder.button(text=t["settings"], callback_data="settings")
    builder.adjust(1)
    return builder.as_markup()


def get_rating_menu_keyboard(lang: Language):
    t = TRANSLATIONS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t.get("rating_players", "🏅 Рейтинг игроков"), callback_data="rating_players")
    builder.button(text=t.get("rating_clans", "🏆 Рейтинг кланов"), callback_data="rating_clans")
    builder.button(text=t.get("back", "⬅ Назад"), callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

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
    builder.button(text=t["roll_dice"], callback_data="roll_dice")
    if show_back:
        builder.button(text=t["back_to_menu"], callback_data="main_menu")
    builder.adjust(1)
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
    builder.button(text=t["back"], callback_data="mini_game")
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
        builder.button(text=t["close"], callback_data=f"collection_return_{current_index}")
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
    subtitle = "Выберите раздел ниже 👇" if user.language == Language.RU else "Choose a section below 👇"
    return f"⚽ <b>{t['main_menu']}</b>\n<i>{subtitle}</i>"

def get_text_card_detail(card: dict, lang: Language):
    t = TRANSLATIONS[lang]
    rarity_emoji = {
        "common": "🟢", "rare": "🔵", "epic": "🟣",
        "legendary": "👑", "mythic": "🤍💎"
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
        "legendary": "👑", "mythic": "🤍💎"
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
    t = TRANSLATIONS[user.language]
    
    total = len(user.collection)
    common = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "common"])
    rare = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "rare"])
    epic = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "epic"])
    legendary = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "legendary"])
    mythic = len([c for c in user.collection if normalize_rarity(c.get("rarity")) == "mythic"])
    user.check_free_packs_refresh()
    
    display_name = get_user_display_name(user)
    
    if user.language == Language.RU:
        title = f"👤 <b>Ваш профиль</b> {display_name}"
        balance = "Баланс"
        stats = "Статистика"
        casino = "Казино"
    else:
        title = f"👤 <b>Your profile</b> {display_name}"
        balance = "Balance"
        stats = "Stats"
        casino = "Casino"
    
    text = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 <b>{balance}</b>\n"
        f"{t['coins']}: <b>{user.coins}</b> 🪙\n"
        f"{t['gems']}: <b>{user.gems}</b> 💎\n"
        f"━━━━━━━━━━━━━━\n"
        f"📊 <b>{stats}</b>\n"
        f"📚 {t['collection']}: <b>{total}</b>\n"
        f"🟢 {t['sort_common']}: <b>{common}</b>\n"
        f"🔵 {t['sort_rare']}: <b>{rare}</b>\n"
        f"🟣 {t['sort_epic']}: <b>{epic}</b>\n"
        f"👑 {t['sort_legendary']}: <b>{legendary}</b>\n"
        f"🤍💎 {t['sort_mythic']}: <b>{mythic}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎁 {t['free_packs']}: <b>{user.free_packs}</b>/5\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎲 <b>{casino}</b>\n"
        f"✅ {t['wins']}: <b>{user.dice_wins}</b>\n"
        f"❌ {t['losses']}: <b>{user.dice_losses}</b>\n"
        f"📌 {t['total_games']}: <b>{user.dice_total}</b>"
    )
    
    try:
        if callback.message.photo:
            await callback.message.delete()
            await callback.message.answer(text, reply_markup=get_profile_keyboard(user.language), parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=get_profile_keyboard(user.language), parse_mode="HTML")
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=get_profile_keyboard(user.language), parse_mode="HTML")

@dp.callback_query(F.data == "packs")
async def callback_packs(callback: CallbackQuery):
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    t = TRANSLATIONS[user.language]
    caption = (
        f"📦 <b>{t['packs']}</b>\n\n"
        + (f"💰 {t['coins']}: <b>{user.coins}</b>   {t['gems']}: <b>{user.gems}</b>\n\n"
           if user.language == Language.RU else f"💰 {t['coins']}: <b>{user.coins}</b>   {t['gems']}: <b>{user.gems}</b>\n\n")
        + ("Выберите пак ниже 👇" if user.language == Language.RU else "Choose a pack below 👇")
    )
    await show_page_with_bg(callback.message, "packs.png", caption, reply_markup=get_packs_keyboard(user.language))
@dp.callback_query(F.data == "rating_menu")
async def callback_rating_menu(callback: CallbackQuery):
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    t = TRANSLATIONS[user.language]
    caption = f"🏆 <b>{t['rating']}</b>\n\nВыберите раздел 👇" if user.language == Language.RU else f"🏆 <b>{t['rating']}</b>\n\nChoose a section 👇"
    await show_page_with_bg(callback.message, "rating.png", caption, reply_markup=get_rating_menu_keyboard(user.language))

@dp.callback_query(F.data == "rating_players")
async def callback_rating_players(callback: CallbackQuery):
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    t = TRANSLATIONS[user.language]
    # топ-20 по Elo
    users_sorted = sorted(user_manager.users.values(), key=lambda u: getattr(u, "elo", 0), reverse=True)
    top = users_sorted[:20]
    lines = []
    for i, u in enumerate(top, 1):
        uname = f"@{u.username}" if getattr(u, "username", None) else str(u.user_id)
        lines.append(f"{i}. {uname} — <b>{getattr(u, 'elo', 0)}</b>")
    body = "\n".join(lines) if lines else ("Пока нет игроков." if user.language == Language.RU else "No players yet.")
    caption = f"🏅 <b>{t['rating_players']}</b>\n\n{body}"
    kb = InlineKeyboardBuilder()
    kb.button(text=t["back"], callback_data="rating_menu")
    kb.adjust(1)
    await show_page_with_bg(callback.message, "rating.png", caption, reply_markup=kb.as_markup())

@dp.callback_query(F.data == "rating_clans")
async def callback_rating_clans(callback: CallbackQuery):
    user = user_manager.get_user(callback.from_user.id, callback.from_user.username)
    t = TRANSLATIONS[user.language]
    # Если кланов нет в этой версии — показываем заглушку
    caption = f"🏆 <b>{t['rating_clans']}</b>\n\nСистема кланов в этой версии бота не подключена." if user.language == Language.RU else f"🏆 <b>{t['rating_clans']}</b>\n\nClans are not enabled in this bot version."
    kb = InlineKeyboardBuilder()
    kb.button(text=t["back"], callback_data="rating_menu")
    kb.adjust(1)
    await show_page_with_bg(callback.message, "rating.png", caption, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("buy_"))
async def callback_buy_pack(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    t = TRANSLATIONS[user.language]
    
    try:
        await callback.answer("📦 Открываю пак…")
    except TelegramBadRequest:
        return
    
    pack_type = callback.data.split("_")[1]
    price = PACK_PRICES[pack_type]
    
    if pack_type == "basic" and user.coins < price["coins"]:
        await callback.answer(t["not_enough_coins"], show_alert=True)
        return
    elif pack_type == "premium" and user.gems < price["gems"]:
        await callback.answer(t["not_enough_gems"], show_alert=True)
        return
    
    if pack_type == "basic":
        user.coins -= price["coins"]
    else:
        user.gems -= price["gems"]
    
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
        f"🎮 <b>{t['mini_game']}</b>\\n"
        f"━━━━━━━━━━━━━━\\n"
        f"1️⃣ {t['play_casino']}\\n"
        f"━━━━━━━━━━━━━━\\n"
        f"{t.get('choose_game', 'Выберите игру:')}"
    )

    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=get_mini_game_keyboard(user.language), parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(text, reply_markup=get_mini_game_keyboard(user.language), parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=get_mini_game_keyboard(user.language), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "play_casino")
async def callback_play_casino(callback: CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    
    text = get_text_casino(user)

    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=get_casino_keyboard(user.language, show_back=True), parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(text, reply_markup=get_casino_keyboard(user.language, show_back=True), parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=get_casino_keyboard(user.language, show_back=True), parse_mode="HTML")
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
    if callback.message.photo:
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
    else:
        try:
            await callback.message.edit_text(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
        except TelegramBadRequest:
            await callback.message.answer(text, reply_markup=get_settings_keyboard(user.language), parse_mode="HTML")
    await callback.answer()

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
    
    if not user.collection:
        t = TRANSLATIONS[user.language]
        text = t["empty_collection"]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=t["back"], callback_data="main_menu")]]
        )
        await callback.message.delete()
        await callback.message.answer(text, reply_markup=keyboard)
        return
    
    sorted_collection = get_sorted_collection(user.collection)
    await show_collection_card(callback.message, user, sorted_collection, 0)

@dp.callback_query(F.data.startswith("collection_prev_"))
async def callback_collection_prev(callback: CallbackQuery):
    try:
        await callback.answer()
    except TelegramBadRequest:
        return
    
    user_id = callback.from_user.id
    username = callback.from_user.username
    user = user_manager.get_user(user_id, username)
    sorted_collection = get_sorted_collection(user.collection)
    
    current_index = int(callback.data.split("_")[2])
    
    if current_index > 0:
        await show_collection_card(callback.message, user, sorted_collection, current_index - 1)
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
    sorted_collection = get_sorted_collection(user.collection)
    
    current_index = int(callback.data.split("_")[2])
    
    if current_index < len(sorted_collection) - 1:
        await show_collection_card(callback.message, user, sorted_collection, current_index + 1)
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
    sorted_collection = get_sorted_collection(user.collection)
    
    current_index = int(callback.data.split("_")[2])
    card = sorted_collection[current_index]
    
    media = get_card_media(card)
    
    if media:
        msg = await callback.message.answer_photo(
            media,
            caption=get_text_card_detail(card, user.language),
            reply_markup=get_card_detail_keyboard(user, card, from_collection=True, current_index=current_index),
            parse_mode="HTML"
        )
        await save_tg_file_id(card, msg)
    else:
        await callback.message.answer(
            get_text_card_detail(card, user.language),
            reply_markup=get_card_detail_keyboard(user, card, from_collection=True, current_index=current_index),
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
    sorted_collection = get_sorted_collection(user.collection)
    
    current_index = int(callback.data.split("_")[2])
    
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
        await callback.message.edit_text(t["battle_search_cancelled"])
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
