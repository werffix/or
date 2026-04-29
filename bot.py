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

async def scrape_ordistribution(artist: str, release: str):
    logger.info(f"Запуск парсера для: {artist} - {release}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()

        try:
            # 1. Авторизация
            logger.info("Переход на страницу логина...")
            await page.goto("https://ordistribution.com/login", timeout=60000)
            
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            
            # 2. Переход в админку
            logger.info("Переход в админ-панель...")
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            
            # Ждем появления таблицы
            await page.wait_for_selector("table", timeout=30000)
            logger.info("Таблица загружена.")

            # 3. Поиск (Обновленные селекторы под "Поиск...")
            search_query = f"{artist} {release}"
            logger.info(f"Выполняем поиск: {search_query}")
            
            # Ищем по атрибутам, включая русский плейсхолдер
            search_input = page.locator('input[placeholder*="Поиск"], input[type="search"], .dataTables_filter input').first
            
            await search_input.wait_for(state="visible", timeout=20000)
            await search_input.click()
            await search_input.fill("") 
            await search_input.type(search_query, delay=150)
            await search_input.press('Enter')
            
            # Даем время таблице отфильтровать результаты
            await asyncio.sleep(5) 

            # 4. Скачивание
            logger.info("Ищем кнопку ZIP...")
            
            # В админке это может быть ссылка с иконкой или текстом
            download_link = page.locator('table tbody tr a[href*="download"], table tbody tr a:has-text("ZIP"), table tbody tr a:has-text("Скачать")').first
            
            if await download_link.count() == 0:
                # Резервный поиск по всем ссылкам на странице
                download_link = page.get_by_role("link", name="ZIP").first

            async with page.expect_download(timeout=60000) as download_info:
                await download_link.click()
            
            download = await download_info.value
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            row_data = await page.locator("table tbody tr").first.inner_text()
            
            await browser.close()
            return {
                "status": "success", 
                "info": row_data.replace("\t", " ").strip(),
                "file_path": file_path
            }

        except Exception as e:
            error_img = os.path.join(BASE_DIR, "error_debug.png")
            await page.screenshot(path=error_img)
            logger.error(f"Ошибка: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": f"{str(e)[:100]}... Скриншот прикреплен."}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Пришли: `Артист - Название релиза`")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text:
        await message.answer("Формат: `Артист - Название релиза`")
        return

    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer("⏳ Работаю...")

    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        error_file = os.path.join(BASE_DIR, "error_debug.png")
        if os.path.exists(error_file):
            await message.answer_photo(FSInputFile(error_file), caption=f"❌ {result['message']}")
        else:
            await status_msg.edit_text(f"❌ {result['message']}")
        return

    await status_msg.edit_text(f"✅ Готово!\n\n`{result['info']}`")
    
    try:
        await message.answer_document(FSInputFile(result["file_path"]))
        os.remove(result["file_path"]) 
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        await message.answer("Ошибка при отправке файла.")

async def main():
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
