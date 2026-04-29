import os
import logging
import asyncio
import json
import re
import zipfile
import shutil
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOGIN = os.getenv("SITE_LOGIN")
PASSWORD = os.getenv("SITE_PASSWORD")
ALLOWED_TELEGRAM_IDS = os.getenv("ALLOWED_TELEGRAM_IDS", "")

# Настройка путей
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(BASE_DIR, "bot_debug.log")
downloads_path = os.path.join(BASE_DIR, "downloads")
accounts_path = os.path.join(BASE_DIR, "accounts.json")
unpacked_path = os.path.join(BASE_DIR, "unpacked")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_path, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

os.makedirs(downloads_path, exist_ok=True)
os.makedirs(unpacked_path, exist_ok=True)

# --- Вспомогательные функции для аккаунтов ---

def parse_allowed_ids():
    ids = set()
    for value in ALLOWED_TELEGRAM_IDS.split(","):
        value = value.strip()
        if value.isdigit(): ids.add(int(value))
    return ids

AUTHORIZED_USER_IDS = parse_allowed_ids()

def is_authorized(message: types.Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    return user_id in AUTHORIZED_USER_IDS

def read_accounts():
    if not os.path.exists(accounts_path):
        return {"accounts": [], "active_index": None}
    with open(accounts_path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_accounts(data):
    with open(accounts_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def active_account():
    data = read_accounts()
    idx = data.get("active_index")
    accounts = data.get("accounts", [])
    if idx is None or not (0 <= idx < len(accounts)): return None
    return accounts[idx]

# --- Логика парсинга и работы с файлами ---

def parse_release_info(row_info: str, fallback_artist: str = "", fallback_release: str = ""):
    info = " ".join(row_info.split())
    # Извлекаем UPC
    upc_match = re.search(r"(upc|ean)\s*:\s*([0-9]{8,20})", info, flags=re.I)
    # Извлекаем дату
    date_match = re.search(r"релиз:\s*([0-9]{2}-[0-9]{2}-[0-9]{4})", info, flags=re.I)
    
    # Пытаемся найти версию в скобках из названия
    version = ""
    version_match = re.search(r"\(([^)]+)\)", fallback_release)
    if version_match:
        version = version_match.group(1).strip()

    return {
        "title": fallback_release.strip(),
        "artists": [a.strip() for a in fallback_artist.split(",") if a.strip()],
        "release_date": date_match.group(1) if date_match else "",
        "upc": upc_match.group(2) if upc_match else "",
        "version": version
    }

def extract_zip_assets(zip_path: str):
    stem = Path(zip_path).stem
    target_dir = os.path.join(unpacked_path, stem)
    if os.path.exists(target_dir): shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(target_dir)
    
    cover_path = None
    for root, _, files in os.walk(target_dir):
        for f in files:
            if f.lower().endswith((".jpg", ".jpeg", ".png")):
                cover_path = os.path.join(root, f)
                break
    return {"cover_path": cover_path}

# --- Взаимодействие с браузером (MusicAlligator) ---

async def fill_input_by_label(page, label_text: str, value: str):
    if not value: return False
    locator = page.locator(f'label:has-text("{label_text}") >> xpath=following::input[1]').first
    if await locator.count() > 0:
        await locator.click()
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        await locator.fill(value)
        return True
    return False

async def fill_input_by_prompt(page, prompt_text: str, value: str):
    if not value: return False
    locator = page.locator(f'input[placeholder*="{prompt_text}" i]').first
    if await locator.count() > 0:
        await locator.click()
        await page.keyboard.press("Meta+A")
        await page.keyboard.press("Backspace")
        await locator.fill(value)
        await page.keyboard.press("Enter")
        return True
    return False

async def fill_select_input(page, field_label: str, value: str):
    if not value: return False
    root = page.locator(f'div:has(label p:has-text("{field_label}"))').first
    input_field = root.locator('input[type="text"]').first
    if await input_field.count() > 0:
        await input_field.click()
        await input_field.fill(value)
        await asyncio.sleep(1)
        await page.keyboard.press("Enter")
        return True
    return False

async def enable_toggle_by_text(page, toggle_text: str):
    root = page.locator(f'div.row-toggle:has(p:has-text("{toggle_text}"))').first
    checkbox = root.locator('input[type="checkbox"]')
    if await checkbox.count() > 0 and not await checkbox.is_checked():
        await root.click()
        await asyncio.sleep(0.5)

async def set_artist_with_create_fallback(page, label_text: str, artist_name: str):
    await fill_select_input(page, label_text, artist_name)
    await asyncio.sleep(1.5)
    
    create_btn = page.locator('button:has-text("Создать исполнителя")').last
    if await create_btn.count() > 0 and await create_btn.is_visible():
        await create_btn.click()
        await asyncio.sleep(1)
        # Нажимаем "Нет" на вопросы про Apple и Spotify
        no_buttons = page.locator('button:has-text("Нет")')
        for i in range(await no_buttons.count()):
            await no_buttons.nth(i).click()
            await asyncio.sleep(0.3)
        # Финальная кнопка создания в модалке
        await page.locator('div.modal-footer button:has-text("Создать исполнителя")').click()
        await asyncio.sleep(1)

async def upload_to_musicalligator(release_meta: dict, zip_path: str):
    account = active_account()
    if not account: return {"status": "error", "message": "Аккаунт не выбран"}
    
    assets = extract_zip_assets(zip_path)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True) # Поставь False для тестов
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        try:
            # 1. Авторизация
            await page.goto("https://app.musicalligator.ru/auth/signin")
            await page.fill('input[type="email"]', account["email"])
            await page.fill('input[type="password"]', account["password"])
            await page.click('button:has-text("Продолжить")')
            await page.wait_for_url("**/releases**", timeout=60000)
            
            # 2. Создание релиза
            await page.locator('button:has-text("Новый релиз")').first.click()
            await page.wait_for_selector('input[type="file"][accept*="image"]')
            
            if assets["cover_path"]:
                await page.locator('input[type="file"][accept*="image"]').set_input_files(assets["cover_path"])
            
            # 3. Заполнение артистов
            artists = release_meta["artists"]
            await set_artist_with_create_fallback(page, "Исполнитель", artists[0])
            
            for i in range(1, len(artists)):
                add_btn = page.locator('.ui-artists-select button.add-btn, .ui-artists-select i.gs-plus').last
                await add_btn.click()
                await set_artist_with_create_fallback(page, "Дополнительный исполнитель", artists[i])
            
            # 4. Название и версия
            await fill_input_by_prompt(page, "Введите название релиза", release_meta["title"])
            if release_meta["version"]:
                await fill_input_by_prompt(page, "Введите версию релиза", release_meta["version"])
            
            # 5. Лейбл (первый артист)
            await fill_select_input(page, "Лейбл", artists[0])
            create_label_btn = page.locator('button:has-text("Проверить и создать лейбл")')
            if await create_label_btn.count() > 0 and await create_label_btn.is_visible():
                await create_label_btn.click()
            
            # 6. Дата и UPC
            if release_meta["release_date"]:
                await enable_toggle_by_text(page, "Оригинальная дата релиза")
                await fill_input_by_label(page, "Оригинальная дата релиза", release_meta["release_date"].replace("-", "."))
            
            if release_meta["upc"]:
                await enable_toggle_by_text(page, "У меня есть свой EAN/UPC")
                await fill_input_by_prompt(page, "EAN/UPC", release_meta["upc"])
            
            await asyncio.sleep(2)
            await page.goto("https://app.musicalligator.ru/releases")
            await browser.close()
            return {"status": "success", "message": f"Загружено в {account['note']}"}
            
        except Exception as e:
            await page.screenshot(path="error_alligator.png")
            await browser.close()
            return {"status": "error", "message": str(e)}

# --- Скрапинг OR Distribution ---

async def scrape_ordistribution(artist: str, release: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto("https://ordistribution.com/login")
            await page.fill('input[type="email"]', LOGIN)
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            await asyncio.sleep(5)
            
            search_input = page.locator('input[type="search"]').first
            await search_input.fill(release)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)
            
            row = page.locator(f'div:has-text("{artist}"):has-text("{release}")').first
            info_text = await row.inner_text()
            
            await row.locator('button:has-text("Подробнее")').click()
            async with page.expect_download() as download_info:
                await page.locator('button:has-text("Скачать ZIP")').click()
            
            download = await download_info.value
            path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(path)
            await browser.close()
            return {"status": "success", "info": info_text, "file_path": path}
        except Exception as e:
            await browser.close()
            return {"status": "error", "message": str(e)}

# --- Обработчики Telegram ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_authorized(message):
        await message.answer("Пришли: `Артист - Название` или настрой /accounts")

@dp.message(Command("accounts"))
async def cmd_accounts(message: types.Message):
    if not is_authorized(message): return
    args = message.text.replace("/accounts", "").strip()
    data = read_accounts()
    
    if not args:
        accs = data.get("accounts", [])
        active = data.get("active_index")
        msg = "Аккаунты:\n" + "\n".join([f"{'✅' if active==i else '•'} {i+1}. {a['note']}" for i,a in enumerate(accs)])
        await message.answer(msg + "\n\n/accounts add Note | Email | Pass\n/accounts use N")
    elif args.startswith("add"):
        parts = [p.strip() for p in args[3:].split("|")]
        data["accounts"].append({"note": parts[0], "email": parts[1], "password": parts[2]})
        write_accounts(data)
        await message.answer("Добавлено")
    elif args.startswith("use"):
        data["active_index"] = int(args[3:].strip()) - 1
        write_accounts(data)
        await message.answer("Переключено")

@dp.message(F.text)
async def handle_msg(message: types.Message):
    if not is_authorized(message) or " - " not in message.text: return
    
    artist, release = message.text.split(" - ", 1)
    status = await message.answer("⏳ Работаю...")
    
    # 1. Забираем с OR
    res_or = await scrape_ordistribution(artist.strip(), release.strip())
    if res_or["status"] == "error":
        await status.edit_text(f"❌ Ошибка OR: {res_or['message']}")
        return
        
    # 2. Парсим данные
    meta = parse_release_info(res_or["info"], artist, release)
    
    # 3. Грузим на Alligator
    res_al = await upload_to_musicalligator(meta, res_or["file_path"])
    
    if res_al["status"] == "success":
        await status.edit_text(f"✅ Готово!\n{res_al['message']}")
    else:
        await status.edit_text(f"❌ Ошибка Alligator: {res_al['message']}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
