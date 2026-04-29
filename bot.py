import os
import logging
import asyncio
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

async def scrape_ordistribution(target_artist: str, target_release: str):
    logger.info(f"Запуск глубокого поиска для: {target_artist} - {target_release}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True, viewport={'width': 1920, 'height': 1080})
        page = await context.new_page()

        try:
            # 1. Авторизация
            await page.goto("https://ordistribution.com/login", timeout=60000)
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            
            # 2. Переход в админку
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            await page.wait_for_selector("table", timeout=30000)

            # Находим поле поиска
            search_input = page.locator('input[placeholder*="Поиск"], input[type="search"], .dataTables_filter input').first
            await search_input.wait_for(state="visible", timeout=20000)

            async def find_row(search_query: str, verify_string: str):
                """Внутренняя функция для поиска и верификации строки в таблице"""
                logger.info(f"Пробуем искать через запрос: {search_query}")
                await search_input.click()
                await search_input.fill("")
                await search_input.type(search_query, delay=100)
                await page.keyboard.press("Enter")
                await asyncio.sleep(4) # Ждем фильтрации таблицы

                rows = page.locator("table tbody tr")
                count = await rows.count()
                
                for i in range(count):
                    row_text = await rows.nth(i).inner_text()
                    # Проверяем, есть ли в этой строке и артист, и название релиза (регистронезависимо)
                    if verify_string.lower() in row_text.lower():
                        logger.info(f"Найдено совпадение в строке: {row_text}")
                        return rows.nth(i)
                return None

            # ШАГ 1: Ищем по названию релиза, проверяем артиста
            result_row = await find_row(target_release, target_artist)

            # ШАГ 2: Если не нашли, ищем по артисту, проверяем релиз
            if not result_row:
                logger.info("По названию релиза совпадений не найдено. Пробуем поиск по артисту...")
                result_row = await find_row(target_artist, target_release)

            if not result_row:
                await browser.close()
                return {"status": "error", "message": f"Релиз '{target_release}' артиста '{target_artist}' не найден в таблице."}

            # 3. Скачивание ZIP из найденной строки
            logger.info("Кликаем по кнопке скачивания в найденной строке...")
            download_link = result_row.locator('a[href*="download"], a:has-text("ZIP"), a:has-text("Скачать")').first
            
            row_info = await result_row.inner_text()

            async with page.expect_download(timeout=60000) as download_info:
                await download_link.click()
            
            download = await download_info.value
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            await browser.close()
            return {
                "status": "success", 
                "info": row_info.replace("\t", " ").strip(),
                "file_path": file_path
            }

        except Exception as e:
            error_img = os.path.join(BASE_DIR, "error_debug.png")
            await page.screenshot(path=error_img)
            logger.error(f"Ошибка: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": f"Ошибка системы. Скриншот отправлен."}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Пришли запрос в формате: `Артист - Название релиза`.\nЯ проверю совпадение обоих полей перед скачиванием.")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text:
        await message.answer("Используй формат: `Артист - Название релиза`")
        return

    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer(f"⏳ Ищу релиз '{release}' от '{artist}'...")

    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        error_file = os.path.join(BASE_DIR, "error_debug.png")
        if "Ошибка системы" in result["message"] and os.path.exists(error_file):
            await message.answer_photo(FSInputFile(error_file), caption=f"❌ {result['message']}")
        else:
            await status_msg.edit_text(f"❌ {result['message']}")
        return

    await status_msg.edit_text(f"✅ Релиз подтвержден и скачан!\n\n`{result['info']}`")
    
    try:
        await message.answer_document(FSInputFile(result["file_path"]))
        os.remove(result["file_path"]) 
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await message.answer("Не удалось отправить файл в Telegram.")

async def main():
    logger.info("Бот запущен с логикой двойной проверки!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
