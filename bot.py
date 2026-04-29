import os
import logging
import asyncio
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

# Настройка путей
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
downloads_path = os.path.join(BASE_DIR, "downloads")
os.makedirs(downloads_path, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def parse_release_info(text, fallback_artist="", fallback_release=""):
    """Парсинг данных из строки OR для Аллигатора"""
    info = {
        "artists": fallback_artist,
        "title": fallback_release,
        "subtitle": "",
        "upc": "",
        "release_date": ""
    }
    # Ищем UPC (12-13 цифр)
    upc_match = re.search(r'\b\d{12,13}\b', text)
    if upc_match: info["upc"] = upc_match.group(0)
        
    # Ищем дату (ДД.ММ.ГГГГ)
    date_match = re.search(r'\b\d{2}\.\d{2}\.\d{4}\b', text)
    if date_match: info["release_date"] = date_match.group(0)

    # Если в строке есть доп. инфа в скобках — это подзаголовок (версия)
    subtitle_match = re.search(r'\((.*?)\)', text)
    if subtitle_match: info["subtitle"] = subtitle_match.group(1)
    
    return info

async def upload_to_musicalligator(meta):
    """ПОЧИНЕННЫЙ КОД ДЛЯ АЛЛИГАТОРА"""
    logger.info(f"Заполнение MusicAlligator для релиза: {meta['title']}")
    async with async_playwright() as p:
        # headless=False если хочешь видеть глазами, как он кликает
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width': 1920, 'height': 1080})
        page = await context.new_page()
        
        try:
            # Заходим (предполагается, что ты залогинен или добавишь куки/логин)
            await page.goto("https://app.musicalligator.ru/releases")
            # Жмем кнопку создания или переходим в черновик (тут линк для примера из твоего файла)
            await page.goto("https://app.musicalligator.ru/releases/334360/edit/release")
            await asyncio.sleep(5)

            # 1. Название релиза
            await page.fill('input[name="title"]', meta['title'])

            # 2. Исполнители (Разделение по запятой)
            artists = [a.strip() for a in meta['artists'].split(',')]
            
            async def fill_artist_logic(name, idx):
                # Находим инпут исполнителя по счету
                artist_inputs = page.locator('input[placeholder="Выберите исполнителя"]')
                target_input = artist_inputs.nth(idx)
                
                await target_input.click()
                await target_input.fill(name)
                await asyncio.sleep(2)
                
                create_artist_btn = page.locator('button:has-text("Создать исполнителя")')
                if await create_artist_btn.is_visible():
                    await create_artist_btn.click()
                    await asyncio.sleep(1)
                    # В модальном окне ставим "Нет" для Apple и Spotify
                    no_options = page.locator('label:has-text("Нет")')
                    for i in range(await no_options.count()):
                        await no_options.nth(i).click()
                    await page.click('button:has-text("Создать исполнителя")')
                    await asyncio.sleep(1)
                else:
                    await page.keyboard.press("Enter")

            # Первый (основной)
            await fill_artist_logic(artists[0], 0)

            # Дополнительные (через кнопку плюс)
            for i in range(1, len(artists)):
                await page.click('button.-more') # Кнопка + справа
                await asyncio.sleep(0.5)
                await fill_artist_logic(artists[i], i)

            # 3. Версия релиза (Подзаголовок)
            if meta.get('subtitle'):
                await page.fill('input[name="version"]', meta['subtitle'])

            # 4. Лейбл (Первый артист)
            await page.fill('input[placeholder="Выберите лейбл"]', artists[0])
            await asyncio.sleep(2)
            create_label_btn = page.locator('button:has-text("создать лейбл")')
            if await create_label_btn.is_visible():
                await create_label_btn.click()
                await page.click('button:has-text("Проверить и создать лейбл")')
            else:
                await page.keyboard.press("Enter")

            # 5. Оригинальная дата релиза (Тумблер)
            if meta.get('release_date'):
                await page.click('label:has-text("Оригинальная дата релиза")')
                await page.fill('input[placeholder="ДД.ММ.ГГГГ"]', meta['release_date'])

            # 6. UPC (Тумблер)
            if meta.get('upc'):
                await page.click('label:has-text("У меня есть свой EAN/UPC")')
                await page.fill('input[name="upc"]', meta['upc'])

            # Сохраняем
            await page.click('button:has-text("Сохранить")')
            await asyncio.sleep(2)
            
            # Обновляем и выходим в список
            await page.goto("https://app.musicalligator.ru/releases")
            
            await browser.close()
            return {"status": "success"}
        except Exception as e:
            await page.screenshot(path="alligator_debug.png")
            await browser.close()
            return {"status": "error", "message": str(e)}

async def scrape_ordistribution(target_artist, target_release):
    """ТВОЙ РАБОЧИЙ КОД OR (НЕ ТРОГАЕМ)"""
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
            
            # Поиск
            search_input = page.locator('input[type="search"], .dataTables_filter input').first
            await search_input.click()
            await search_input.fill(target_release)
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)
            
            row = page.locator("table tbody tr").first
            info = await row.inner_text()
            
            async with page.expect_download() as download_info:
                await row.locator('a[href*="download"]').first.click()
            download = await download_info.value
            path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(path)
            
            await browser.close()
            return {"status": "success", "info": info, "file_path": path}
        except Exception as e:
            await browser.close()
            return {"status": "error", "message": str(e)}

@dp.message(F.text)
async def handle_request(message: types.Message):
    if " - " not in message.text: return
    artist, release = message.text.split(" - ", 1)
    
    msg = await message.answer("🛠 Работаю...")
    
    # Сначала OR
    res_or = await scrape_ordistribution(artist.strip(), release.strip())
    if res_or["status"] == "error":
        await msg.edit_text(f"❌ Ошибка OR: {res_or['message']}")
        return
    
    # Парсим что достали
    meta = parse_release_info(res_or["info"], artist.strip(), release.strip())
    
    # Теперь Аллигатор
    res_ali = await upload_to_musicalligator(meta)
    
    if res_ali["status"] == "success":
        await msg.edit_text(f"✅ Релиз '{release}' успешно отгружен!")
    else:
        await msg.edit_text(f"⚠️ OR Ок, но в Аллигаторе ошибка: {res_ali['message']}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
