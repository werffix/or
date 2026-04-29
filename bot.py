import os
import logging
import asyncio
import json
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
        context = await browser.new_context(
            accept_downloads=True, 
            viewport={'width': 1920, 'height': 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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
            await asyncio.sleep(8) # Даем время скриптам DataTables инициализироваться

            async def set_search_value(search_input, value: str):
                await search_input.click()
                await search_input.press("Meta+A")
                await search_input.press("Backspace")
                await search_input.fill("")

                # Для React-controlled input простого fill не всегда достаточно:
                # вызываем native setter и вручную диспатчим события ввода.
                await search_input.evaluate(
                    """(el, newValue) => {
                        const prototype = Object.getPrototypeOf(el);
                        const valueSetter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;

                        if (valueSetter) {
                            valueSetter.call(el, newValue);
                        } else {
                            el.value = newValue;
                        }

                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )

            async def find_row(search_query: str, verify_string: str):
                logger.info(f"--- Поиск: {search_query} ---")

                search_selectors = [
                    'input[type="search"]',
                    'input[placeholder*="поиск" i]',
                    'input[placeholder*="search" i]',
                    'input[aria-label*="поиск" i]',
                    'input[aria-label*="search" i]',
                    'input[class*="search" i]',
                    'input',
                ]

                search_input = None
                for selector in search_selectors:
                    candidate = page.locator(selector).first
                    if await candidate.count() > 0 and await candidate.is_visible():
                        search_input = candidate
                        logger.info(f"Используем инпут поиска: {selector}")
                        break

                if search_input is None:
                    logger.error("Инпут поиска не найден!")
                    return None

                await set_search_value(search_input, search_query)
                await asyncio.sleep(2)

                # На случай логики, которая запускает фильтр только после подтверждения.
                await search_input.press("Enter")
                await asyncio.sleep(3)

                body_text = await page.locator("body").inner_text()
                logger.info(
                    f"Запрос '{search_query}' в тексте страницы: "
                    f"{search_query.strip().lower() in body_text.lower()}"
                )

                clean_verify = verify_string.strip().lower()
                search_query_json = json.dumps(search_query)

                # В OR результаты выводятся карточками со множеством вложенных div,
                # поэтому ищем контейнеры, содержащие текст запроса.
                card_candidates = await page.locator(
                    f'article:has-text({search_query_json}), '
                    f'section:has-text({search_query_json}), '
                    f'li:has-text({search_query_json}), '
                    f'div:has-text({search_query_json})'
                ).all()
                logger.info(f"Контейнеров с текстом запроса: {len(card_candidates)}")

                for card in card_candidates:
                    if not await card.is_visible():
                        continue

                    card_text = await card.inner_text()
                    clean_card = " ".join(card_text.split()).lower()

                    if (
                        clean_verify in clean_card
                        and 20 < len(clean_card) < 2000
                        and "панель администратора" not in clean_card
                        and "всего:" not in clean_card
                        and "на рассмотрении:" not in clean_card
                    ):
                        logger.info(f"Найдена карточка: {clean_card[:250]}")
                        return card

                # Запасной проход по видимым контейнерам на странице.
                cards = await page.locator("article, section, li, div").all()
                valid_cards = []

                for card in cards:
                    if not await card.is_visible():
                        continue

                    card_text = await card.inner_text()
                    clean_card = " ".join(card_text.split()).lower()

                    if (
                        search_query.strip().lower() in clean_card
                        and clean_verify in clean_card
                        and 20 < len(clean_card) < 2000
                        and "панель администратора" not in clean_card
                        and "всего:" not in clean_card
                    ):
                        valid_cards.append((card, clean_card))

                logger.info(f"Карточек для проверки: {len(valid_cards)}")

                for card, clean_card in valid_cards:
                    logger.info(f"Проверка карточки: {clean_card[:250]}")
                    return card
                
                return None

            async def open_release_details(result_row):
                detail_candidates = [
                    result_row.get_by_role("button", name="Подробнее"),
                    result_row.get_by_text("Подробнее", exact=False),
                    result_row.locator('button:has-text("Подробнее"), a:has-text("Подробнее")'),
                ]

                for candidate in detail_candidates:
                    if await candidate.count() > 0:
                        button = candidate.first
                        if await button.is_visible():
                            logger.info("Открываем карточку релиза через 'Подробнее'")
                            await button.click()
                            await asyncio.sleep(2)
                            return True

                # Запасной вариант: если кнопка лежит в общей строке/карточке рядом.
                row_text = await result_row.inner_text()
                page_button = page.locator(
                    f'tr:has-text("{row_text[:50]}") button:has-text("Подробнее"), '
                    f'tr:has-text("{row_text[:50]}") a:has-text("Подробнее")'
                ).first
                if await page_button.count() > 0 and await page_button.is_visible():
                    logger.info("Открываем карточку релиза через запасной селектор")
                    await page_button.click()
                    await asyncio.sleep(2)
                    return True

                # В некоторых карточках детали открываются кликом по самой карточке.
                if await result_row.is_visible():
                    logger.info("Пробуем открыть карточку кликом по найденному блоку")
                    await result_row.click()
                    await asyncio.sleep(2)
                    return True

                return False

            async def download_zip_from_details():
                zip_candidates = [
                    page.get_by_role("button", name="Скачать ZIP"),
                    page.get_by_role("link", name="Скачать ZIP"),
                    page.get_by_text("Скачать ZIP", exact=False),
                    page.locator('button:has-text("Скачать ZIP"), a:has-text("Скачать ZIP")'),
                    page.locator('button:has-text("ZIP"), a:has-text("ZIP"), a[href*="zip"], a[href*="download"]'),
                ]

                for candidate in zip_candidates:
                    if await candidate.count() == 0:
                        continue

                    button = candidate.first
                    if not await button.is_visible():
                        continue

                    logger.info("Найдена кнопка скачивания ZIP")
                    async with page.expect_download(timeout=60000) as download_info:
                        await button.click()
                    return await download_info.value

                raise RuntimeError("Кнопка 'Скачать ZIP' не найдена после открытия карточки")

            # Пробуем найти
            result_row = await find_row(target_release, target_artist)
            if not result_row:
                logger.info("По релизу пусто, пробуем по артисту...")
                result_row = await find_row(target_artist, target_release)

            if not result_row:
                await browser.close()
                return {"status": "error", "message": f"Не найден: {target_artist} - {target_release}"}

            row_info = await result_row.inner_text()

            details_opened = await open_release_details(result_row)
            if not details_opened:
                raise RuntimeError("Кнопка 'Подробнее' не найдена у найденного релиза")

            download = await download_zip_from_details()
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            await browser.close()
            return {
                "status": "success", 
                "info": row_info.replace("\n", " ").strip(), 
                "file_path": file_path
            }

        except Exception as e:
            error_img = os.path.join(BASE_DIR, "error_debug.png")
            await page.screenshot(path=error_img)
            logger.error(f"Ошибка: {e}", exc_info=True)
            await browser.close()
            return {"status": "error", "message": f"Ошибка: {str(e)[:50]}"}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Пришли: `Артист - Название релиза`")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text: return
    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer(f"🔍 Ищу...")

    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        error_file = os.path.join(BASE_DIR, "error_debug.png")
        if os.path.exists(error_file):
            await message.answer_photo(FSInputFile(error_file), caption=f"❌ {result['message']}")
        else:
            await status_msg.edit_text(f"❌ {result['message']}")
        return

    await status_msg.edit_text(f"✅ Готово!\n\n`{result['info']}`")
    await message.answer_document(FSInputFile(result["file_path"]))
    os.remove(result["file_path"])

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
