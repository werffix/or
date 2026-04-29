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


def parse_release_info(row_info: str):
    info = " ".join(row_info.split())

    title = ""
    artist = ""
    release_date = ""
    upc = ""
    version = ""

    title_artist_match = re.match(r"^\s*(.*?)\s{1,}([^\s].*?)\s+(одобрен|на рассмотрении|отклонен|черновик)", info, flags=re.I)
    if title_artist_match:
        title = title_artist_match.group(1).strip()
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
    await input_locator.press("Meta+A")
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

    await target.click()
    await target.press("Meta+A")
    await target.press("Backspace")
    await target.fill(value)
    await asyncio.sleep(0.2)
    if press_enter:
        await target.press("Enter")
    return True


async def enable_toggle_by_text(page, toggle_text: str):
    # В MusicAlligator сам input чекбокса часто скрыт, кликабельный элемент - switch/slider рядом с подписью.
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

    # 1) Пытаемся кликнуть по видимому слайдеру
    slider = root.locator('.slider, .ui-switch, label.ui-switch, span.round').first
    if await slider.count() > 0:
        try:
            await slider.click(timeout=3000)
            await asyncio.sleep(0.2)
            if await checkbox.is_checked():
                return True
        except Exception:
            pass

    # 2) Пытаемся кликнуть по тексту строки
    text_node = root.locator(f'p:has-text("{toggle_text}")').first
    if await text_node.count() > 0:
        try:
            await text_node.click(timeout=3000)
            await asyncio.sleep(0.2)
            if await checkbox.is_checked():
                return True
        except Exception:
            pass

    # 3) Fallback: выставляем состояние через JS и шлем события
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
    }
    prompt = prompt_map.get(label_text, "")

    ok = False
    if prompt:
        ok = await fill_input_by_prompt(page, prompt, artist_name, press_enter=True)
    if not ok:
        ok = await fill_input_by_label(page, label_text, artist_name)
        if ok:
            artist_input = page.locator('input:focus').first
            if await artist_input.count() > 0:
                await artist_input.press("Enter")
    if not ok:
        logger.warning("Не удалось заполнить поле артиста: %s", label_text)
        return

    await asyncio.sleep(1)
    create_btn = page.locator('button:has-text("Создать исполнителя"), div:has-text("Создать исполнителя")').first
    if await create_btn.count() > 0 and await create_btn.is_visible():
        await create_btn.click()
        await asyncio.sleep(1)
        no_buttons = page.locator('button:has-text("Нет"), div:has-text("Нет")')
        if await no_buttons.count() >= 2:
            await no_buttons.nth(0).click()
            await no_buttons.nth(1).click()
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
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
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
            else:
                logger.warning("В ZIP не найдена обложка — продолжаем без загрузки обложки")

            artists = release_meta.get("artists", [])
            main_artist = artists[0] if artists else ""
            extra_artists = artists[1:] if len(artists) > 1 else []

            current_step = "fill_artist"
            await set_artist_with_create_fallback(page, "Исполнитель", main_artist)
            if extra_artists:
                await set_artist_with_create_fallback(page, "Дополнительный исполнитель", extra_artists[0])

            current_step = "fill_release_fields"
            title_ok = await fill_input_by_prompt(page, "Введите название релиза", release_meta.get("title", ""))
            if not title_ok:
                await fill_input_by_label(page, "Название релиза", release_meta.get("title", ""))

            version_ok = await fill_input_by_prompt(page, "Введите версию релиза", release_meta.get("version", ""))
            if not version_ok:
                await fill_input_by_label(page, "Версия релиза", release_meta.get("version", ""))

            # Лейбл = первый артист; если не найден, создаем лейбл.
            label_ok = await fill_input_by_prompt(page, "Введите лейбл", main_artist, press_enter=True)
            if not label_ok:
                label_ok = await fill_input_by_label(page, "Лейбл", main_artist)
            if not label_ok:
                logger.warning("Не удалось заполнить поле лейбла")
            await asyncio.sleep(1)
            create_label_btn = page.locator('button:has-text("Проверить и создать лейбл"), div:has-text("Проверить и создать лейбл")').first
            if await create_label_btn.count() > 0 and await create_label_btn.is_visible():
                await create_label_btn.click()

            # Оригинальная дата релиза
            date_value = release_meta.get("release_date", "")
            if date_value:
                await enable_toggle_by_text(page, "Оригинальная дата релиза")
                date_ok = await fill_input_by_label(page, "Оригинальная дата релиза", date_value)
                if not date_ok:
                    # Fallback по текущему открытому календарному input.
                    date_input = page.locator('input.-filled, input[value*="."]').first
                    if await date_input.count() > 0:
                        await date_input.click()
                        await date_input.press("Meta+A")
                        await date_input.type(date_value, delay=30)

            # Свой EAN/UPC
            upc = release_meta.get("upc", "")
            if upc:
                await enable_toggle_by_text(page, "У меня есть свой EAN/UPC")
                upc_ok = await fill_input_by_prompt(page, "EAN/UPC", upc)
                if not upc_ok:
                    await fill_input_by_label(page, "EAN/UPC", upc)

            current_step = "return_releases"
            await page.goto("https://app.musicalligator.ru/releases", timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_selector('button:has-text("Новый релиз")', timeout=90000)

            await browser.close()
            no_cover_note = " (без обложки)" if not cover_path else ""
            return {"status": "success", "message": f"Релиз отправлен в кабинет {account['note']} ({account['email']}){no_cover_note}"}
        except Exception as e:
            debug_path = os.path.join(BASE_DIR, "error_musicalligator.png")
            try:
                await page.screenshot(path=debug_path, full_page=True)
            except Exception:
                pass
            await browser.close()
            logger.error("Ошибка MusicAlligator на шаге '%s': %s", current_step, e, exc_info=True)
            return {"status": "error", "message": f"Ошибка MusicAlligator (шаг: {current_step}): {str(e)[:180]}"}

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
    if not is_authorized(message):
        await message.answer("⛔ Доступ запрещен")
        return
    await message.answer("Пришли: `Артист - Название релиза`\n\n/accounts - управление кабинетами MusicAlligator")


@dp.message(Command("accounts"))
async def cmd_accounts(message: types.Message):
    if not is_authorized(message):
        await message.answer("⛔ Доступ запрещен")
        return

    args = (message.text or "").replace("/accounts", "", 1).strip()
    data = read_accounts()
    accounts = data.get("accounts", [])
    active_idx = data.get("active_index")

    if not args:
        lines = ["Аккаунты MusicAlligator:"]
        if not accounts:
            lines.append("Пусто")
        for i, acc in enumerate(accounts, start=1):
            marker = "✅" if active_idx == (i - 1) else "•"
            lines.append(f"{marker} {i}. {acc['note']} | {acc['email']}")
        lines.append("")
        lines.append("Команды:")
        lines.append("/accounts add Примечание | email@example.com | password")
        lines.append("/accounts use 1")
        lines.append("/accounts del 1")
        await message.answer("\n".join(lines))
        return

    if args.lower().startswith("add "):
        payload = args[4:].strip()
        parts = [p.strip() for p in payload.split("|")]
        if len(parts) != 3:
            await message.answer("Формат: /accounts add Примечание | email | password")
            return
        note, email, password = parts
        if len(accounts) >= 5:
            await message.answer("Лимит: максимум 5 аккаунтов")
            return
        accounts.append({"note": note, "email": email, "password": password})
        if data.get("active_index") is None:
            data["active_index"] = 0
        write_accounts(data)
        await message.answer(f"Добавлен аккаунт: {note}")
        return

    if args.lower().startswith("use "):
        try:
            idx = int(args[4:].strip()) - 1
        except ValueError:
            await message.answer("Формат: /accounts use 1")
            return
        if idx < 0 or idx >= len(accounts):
            await message.answer("Нет такого аккаунта")
            return
        data["active_index"] = idx
        write_accounts(data)
        await message.answer(f"Активный аккаунт: {accounts[idx]['note']}")
        return

    if args.lower().startswith("del "):
        try:
            idx = int(args[4:].strip()) - 1
        except ValueError:
            await message.answer("Формат: /accounts del 1")
            return
        if idx < 0 or idx >= len(accounts):
            await message.answer("Нет такого аккаунта")
            return
        removed = accounts.pop(idx)
        active_idx = data.get("active_index")
        if active_idx is not None:
            if idx == active_idx:
                data["active_index"] = 0 if accounts else None
            elif idx < active_idx:
                data["active_index"] = active_idx - 1
        write_accounts(data)
        await message.answer(f"Удален аккаунт: {removed['note']}")
        return

    await message.answer("Неизвестная команда. Используй /accounts")

@dp.message(F.text)
async def handle_request(message: types.Message):
    if not is_authorized(message):
        await message.answer("⛔ Доступ запрещен")
        return

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

    file_path = result["file_path"]
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)

    release_meta = parse_release_info(result["info"])
    upload_result = await upload_to_musicalligator(release_meta, file_path)
    if upload_result["status"] == "error":
        await status_msg.edit_text(
            "⚠️ ZIP скачан, но автозагрузка в MusicAlligator не завершилась.\n\n"
            f"`{upload_result['message']}`\n\n"
            f"Файл: `{os.path.basename(file_path)}`\n"
            f"Размер: `{file_size_mb:.1f} MB`\n"
            f"Путь: `{file_path}`"
        )
        return

    await status_msg.edit_text(
        "✅ Готово: релиз обработан и отправлен в MusicAlligator.\n\n"
        f"`{result['info']}`\n\n"
        f"{upload_result['message']}\n"
        f"Файл ZIP: `{os.path.basename(file_path)}`"
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
