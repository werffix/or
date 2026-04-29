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

# Создаем папку для загрузок, если её нет
os.makedirs(downloads_path, exist_ok=True)

async def scrape_ordistribution(artist: str, release: str):
    logger.info(f"Запуск парсера для: {artist} - {release}")
    
    async with async_playwright() as p:
        # Запускаем браузер с эмуляцией десктопного разрешения
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
            
            logger.info("Ввод учетных данных...")
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            
            # Ждем прогрузки после входа
            await page.wait_for_load_state("networkidle")
            
            # 2. Принудительный переход в админ-панель
            logger.info("Переход в админ-панель...")
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            
            # Ждем появления таблицы с релизами
            await page.wait_for_selector("table", timeout=20000)
            logger.info("Таблица админ-панели загружена.")

            # 3. Поиск (используем селекторы для DataTables)
            search_query = f"{artist} {release}"
            logger.info(f"Выполняем поиск: {search_query}")
            
            # Находим поле поиска. В DataTables это обычно input[type="search"]
            search_input = page.locator('input[type="search"], .dataTables_filter input, input[placeholder*="Search"]').first
            await search_input.wait_for(state="visible", timeout=10000)
            
            await search_input.fill("") # Очистка
            await search_input.type(search_query, delay=100) # Печатаем как человек для срабатывания скриптов
            await search_input.press('Enter')
            
            # Небольшая пауза, чтобы таблица отфильтровалась
            await asyncio.sleep(3) 

            # 4. Поиск и клик по кнопке скачивания
            logger.info("Поиск кнопки ZIP в результатах...")
            
            # Ищем первую подходящую ссылку в строке таблицы
            # Обычно в админках это 'a' с атрибутом href содержащим download или текст ZIP
            download_link = page.locator('table tbody tr a[href*="download"], table tbody tr a:has-text("ZIP"), table tbody tr a:has-text("Download")').first
            
            if await download_link.count() == 0:
                logger.warning("Кнопка скачивания не найдена через селектор, пробуем найти текст.")
                download_link = page.get_by_role("link", name="ZIP").first

            async with page.expect_download() as download_info:
                await download_link.click()
            
            download = await download_info.value
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            # Собираем данные первой строки для ответа пользователю
            row_data = await page.locator("table tbody tr").first.inner_text()
            
            await browser.close()
            return {
                "status": "success", 
                "info": row_data.replace("\t", " ").strip(),
                "file_path": file_path
            }

        except Exception as e:
            # Сохраняем скриншот ошибки для диагностики
            error_img = os.path.join(BASE_DIR, "error_debug.png")
            await page.screenshot(path=error_img)
            logger.error(f"Произошла ошибка: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": str(e)}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Бот для ordistribution готов!\n\n"
        "Отправь мне сообщение в формате:\n"
        "`Артист - Название релиза`"
    )

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text:
        await message.answer("⚠️ Неверный формат. Нужно: `Артист - Название релиза`")
        return

    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer("🔍 Захожу в админку, ищу и скачиваю...")

    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        # Если есть скриншот ошибки, можно его отправить (опционально)
        error_file = os.path.join(BASE_DIR, "error_debug.png")
        if os.path.exists(error_file):
            await message.answer_photo(FSInputFile(error_file), caption=f"❌ Ошибка: {result['message']}")
        else:
            await status_msg.edit_text(f"❌ Ошибка: {result['message']}")
        return

    await status_msg.edit_text(f"✅ Готово!\n\n**Данные из системы:**\n`{result['info']}`")
    
    try:
        doc = FSInputFile(result["file_path"])
        await message.answer_document(doc)
        os.remove(result["file_path"]) # Чистим место на диске
    except Exception as e:
        logger.error(f"Ошибка при отправке ZIP: {e}")
        await message.answer("Файл скачан на сервер, но не удалось отправить его в Telegram.")

async def main():
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
