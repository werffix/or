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
    input_locator = page.locator(
        f'label:has-text("{label_text}") >> xpath=following::input[1]'
    ).first
    if await input_locator.count() == 0:
        return False
    await input_locator.click()
    await input_locator.press("Control+A")
    await input_locator.press("Backspace")
    await input_locator.fill(value)
    return True


async def fill_input_by_prompt(page, prompt_text: str, value: str, press_enter: bool = False):
    if not value:
        return False
    selectors = [
        f'div.ui-input:has(span:has-text("{prompt_text}")) input',
        f'div.gs-search:has(span:has-text("{prompt_text}")) input',
        f'input[placeholder*="{prompt_text}" i]',
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
    await page.keyboard.press("Control+A")
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
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await input_locator.fill(value, timeout=3000)
    await asyncio.sleep(0.2)
    if press_enter:
        await page.keyboard.press("Enter")
    return True


async def enable_toggle_by_text(page, toggle_text: str):
    root = page.locator(
        f'.row-toggle:has(p:has-text("{toggle_text}")), '
        f'div:has(> p:has-text("{toggle_text}"))'
    ).first
    if await root.count() == 0:
        return False
    checkbox = root.locator('input[type="checkbox"]').first
    if await checkbox.count() == 0:
        return False
    try:
        if await checkbox.is_checked():
            return True
    except Exception:
        pass
    slider = root.locator('.slider, .ui-switch, label.ui-switch, span.round').first
    if await slider.count() > 0:
        try:
            await slider.click(timeout=3000)
            await asyncio.sleep(0.2)
            if await checkbox.is_checked():
                return True
        except Exception:
            pass
    text_node = root.locator(f'p:has-text("{toggle_text}")').first
    if await text_node.count() > 0:
        try:
            await text_node.click(timeout=3000)
            await asyncio.sleep(0.2)
            if await checkbox.is_checked():
                return True
        except Exception:
            pass
    await checkbox.evaluate(
        """(el) => {
            if (!el.checked) {
                el.checked = true;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }"""
    )
    await asyncio.sleep(0.2)
    return await checkbox.is_checked()


async def set_artist_with_create_fallback(page, label_text: str, artist_name: str):
    if not artist_name:
        return
    prompt_map = {
        "Исполнитель": "Введите основного исполнителя",
        "Дополнительный исполнитель": "Введите доп. исполнителя",
        "При участии (feat.)": "Введите feat. исполнителя",
        "Лейбл": "Введите лейбл"
    }
    prompt = prompt_map.get(label_text, "")
    ok = await fill_select_input_by_field_label(page, label_text, artist_name, press_enter=False)
    if not ok and prompt:
        ok = await fill_input_by_prompt(page, prompt, artist_name, press_enter=False)
    if not ok:
        ok = await fill_input_by_label(page, label_text, artist_name)
    if not ok:
        return
    await asyncio.sleep(1)
    first_option = page.locator('.multiselect-option, .gs-search-list-item, .dropdown-item').first
    if await first_option.count() > 0:
        await first_option.click()
    await asyncio.sleep(1)
    create_btn = page.locator('button:has-text("Создать исполнителя"), div:has-text("Создать исполнителя")').first
    if await create_btn.count() > 0 and await create_btn.is_visible():
        await create_btn.click()
        await asyncio.sleep(1)
        apple_toggle = page.locator('text="Есть ли у этого исполнителя профиль в Apple Music?"').locator("..").locator('button:has-text("Нет")').first
        spotify_toggle = page.locator('text="Есть ли у этого исполнителя профиль в Spotify?"').locator("..").locator('button:has-text("Нет")').first
        if await apple_toggle.count() > 0: await apple_toggle.click()
        if await spotify_toggle.count() > 0: await spotify_toggle.click()
        await asyncio.sleep(0.5)
        create_final = page.locator('button:has-text("Создать исполнителя")').last
        if await create_final.count() > 0 and await create_final.is_visible():
            await create_final.click()


async def upload_to_musicalligator(release_meta: dict, zip_path: str):
    account = active_account()
    if not account:
        return {"status": "error", "message": "Нет выбранного аккаунта MusicAlligator. Используй /accounts"}

    assets = extract_zip_assets(zip_path)
    cover_path = assets["cover_path"]

    async with async_playwright() as p:
        # ВАРИАНТ 2: Headless режим с маскировкой под реального пользователя
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        # Настройка user_agent и параметров, чтобы избежать детектирования ботов
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(90000)
        current_step = "init"

        try:
            current_step = "login"
            await login_musicalligator(page, account["email"], account["password"])

            current_step = "click_new_release"
            await page.locator('button:has-text("Новый релиз")').first.click()
            await page.wait_for_selector('input[type="file"][accept*="image"]', timeout=90000)
            await asyncio.sleep(2)

            if cover_path:
                current_step = "upload_cover"
                file_input = page.locator('input[type="file"][accept*="image"]').first
                await file_input.set_input_files(cover_path)
                await asyncio.sleep(1)

            current_step = "fill_artist"
            artists = release_meta.get("artists", [])
            main_artist = artists[0] if artists else ""
            extra_artists = artists[1:] if len(artists) > 1 else []
            await set_artist_with_create_fallback(page, "Исполнитель", main_artist)

            for index, artist in enumerate(extra_artists):
                plus_btn = page.locator('button:has(svg), .add-button, .plus').filter(has_text="+").last
                if await plus_btn.count() > 0:
                    await plus_btn.click()
                    await asyncio.sleep(1)
                await set_artist_with_create_fallback(page, "Дополнительный исполнитель", artist)

            current_step = "fill_release_fields"
            await fill_input_by_prompt(page, "Введите название релиза", release_meta.get("title", ""))
            await fill_input_by_prompt(page, "Введите версию релиза", release_meta.get("version", ""))
            await set_artist_with_create_fallback(page, "Лейбл", main_artist)
            
            create_label_btn = page.locator('button:has-text("Проверить и создать лейбл")').first
            if await create_label_btn.count() > 0 and await create_label_btn.is_visible():
                await create_label_btn.click()

            date_value = release_meta.get("release_date", "")
            if date_value:
                normalized_date = date_value.replace("-", ".")
                await enable_toggle_by_text(page, "Оригинальная дата релиза")
                await fill_input_by_label(page, "Оригинальная дата релиза", normalized_date)

            upc = release_meta.get("upc", "")
            if upc:
                await enable_toggle_by_text(page, "У меня есть свой EAN/UPC")
                await fill_input_by_prompt(page, "EAN/UPC", upc)

            current_step = "return_releases"
            await page.goto("https://app.musicalligator.ru/releases", timeout=90000, wait_until="domcontentloaded")
            await browser.close()
            return {"status": "success", "message": f"Релиз отправлен в кабинет {account['note']}"}
            
        except Exception as e:
            debug_path = os.path.join(BASE_DIR, "error_musicalligator.png")
            try: await page.screenshot(path=debug_path, full_page=True)
            except: pass
            await browser.close()
            logger.error("Ошибка MusicAlligator: %s", e)
            return {"status": "error", "message": f"Ошибка MusicAlligator: {str(e)[:180]}"}

async def scrape_ordistribution(target_artist: str, target_release: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True, viewport={'width': 1920, 'height': 1080})
        page = await context.new_page()
        try:
            await page.goto("https://ordistribution.com/login", timeout=60000)
            await page.fill('input[type="email"]', LOGIN) 
            await page.fill('input[type="password"]', PASSWORD)
            await page.click('button[type="submit"]')
            await page.wait_for_load_state("networkidle")
            await page.goto("https://ordistribution.com/admin/dashboard", timeout=60000)
            await asyncio.sleep(5)

            search_input = page.locator('input[type="search"]').first
            await search_input.fill(target_release)
            await asyncio.sleep(3)

            card = page.locator(f'div:has-text("{target_release}")').filter(has_text="Подробнее").first
            if await card.count() == 0:
                await search_input.fill(target_artist)
                await asyncio.sleep(3)
                card = page.locator(f'div:has-text("{target_release}")').filter(has_text="Подробнее").first

            if await card.count() == 0:
                await browser.close()
                return {"status": "error", "message": "Релиз не найден в OR"}

            row_info = await card.inner_text()
            await card.locator('button:has-text("Подробнее"), a:has-text("Подробнее")').first.click()
            await asyncio.sleep(2)

            async with page.expect_download(timeout=60000) as download_info:
                await page.locator('button:has-text("Скачать ZIP"), a:has-text("Скачать ZIP")').first.click()
            download = await download_info.value
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            await browser.close()
            return {"status": "success", "info": row_info, "file_path": file_path}
        except Exception as e:
            await browser.close()
            return {"status": "error", "message": str(e)}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_authorized(message): return
    await message.answer("Пришли: `Артист - Название релиза`")

@dp.message(Command("accounts"))
async def cmd_accounts(message: types.Message):
    if not is_authorized(message): return
    args = (message.text or "").replace("/accounts", "", 1).strip()
    data = read_accounts()
    accounts = data.get("accounts", [])
    active_idx = data.get("active_index")

    if not args:
        lines = ["Аккаунты MusicAlligator:"]
        for i, acc in enumerate(accounts, start=1):
            marker = "✅" if active_idx == (i - 1) else "•"
            lines.append(f"{marker} {i}. {acc['note']} | {acc['email']}")
        await message.answer("\n".join(lines) + "\n\n/accounts add... | /accounts use 1")
        return

    if args.lower().startswith("add "):
        parts = [p.strip() for p in args[4:].split("|")]
        if len(parts) == 3:
            accounts.append({"note": parts[0], "email": parts[1], "password": parts[2]})
            if data["active_index"] is None: data["active_index"] = 0
            write_accounts(data)
            await message.answer("Добавлен.")
    elif args.lower().startswith("use "):
        idx = int(args[4:]) - 1
        if 0 <= idx < len(accounts):
            data["active_index"] = idx
            write_accounts(data)
            await message.answer(f"Активен: {accounts[idx]['note']}")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if not is_authorized(message): return
    if " - " not in message.text: return
    artist, release = message.text.split(" - ", 1)
    status_msg = await message.answer(f"🔍 Работаю...")

    result = await scrape_ordistribution(artist.strip(), release.strip())
    if result["status"] == "error":
        await status_msg.edit_text(f"❌ {result['message']}")
        return

    release_meta = parse_release_info(result["info"], artist.strip(), release.strip())
    release_meta["title"] = release.strip()
    
    upload_result = await upload_to_musicalligator(release_meta, result["file_path"])
    await status_msg.edit_text(f"Результат: {upload_result['message']}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
