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
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            # 1. Авторизация
            logger.info("Переход на страницу логина...")
            await page.goto("https://ordistribution.com/login", timeout=45000)
            
            logger.info("Ввод учетных данных...")
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            
            await page.wait_for_load_state("networkidle")
            
            # 2. Переход в админку
            logger.info("Переход в админ-панель...")
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=45000)
            await page.wait_for_load_state("networkidle")
            
            # 3. Поиск
            search_query = f"{artist} {release}"
            logger.info(f"Выполняем поиск: {search_query}")
            
            # Ищем поле поиска (универсальный селектор для админки)
            search_input = page.locator('input[type="search"], input[placeholder*="Search" i], input[type="text"]').first
            await search_input.wait_for(state="visible", timeout=15000)
            await search_input.fill(search_query)
            await search_input.press('Enter')
            
            await page.wait_for_load_state("networkidle")
            
            # 4. Клик по релизу
            # Ищем ссылку, текст которой содержит название релиза
            try:
                release_element = page.get_by_text(release, exact=False).first
                await release_element.click()
                await page.wait_for_load_state("networkidle")
                logger.info("Страница релиза открыта.")
            except Exception as e:
                logger.warning(f"Не удалось кликнуть по релизу: {e}")

            # 5. Сбор данных и скачивание ZIP
            logger.info("Поиск кнопки скачивания ZIP...")
            
            async with page.expect_download() as download_info:
                # Пробуем найти кнопку ZIP или Download
                zip_button = page.locator('a:has-text("ZIP"), button:has-text("ZIP"), a:has-text("Download")').first
                await zip_button.click()
            
            download = await download_info.value
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            # Собираем текстовую информацию со страницы (основной контент)
            release_info = await page.locator('body').inner_text()
            
            await browser.close()
            return {
                "status": "success", 
                "info": release_info[:500], # Берем первые 500 символов для превью
                "file_path": file_path
            }

        except PlaywrightTimeoutError as e:
            logger.error(f"Ошибка таймаута: {e}")
            await browser.close()
            return {"status": "error", "message": "Сайт долго отвечал или элемент не найден."}
        except Exception as e:
            logger.error(f"Ошибка парсера: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": str(e)}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Бот готов к работе!\n\n"
        "Введи запрос в формате:\n"
        "`Артист - Название релиза`"
    )

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text:
        await message.answer("⚠️ Неверный формат. Используй: `Артист - Название релиза`")
        return

    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer("🔍 Захожу в админку и ищу релиз...")

    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        await status_msg.edit_text(f"❌ Ошибка:\n{result['message']}")
        return

    await status_msg.edit_text(f"✅ Данные собраны!\n\n{result['info']}...")
    
    try:
        doc = FSInputFile(result["file_path"])
        await message.answer_document(doc)
        os.remove(result["file_path"]) # Удаляем файл после отправки
    except Exception as e:
        logger.error(f"Ошибка отправки файла: {e}")
        await message.answer("Не удалось отправить ZIP-файл.")

async def main():
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
