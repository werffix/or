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
        # Устанавливаем User-Agent, чтобы сайт не думал, что мы бот
        context = await browser.new_context(
            accept_downloads=True, 
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        try:
            # 1. Авторизация
            await page.goto("https://ordistribution.com/login", timeout=60000)
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            
            # 2. Переход в админку
            logger.info("Переход в админ-панель...")
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            
            # Вместо ожидания таблицы, просто ждем немного, пока страница "оживет"
            await asyncio.sleep(5)

            async def find_row(search_query: str, verify_string: str):
                logger.info(f"Попытка поиска: {search_query}")
                
                # Ищем поле поиска (учитываем твой скриншот с плейсхолдером "Поиск...")
                search_input = page.locator('input[placeholder*="Поиск"], input[type="search"], .dataTables_filter input, #search').first
                
                if await search_input.count() == 0:
                    logger.error("Поле поиска не найдено на странице!")
                    return None

                await search_input.click()
                await search_input.fill("") 
                await search_input.type(search_query, delay=100)
                await page.keyboard.press("Enter")
                
                # Ждем обновления таблицы
                await asyncio.sleep(4)

                # Ищем строки в любой таблице на странице
                rows = page.locator("table tbody tr")
                count = await rows.count()
                
                for i in range(count):
                    row_text = await rows.nth(i).inner_text()
                    if verify_string.lower() in row_text.lower():
                        logger.info(f"Найдено совпадение!")
                        return rows.nth(i)
                return None

            # Логика: Релиз -> Артист, если нет, то Артист -> Релиз
            result_row = await find_row(target_release, target_artist)

            if not result_row:
                logger.info("Не найдено по релизу, пробуем по артисту...")
                result_row = await find_row(target_artist, target_release)

            if not result_row:
                await browser.close()
                return {"status": "error", "message": f"Не удалось найти '{target_release}' от '{target_artist}'"}

            # 3. Скачивание
            row_info = await result_row.inner_text()
            logger.info(f"Скачиваем из строки: {row_info}")
            
            # Ищем кнопку/ссылку внутри найденной строки
            download_btn = result_row.locator('a[href*="download"], a:has-text("ZIP"), a:has-text("Скачать"), button:has-text("ZIP")').first
            
            async with page.expect_download(timeout=60000) as download_info:
                await download_btn.click()
            
            download = await download_info.value
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            await browser.close()
            return {"status": "success", "info": row_info.replace("\t", " ").strip(), "file_path": file_path}

        except Exception as e:
            error_img = os.path.join(BASE_DIR, "error_debug.png")
            await page.screenshot(path=error_img)
            logger.error(f"Ошибка: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": f"Ошибка: {str(e)[:50]}"}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Пришли: `Артист - Название релиза` (например: Le Bober - Prayer In C)")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text:
        return

    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer("🔍 Ищу и проверяю данные...")

    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        error_file = os.path.join(BASE_DIR, "error_debug.png")
        if os.path.exists(error_file):
            await message.answer_photo(FSInputFile(error_file), caption=f"❌ {result['message']}")
        else:
            await status_msg.edit_text(f"❌ {result['message']}")
        return

    await status_msg.edit_text(f"✅ Найдено!\n\n`{result['info']}`")
    await message.answer_document(FSInputFile(result["file_path"]))
    os.remove(result["file_path"])

async def main():
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
