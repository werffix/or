import os
import logging
import asyncio
import json
import re
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

# Настройка путей и логирования
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(BASE_DIR, "bot_debug.log")
downloads_path = os.path.join(BASE_DIR, "downloads")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_path, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

os.makedirs(downloads_path, exist_ok=True)

def parse_release_info(text, fallback_artist="", fallback_release=""):
    """Парсинг метаданных из текста строки OR"""
    info = {
        "artists": fallback_artist,
        "title": fallback_release,
        "subtitle": "",
        "upc": "",
        "release_date": ""
    }
    
    # Поиск UPC (12-13 цифр)
    upc_match = re.search(r'\b\d{12,13}\b', text)
    if upc_match:
        info["upc"] = upc_match.group(0)
        
    # Поиск даты (ДД.ММ.ГГГГ)
    date_match = re.search(r'\b\d{2}\.\d{2}\.\d{4}\b', text)
    if date_match:
        info["release_date"] = date_match.group(0)

    # Попытка найти артистов (обычно до названия релиза или в спец. поле)
    # Здесь логика зависит от того, как именно копируется текст из OR
    return info

async def upload_to_musicalligator(meta):
    """Логика заполнения релиза на MusicAlligator"""
    logger.info(f"Начало загрузки на MusicAlligator: {meta['title']}")
    
    async with async_playwright() as p:
        # headless=True для сервера, False если хочешь видеть процесс
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
        page = await context.new_page()
        
        try:
            # 1. Авторизация на MusicAlligator (замени URL на страницу входа если нужно)
            await page.goto("https://app.musicalligator.ru/releases")
            # Здесь должна быть проверка авторизации. Если сессии нет, нужно добавить login/pass
            
            # Предположим, мы уже нажали "Создать релиз" или находимся в черновике
            # Для теста используем URL из твоего HTML
            await page.goto("https://app.musicalligator.ru/releases/334360/edit/release") 
            await asyncio.sleep(5)

            # --- ЗАПОЛНЕНИЕ ---

            # Название релиза
            await page.fill('input[name="title"]', meta['title'])

            # Работа с артистами
            artists_list = [a.strip() for a in meta['artists'].split(',')]
            
            async def handle_artist(name, index):
                logger.info(f"Обработка артиста: {name}")
                # Находим нужный инпут по индексу
                inputs = page.locator('input[placeholder="Выберите исполнителя"]')
                current_input = inputs.nth(index)
                
                await current_input.click()
                await current_input.fill(name)
                await asyncio.sleep(2)
                
                # Проверка кнопки "Создать исполнителя"
                create_btn = page.locator('button:has-text("Создать исполнителя")')
                if await create_btn.is_visible():
                    await create_btn.click()
                    await asyncio.sleep(1)
                    # Выбираем "Нет" в модальном окне
                    # Ищем радиокнопки "Нет" (обычно их две: для Apple и Spotify)
                    no_labels = page.locator('label:has-text("Нет")')
                    count = await no_labels.count()
                    for i in range(count):
                        await no_labels.nth(i).click()
                    
                    await page.click('div.modal-footer button:has-text("Создать исполнителя")')
                    await asyncio.sleep(2)
                else:
                    await page.keyboard.press("Enter")

            # Основной артист
            await handle_artist(artists_list[0], 0)

            # Дополнительные артисты
            for i, extra_artist in enumerate(artists_list[1:], start=1):
                await page.click('button.-more') # Кнопка "+"
                await asyncio.sleep(1)
                await handle_artist(extra_artist, i)

            # Версия релиза (Подзаголовок)
            if meta.get('subtitle'):
                await page.fill('input[name="version"]', meta['subtitle'])

            # Лейбл (первый артист)
            await page.fill('input[placeholder="Выберите лейбл"]', artists_list[0])
            await asyncio.sleep(2)
            create_label = page.locator('button:has-text("создать лейбл")')
            if await create_label.is_visible():
                await create_label.click()
                await page.wait_for_selector('button:has-text("Проверить и создать лейбл")')
                await page.click('button:has-text("Проверить и создать лейбл")')
            else:
                await page.keyboard.press("Enter")

            # Оригинальная дата релиза
            if meta.get('release_date'):
                # Клик по тумблеру (ищем label связанный с датой)
                await page.click('label:has-text("Оригинальная дата релиза")')
                await page.fill('input[placeholder="ДД.ММ.ГГГГ"]', meta['release_date'])

            # UPC
            if meta.get('upc'):
                await page.click('label:has-text("У меня есть свой EAN/UPC")')
                await page.fill('input[name="upc"]', meta['upc'])

            # Сохранение
            await page.click('button:has-text("Сохранить")')
            await asyncio.sleep(3)
            
            # Обновление и возврат
            await page.goto("https://app.musicalligator.ru/releases")
            
            await browser.close()
            return {"status": "success"}

        except Exception as e:
            await page.screenshot(path=os.path.join(BASE_DIR, "alligator_error.png"))
            logger.error(f"Ошибка MusicAlligator: {e}")
            await browser.close()
            return {"status": "error", "message": str(e)}

async def scrape_ordistribution(target_artist, target_release):
    """Твоя текущая логика поиска в OR"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            await page.goto("https://ordistribution.com/login")
            await page.fill('input[type="email"]', LOGIN)
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.goto("https://ordistribution.com/admin/dashboard")
            await asyncio.sleep(5)
            
            # Поиск инпута
            search_input = page.locator('input[type="search"], .dataTables_filter input').first
            await search_input.fill(target_release)
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            
            row = page.locator("table tbody tr").first
            row_text = await row.inner_text()
            
            # Скачивание
            async with page.expect_download() as download_info:
                await row.locator('a[href*="download"]').first.click()
            download = await download_info.value
            path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(path)
            
            await browser.close()
            return {"status": "success", "file_path": path, "info": row_text}
        except Exception as e:
            await browser.close()
            return {"status": "error", "message": str(e)}

@dp.message(F.text)
async def handle_message(message: types.Message):
    if " - " not in message.text: return
    
    artist_in, release_in = message.text.split(" - ", 1)
    status = await message.answer("🚀 Начинаю процесс: OR -> MusicAlligator...")
    
    # 1. OR
    res_or = await scrape_ordistribution(artist_in.strip(), release_in.strip())
    if res_or["status"] == "error":
        await status.edit_text(f"❌ Ошибка в OR: {res_or['message']}")
        return

    # 2. Метаданные
    meta = parse_release_info(res_or["info"], artist_in.strip(), release_in.strip())
    
    # 3. Alligator
    res_ali = await upload_to_musicalligator(meta)
    
    if res_ali["status"] == "success":
        await status.edit_text(f"✅ Релиз '{release_in}' успешно отгружен в MusicAlligator!")
    else:
        await status.edit_text(f"❌ Ошибка в Alligator: {res_ali['message']}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
