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

# Настройка путей и логирования
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
log_path = os.path.join(BASE_DIR, "bot_debug.log")
downloads_path = os.path.join(BASE_DIR, "downloads")
accounts_path = os.path.join(BASE_DIR, "accounts.json")
unpacked_path = os.path.join(BASE_DIR, "unpacked")

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
os.makedirs(unpacked_path, exist_ok=True)


def parse_allowed_ids():
    ids = set()
    for value in ALLOWED_TELEGRAM_IDS.split(","):
        value = value.strip()
        if not value:
            continue
        if value.isdigit():
            ids.add(int(value))
    return ids


AUTHORIZED_USER_IDS = parse_allowed_ids()


def is_authorized(message: types.Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    return user_id in AUTHORIZED_USER_IDS


def read_accounts():
    if not os.path.exists(accounts_path):
        return {"accounts": [], "active_index": None}
    with open(accounts_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("accounts", [])
    data.setdefault("active_index", None)
    return data


def write_accounts(data):
    with open(accounts_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def active_account():
    data = read_accounts()
    idx = data.get("active_index")
    accounts = data.get("accounts", [])
    if idx is None or idx < 0 or idx >= len(accounts):
        return None
    return accounts[idx]


def parse_release_info(row_info: str, fallback_artist: str = "", fallback_release: str = ""):
    info = " ".join(row_info.split())
    raw_lines = [line.strip() for line in row_info.splitlines() if line.strip()]

    title = fallback_release.strip()
    artist = fallback_artist.strip()
    release_date = ""
    upc = ""
    version = ""

    if len(raw_lines) >= 2:
        first = raw_lines[0]
        second = raw_lines[1]
        if first and not any(x in first.lower() for x in ["одобрен", "релиз:", "платформа:", "upc", "ean"]):
            title = first
        if second and not any(x in second.lower() for x in ["одобрен", "релиз:", "платформа:", "upc", "ean"]):
            artist = second

    if not title or not artist:
        title_artist_match = re.match(
            r"^\s*(.*?)\s{1,}([^\s].*?)\s+(одобрен|на рассмотрении|отклонен|черновик)",
            info,
            flags=re.I
        )
        if title_artist_match:
            if not title:
                title = title_artist_match.group(1).strip()
            if not artist:
                artist = title_artist_match.group(2).strip()

    date_match = re.search(r"релиз:\s*([0-9]{2}-[0-9]{2}-[0-9]{4})", info, flags=re.I)
    if date_match:
        release_date = date_match.group(1)

    upc_match = re.search(r"(upc|ean)\s*:\s*([0-9]{8,20})", info, flags=re.I)
    if upc_match:
        upc = upc_match.group(2)

    version_match = re.search(r"\(([^)]+)\)", title)
    if version_match:
        version = version_match.group(1).strip()

    return {
        "title": title,
        "artist_raw": artist,
        "artists": [a.strip() for a in artist.split(",") if a.strip()],
        "release_date": release_date,
        "upc": upc,
        "version": version,
    }


def extract_zip_assets(zip_path: str):
    stem = Path(zip_path).stem
    target_dir = os.path.join(unpacked_path, stem)
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(target_dir)

    cover_path = None
    cover_candidates = []
    for root, _, files in os.walk(target_dir):
        for file_name in files:
            lower = file_name.lower()
            full = os.path.join(root, file_name)
            if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
                score = 0
                if "cover" in lower or "облож" in lower or "artwork" in lower:
                    score += 10
                score += max(0, 8 - len(Path(file_name).stem))
                cover_candidates.append((score, full))

    if cover_candidates:
        cover_candidates.sort(key=lambda x: x[0], reverse=True)
        cover_path = cover_candidates[0][1]

    return {"extract_dir": target_dir, "cover_path": cover_path}


async def login_musicalligator(page, email: str, password: str):
    await page.goto("https://app.musicalligator.ru/auth/signin", timeout=90000, wait_until="domcontentloaded")
    await page.wait_for_selector('input[type="email"]', timeout=90000)
    await page.fill('input[type="email"]', email)
    await page.fill('input[type="password"]', password)
    await page.click('button:has-text("Продолжить")')
    await page.wait_for_url("**/releases**", timeout=90000)
    await page.goto("https://app.musicalligator.ru/releases", timeout=90000, wait_until="domcontentloaded")
    await page.wait_for_selector('button:has-text("Новый релиз")', timeout=90000)


async def fill_input_by_label(page, label_text: str, value: str):
    if not value:
        return False
    input_locator = page.locator(f'label:has-text("{label_text}") >> xpath=following::input[1]').first
    if await input_locator.count() == 0:
        return False
    await input_locator.click()
    await page.keyboard.press("Meta+A")
    await page.keyboard.press("Backspace")
    await input_locator.fill(value)
    return True


async def fill_input_by_prompt(page, prompt_text: str, value: str, press_enter: bool = False):
    if not value:
        return False
    # Ищем поле по плейсхолдеру (из твоего HTML) или по структуре ui-input
    selectors = [
        f'input[placeholder*="{prompt_text}" i]',
        f'div.ui-input:has(span:has-text("{prompt_text}")) input',
        f'div.gs-search:has(span:has-text("{prompt_text}")) input',
    ]
    target = None
    for selector in selectors:
        locator = page.locator(selector).first
        if await locator.count() > 0:
            target = locator
            break
    if target is None:
        return False

    await target.click(timeout=3000)
    await page.keyboard.press("Meta+A")
    await page.keyboard.press("Backspace")
    await target.fill(value, timeout=3000)
    await asyncio.sleep(0.2)
    if press_enter:
        await page.keyboard.press("Enter")
    return True


async def fill_select_input_by_field_label(page, field_label: str, value: str, press_enter: bool = True):
    if not value:
        return False
    roots = [
        page.locator(f'.ui-artists-select:has(label p:has-text("{field_label}"))').first,
        page.locator(f'.ui-labels-select:has(label p:has-text("{field_label}"))').first,
    ]
    root = None
    for candidate in roots:
        if await candidate.count() > 0:
            root = candidate
            break
    if root is None:
        return False
    input_locator = root.locator('.gs-search input[type="text"], .ui-input input[type="text"]').first
    if await input_locator.count() == 0:
        return False
    await input_locator.click(timeout=3000)
    await page.keyboard.press("Meta+A")
    await page.keyboard.press("Backspace")
    await input_locator.fill(value, timeout=3000)
    await asyncio.sleep(0.2)
    if press_enter:
        await page.keyboard.press("Enter")
    return True


async def enable_toggle_by_text(page, toggle_text: str):
    root = page.locator(f'.row-toggle:has(p:has-text("{toggle_text}")), div:has(> p:has-text("{toggle_text}"))').first
    if await root.count() == 0:
        return False
    checkbox = root.locator('input[type="checkbox"]').first
    if await checkbox.count() == 0:
        return False
    try:
        if await checkbox.is_checked(): return True
    except: pass
    slider = root.locator('.slider, .ui-switch, label.ui-switch, span.round').first
    if await slider.count() > 0:
        try:
            await slider.click(timeout=3000)
            await asyncio.sleep(0.2)
            if await checkbox.is_checked(): return True
        except: pass
    await checkbox.evaluate("(el) => { if (!el.checked) { el.checked = true; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); } }")
    return await checkbox.is_checked()


async def set_artist_with_create_fallback(page, label_text: str, artist_name: str):
    if not artist_name: return
    prompt_map = {"Исполнитель": "Введите основного исполнителя", "Дополнительный исполнитель": "Введите доп. исполнителя"}
    prompt = prompt_map.get(label_text, "")
    ok = await fill_select_input_by_field_label(page, label_text, artist_name, press_enter=True)
    if not ok and prompt:
        ok = await fill_input_by_prompt(page, prompt, artist_name, press_enter=True)
    if not ok:
        await fill_input_by_label(page, label_text, artist_name)
    await asyncio.sleep(1)
    create_btn = page.locator('button:has-text("Создать исполнителя")').first
    if await create_btn.count() > 0 and await create_btn.is_visible():
        await create_btn.click()
        await asyncio.sleep(1)
        for _ in range(2):
            no_btn = page.locator('button:has-text("Нет")').first
            if await no_btn.count() > 0: await no_btn.click()
        await page.locator('button:has-text("Создать исполнителя")').last.click()


async def upload_to_musicalligator(release_meta: dict, zip_path: str):
    account = active_account()
    if not account: return {"status": "error", "message": "Нет аккаунта"}
    assets = extract_zip_assets(zip_path)
    cover_path = assets["cover_path"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        try:
            await login_musicalligator(page, account["email"], account["password"])
            await page.locator('button:has-text("Новый релиз")').first.click()
            await page.wait_for_selector('input[type="file"][accept*="image"]', timeout=90000)

            if cover_path:
                await page.locator('input[type="file"][accept*="image"]').first.set_input_files(cover_path)

            artists = release_meta.get("artists", [])
            if artists:
                await set_artist_with_create_fallback(page, "Исполнитель", artists[0])

            # ЗАПОЛНЕНИЕ ПО ТВОЕМУ HTML (placeholder="Название" и "Версия")
            await fill_input_by_prompt(page, "Название", release_meta.get("title", ""))
            await fill_input_by_prompt(page, "Версия", release_meta.get("version", ""))

            await fill_select_input_by_field_label(page, "Лейбл", artists[0] if artists else "CDCULT", press_enter=True)
            
            if release_meta.get("release_date"):
                await enable_toggle_by_text(page, "Оригинальная дата релиза")
                await fill_input_by_label(page, "Оригинальная дата релиза", release_meta["release_date"].replace("-", "."))

            if release_meta.get("upc"):
                await enable_toggle_by_text(page, "У меня есть свой EAN/UPC")
                await fill_input_by_prompt(page, "EAN/UPC", release_meta["upc"])

            # Ждем автосохранения или просто уходим в список
            await page.goto("https://app.musicalligator.ru/releases", timeout=90000)
            await browser.close()
            return {"status": "success", "message": f"Релиз отправлен в {account['note']}"}
        except Exception as e:
            await browser.close()
            logger.error(f"Ошибка Аллигатора: {e}")
            return {"status": "error", "message": f"Ошибка: {str(e)[:100]}"}

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
            await page.goto("https://ordistribution.com/login", timeout=60000)
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            await asyncio.sleep(8) 

            async def set_search_value(search_input, value: str):
                await search_input.click()
                await search_input.press("Meta+A")
                await search_input.press("Backspace")
                await search_input.fill("")
                await search_input.evaluate(
                    """(el, newValue) => {
                        const prototype = Object.getPrototypeOf(el);
                        const valueSetter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                        if (valueSetter) { valueSetter.call(el, newValue); } else { el.value = newValue; }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )

            async def find_row(search_query: str, verify_string: str):
                search_input = page.locator('input[type="search"], input[placeholder*="поиск" i], input[placeholder*="search" i]').first
                if await search_input.count() == 0: return None
                await set_search_value(search_input, search_query)
                await asyncio.sleep(2)
                await search_input.press("Enter")
                await asyncio.sleep(3)

                cards = await page.locator("article:has-text('" + search_query + "'), section:has-text('" + search_query + "'), div:has-text('" + search_query + "')").all()
                for card in cards:
                    if not await card.is_visible(): continue
                    card_text = await card.inner_text()
                    clean_card = " ".join(card_text.split()).lower()
                    if verify_string.lower() in clean_card and 20 < len(clean_card) < 2000 and "панель администратора" not in clean_card:
                        return card
                return None

            async def open_release_details(result_row):
                btn = result_row.locator('button:has-text("Подробнее"), a:has-text("Подробнее")').first
                if await btn.count() > 0:
                    await btn.click()
                    await asyncio.sleep(2)
                    return True
                return False

            async def download_zip_from_details():
                zip_btn = page.locator('button:has-text("Скачать ZIP"), a:has-text("Скачать ZIP"), a[href*="zip"]').first
                async with page.expect_download(timeout=60000) as download_info:
                    await zip_btn.click()
                return await download_info.value

            result_row = await find_row(target_release, target_artist) or await find_row(target_artist, target_release)
            if not result_row:
                await browser.close()
                return {"status": "error", "message": f"Не найден: {target_artist} - {target_release}"}

            row_info = await result_row.inner_text()
            if not await open_release_details(result_row):
                raise RuntimeError("Не удалось открыть карточку")

            download = await download_zip_from_details()
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            await browser.close()
            return {"status": "success", "info": row_info.replace("\n", " ").strip(), "file_path": file_path}

        except Exception as e:
            await browser.close()
            logger.error(f"Ошибка OR: {e}")
            return {"status": "error", "message": f"Ошибка OR: {str(e)[:50]}"}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_authorized(message): return
    await message.answer("Пришли: `Артист - Название релиза`")

@dp.message(Command("accounts"))
async def cmd_accounts(message: types.Message):
    if not is_authorized(message): return
    args = (message.text or "").replace("/accounts", "", 1).strip()
    data = read_accounts()
    if not args:
        lines = [f"{'✅' if data['active_index'] == i else '•'} {i+1}. {a['note']}" for i, a in enumerate(data['accounts'])]
        await message.answer("\n".join(lines) or "Нет аккаунтов")
        return
    if args.startswith("add "):
        p = [x.strip() for x in args[4:].split("|")]
        if len(p) == 3:
            data['accounts'].append({"note": p[0], "email": p[1], "password": p[2]})
            data['active_index'] = 0 if data['active_index'] is None else data['active_index']
            write_accounts(data); await message.answer("Добавлен.")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if not is_authorized(message) or " - " not in message.text: return
    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer(f"🔍 Ищу...")

    result = await scrape_ordistribution(artist.strip(), release.strip())
    if result["status"] == "error":
        await status_msg.edit_text(f"❌ {result['message']}")
        return

    # Извлекаем инфо из OR
    release_meta = parse_release_info(
        result["info"],
        fallback_artist=artist.strip(),
        fallback_release=release.strip()
    )

    # ВНИМАНИЕ: Строка release_meta["title"] = release.strip() УДАЛЕНА.
    # Теперь название берется только из parse_release_info (из данных OR).

    upload_result = await upload_to_musicalligator(release_meta, result["file_path"])
    await status_msg.edit_text(f"{'✅' if upload_result['status'] == 'success' else '⚠️'} {upload_result['message']}\n\nИнфо: `{release_meta['title']}`")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
