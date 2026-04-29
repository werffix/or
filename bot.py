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

# Настройка подробного логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_debug.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Убедимся, что папка для загрузок существует
os.makedirs("downloads", exist_ok=True)

async def scrape_ordistribution(artist: str, release: str):
    """
    Функция для логина на сайт, поиска и скачивания релиза.
    ВНИМАНИЕ: CSS-селекторы (вида 'input[name="email"]') нужно будет 
    заменить на реальные, изучив код страницы сайта.
    """
    logger.info(f"Запуск парсера для: {artist} - {release}")
    
    async with async_playwright() as p:
        # Запускаем браузер в headless режиме (без графического интерфейса)
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            # 1. Логин
            logger.info("Переход на страницу логина...")
            await page.goto("https://ordistribution.com/login", timeout=30000)
            
            logger.info("Ввод учетных данных...")
            # ЗАМЕНИТЬ СЕЛЕКТОРЫ НА АКТУАЛЬНЫЕ
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            
            # Ждем, пока загрузится дашборд после логина
            await page.wait_for_load_state("networkidle")
            logger.info("Авторизация успешна.")

            # 2. Поиск
            search_query = f"{artist} {release}"
            logger.info(f"Выполняем поиск: {search_query}")
            
            # ЗАМЕНИТЬ СЕЛЕКТОРЫ НА АКТУАЛЬНЫЕ
            await page.fill('input[name="search"]', search_query)
            await page.press('input[name="search"]', 'Enter')
            await page.wait_for_load_state("networkidle")

            # Кликаем на первый результат (пример)
            await page.click('.search-result-item:first-child')
            await page.wait_for_load_state("networkidle")

            # 3. Сбор данных
            logger.info("Сбор текстовых данных о релизе...")
            # Пример парсинга: заменить селектор
            release_info = await page.inner_text('.release-details-container') 

            # 4. Скачивание ZIP
            logger.info("Ожидание начала загрузки файла...")
            # ЗАМЕНИТЬ СЕЛЕКТОР КНОПКИ СКАЧИВАНИЯ
            async with page.expect_download() as download_info:
                await page.click('text="Download ZIP"') # Ищет кнопку с текстом Download ZIP
            
            download = await download_info.value
            file_path = os.path.join("downloads", download.suggested_filename)
            await download.save_as(file_path)
            logger.info(f"Файл успешно сохранен: {file_path}")

            await browser.close()
            return {"status": "success", "info": release_info, "file_path": file_path}

        except PlaywrightTimeoutError as e:
            logger.error(f"Таймаут Playwright: элемент не найден или страница не загрузилась. Детали: {e}")
            await browser.close()
            return {"status": "error", "message": "Ошибка таймаута на сайте (элемент не найден). Проверь логи."}
        except Exception as e:
            logger.error(f"Непредвиденная ошибка в парсере: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": str(e)}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    logger.info(f"Пользователь {message.from_user.id} нажал /start")
    await message.answer(
        "Привет! Отправь мне запрос в формате:\n"
        "**Артист - Название релиза**\n\n"
        "Я найду его на ordistribution, соберу данные и скачаю ZIP."
    )

@dp.message(F.text)
async def handle_request(message: types.Message):
    text = message.text.strip()
    
    # Проверка формата
    if " - " not in text:
        logger.warning(f"Неверный формат сообщения от {message.from_user.id}: {text}")
        await message.answer("Пожалуйста, используй формат: Артист - Название релиза")
        return

    artist, release = text.split(" - ", 1)
    artist = artist.strip()
    release = release.strip()

    logger.info(f"Получен запрос. Артист: '{artist}', Релиз: '{release}'")
    status_msg = await message.answer("⏳ Подключаюсь к серверу и ищу релиз... Это может занять около минуты.")

    # Запускаем парсер
    result = await scrape_ordistribution(artist, release)

    if result["status"] == "error":
        await status_msg.edit_text(f"❌ Произошла ошибка при поиске/скачивании:\n`{result['message']}`")
        return

    # Если все успешно
    info_text = result["info"]
    file_path = result["file_path"]

    await status_msg.edit_text(f"✅ Релиз найден!\n\n**Данные:**\n{info_text[:900]}...") # Обрезаем текст, чтобы влез в лимит TG
    
    # Отправляем архив
    try:
        logger.info(f"Отправка файла в Telegram: {file_path}")
        document = FSInputFile(file_path)
        await message.answer_document(document)
        
        # Удаляем файл с сервера после отправки, чтобы не забивать место (опционально)
        os.remove(file_path)
        logger.info(f"Файл {file_path} удален с сервера.")
    except Exception as e:
        logger.error(f"Ошибка при отправке файла в Telegram: {e}", exc_info=True)
        await message.answer("Данные собраны, но произошла ошибка при отправке ZIP-файла.")

async def main():
    logger.info("Бот запущен!")
    # Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
