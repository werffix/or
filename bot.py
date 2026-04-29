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

    def field_value(label_patterns):
        for i, line in enumerate(raw_lines):
            for pattern in label_patterns:
                same_line = re.search(
                    rf"^\s*{pattern}\s*:?\s*(.+)$",
                    line,
                    flags=re.I
                )
                if same_line:
                    value = same_line.group(1).strip()
                    if value:
                        return value

                if re.fullmatch(rf"\s*{pattern}\s*:?", line, flags=re.I):
                    for next_line in raw_lines[i + 1:i + 4]:
                        if next_line and not re.fullmatch(
                            r"(Название релиза|Артисты|Исполнитель|Подзаголовок|Версия|Дата релиза|UPC|EAN|Статус|Платформа)\s*:?",
                            next_line,
                            flags=re.I
                        ):
                            return next_line.strip()

        for pattern in label_patterns:
            flat_match = re.search(
                rf"(?:^|\s){pattern}\s*:?\s*(.+?)(?=\s+(Название релиза|Артисты|Исполнитель|Подзаголовок|Версия|Дата релиза|UPC|EAN|Статус|Платформа)\s*:|\s*$)",
                info,
                flags=re.I
            )
            if flat_match:
                return flat_match.group(1).strip()

        return ""

    # Предпочитаем явные поля из OR, если они есть в карточке/деталях.
    explicit_title = field_value([r"Название релиза"])
    explicit_artists = field_value([r"Артисты", r"Исполнитель"])
    explicit_version = field_value([r"Подзаголовок", r"Версия релиза", r"Версия"])
    explicit_date = field_value([r"Дата релиза"])
    explicit_upc = field_value([r"UPC/EAN", r"EAN/UPC", r"UPC", r"EAN"])

    if explicit_title:
        title = explicit_title
    if explicit_artists:
        artist = explicit_artists
    if explicit_version:
        version = explicit_version
    if explicit_date:
        release_date = explicit_date
    if explicit_upc:
        upc = re.sub(r"\D", "", explicit_upc)

    # Сначала пробуем взять название/артиста из первых строк карточки OR.
    if (not explicit_title or not explicit_artists) and len(raw_lines) >= 2:
        first = raw_lines[0]
        second = raw_lines[1]
        if not explicit_title and first and not any(x in first.lower() for x in ["одобрен", "релиз:", "платформа:", "upc", "ean"]):
            title = first
        if not explicit_artists and second and not any(x in second.lower() for x in ["одобрен", "релиз:", "платформа:", "upc", "ean"]):
            artist = second

    # fallback на старый regex по плоскому тексту
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

    date_match = re.search(r"релиз:\s*([0-9]{2}[-.][0-9]{2}[-.][0-9]{4})", info, flags=re.I)
    if not release_date and date_match:
        release_date = date_match.group(1)

    upc_match = re.search(r"(upc|ean)\s*:\s*([0-9]{8,20})", info, flags=re.I)
    if not upc and upc_match:
        upc = upc_match.group(2)

    version_match = re.search(r"\(([^)]+)\)", title)
    if not version and version_match:
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
    wav_paths = []
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
            elif lower.endswith(".wav"):
                wav_paths.append(full)

    if cover_candidates:
        cover_candidates.sort(key=lambda x: x[0], reverse=True)
        cover_path = cover_candidates[0][1]

    def wav_sort_key(path):
        name = Path(path).name
        prefix_match = re.match(r"^\D*([0-9]{1,3})", name)
        if prefix_match:
            return (int(prefix_match.group(1)), name.lower())
        return (9999, name.lower())

    wav_paths.sort(key=wav_sort_key)

    return {"extract_dir": target_dir, "cover_path": cover_path, "wav_paths": wav_paths}


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

    roots = [
        page.locator(f'.ui-input-row:has(label:has-text("{label_text}"))'),
        page.locator(f'div:has(> label:has-text("{label_text}"))'),
    ]

    input_locator = None
    for root in roots:
        if await root.count() == 0:
            continue
        candidate = root.first.locator('input:not([disabled]), textarea:not([disabled])').first
        if await candidate.count() > 0:
            input_locator = candidate
            break

    if input_locator is None:
        return False

    await input_locator.click()
    await input_locator.press("Meta+A")
    await input_locator.press("Backspace")
    await input_locator.fill(value)
    return True


async def fill_input_by_prompt(page, prompt_text: str, value: str, press_enter: bool = False, occurrence: int = 0):
    if not value:
        return False

    selectors = [
        f'div.ui-input:has(span:has-text("{prompt_text}")) input',
        f'div.gs-search:has(span:has-text("{prompt_text}")) input',
        f'input[placeholder*="{prompt_text}" i]',
    ]

    target = None
    for selector in selectors:
        locators = page.locator(selector)
        count = await locators.count()
        if count > 0:
            index = occurrence if occurrence >= 0 else count + occurrence
            index = max(0, min(index, count - 1))
            locator = locators.nth(index)
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
        # В этом UI поле может перерисоваться после fill; подтверждаем через активный фокус.
        await page.keyboard.press("Enter")
    return True


async def fill_select_input_by_field_label(page, field_label: str, value: str, press_enter: bool = True, occurrence: int = 0):
    if not value:
        return False

    roots = [
        page.locator(f'.ui-artists-select:has(label p:text-is("{field_label}"))'),
        page.locator(f'.ui-labels-select:has(label p:text-is("{field_label}"))'),
    ]

    root = None
    for candidate in roots:
        count = await candidate.count()
        if count > 0:
            index = occurrence if occurrence >= 0 else count + occurrence
            index = max(0, min(index, count - 1))
            root = candidate.nth(index)
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
    await asyncio.sleep(0.8)

    option_roots = [
        page.locator('.gs-options'),
        page.locator('.v-popper__popper'),
        page.locator('.dropdown'),
    ]
    for option_root in option_roots:
        if await option_root.count() == 0:
            continue
        exact_option = option_root.get_by_text(value, exact=True).first
        if await exact_option.count() > 0 and await exact_option.is_visible():
            await exact_option.click(timeout=3000)
            await asyncio.sleep(0.3)
            return True

    if press_enter:
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.3)
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


def normalize_musicalligator_date(date_value: str) -> str:
    value = date_value.strip()
    yyyy_mm_dd = re.fullmatch(r"([0-9]{4})[-.]([0-9]{2})[-.]([0-9]{2})", value)
    if yyyy_mm_dd:
        return f"{yyyy_mm_dd.group(3)}.{yyyy_mm_dd.group(2)}.{yyyy_mm_dd.group(1)}"

    dd_mm_yyyy = re.fullmatch(r"([0-9]{2})[-.]([0-9]{2})[-.]([0-9]{4})", value)
    if dd_mm_yyyy:
        return f"{dd_mm_yyyy.group(1)}.{dd_mm_yyyy.group(2)}.{dd_mm_yyyy.group(3)}"

    return value.replace("-", ".")


async def set_calendar_input_value(page, input_locator, date_value: str):
    await input_locator.scroll_into_view_if_needed(timeout=3000)
    try:
        await input_locator.click(timeout=3000, force=True)
        await page.keyboard.press("Meta+A")
        await page.keyboard.type(date_value, delay=25)
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)
        return True
    except Exception:
        pass

    try:
        await input_locator.evaluate(
            """(el, value) => {
                el.removeAttribute('disabled');
                const prototype = Object.getPrototypeOf(el);
                const valueSetter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                if (valueSetter) {
                    valueSetter.call(el, value);
                } else {
                    el.value = value;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            date_value,
        )
        await asyncio.sleep(0.5)
        return True
    except Exception:
        return False


async def set_original_release_date(page, date_value: str):
    normalized_date = normalize_musicalligator_date(date_value)
    toggle_ok = await enable_toggle_by_text(page, "Оригинальная дата релиза")
    if not toggle_ok:
        logger.warning("Не удалось включить тумблер 'Оригинальная дата релиза'")
        return False

    await asyncio.sleep(0.8)

    calendar_candidates = [
        page.locator(
            '.col-6:has(.row-toggle p:has-text("Оригинальная дата релиза")) '
            '.-full-calendar input[type="text"]'
        ).last,
        page.locator(
            '.row-select:has(.row-toggle p:has-text("Оригинальная дата релиза")) '
            '.-full-calendar input[type="text"]'
        ).last,
        page.locator('.-full-calendar input[type="text"]:not([disabled])').last,
        page.locator('.v-popper input[type="text"]:not([disabled])').last,
    ]

    for date_input in calendar_candidates:
        if await date_input.count() == 0:
            continue
        if await set_calendar_input_value(page, date_input, normalized_date):
            return True

    logger.warning("Не удалось выбрать оригинальную дату релиза")
    return False


async def select_release_type(page, type_text: str):
    type_card = page.locator(f'.release-type .type:has(p:text-is("{type_text}"))').first
    if await type_card.count() == 0:
        logger.warning("Не найден тип релиза: %s", type_text)
        return False

    try:
        class_attr = await type_card.get_attribute("class") or ""
        if "-active" in class_attr:
            return True
    except Exception:
        pass

    await type_card.click(timeout=5000)
    await asyncio.sleep(1)
    return True


async def go_to_tracks_step(page):
    button = page.locator('button:has-text("Перейти к загрузке треков")').first
    if await button.count() == 0:
        logger.warning("Кнопка 'Перейти к загрузке треков' не найдена")
        return False

    await button.scroll_into_view_if_needed(timeout=3000)
    await button.click(timeout=5000)
    await page.wait_for_selector('.release-track, input[type="file"][accept*="audio"]', timeout=90000)
    await asyncio.sleep(1)
    return True


async def ensure_track_slots(page, track_count: int):
    if track_count <= 1:
        return True

    for _ in range(track_count - 1):
        current_count = await page.locator('.release-track').count()
        if current_count >= track_count:
            return True

        add_button = page.locator('.release-track-add:not(.disabled), .release-track-add').last
        if await add_button.count() == 0:
            logger.warning("Кнопка 'Добавить трек' не найдена")
            return False

        await add_button.scroll_into_view_if_needed(timeout=3000)
        class_attr = await add_button.get_attribute("class") or ""
        if "disabled" in class_attr:
            logger.warning("Кнопка 'Добавить трек' отключена")
            return False

        await add_button.click(timeout=5000)
        await asyncio.sleep(1)

    return await page.locator('.release-track').count() >= track_count


async def open_track_card(track):
    file_input = track.locator('input[type="file"][accept*="audio"]').first
    if await file_input.count() > 0:
        return True

    toggles = [
        track.locator('.-track-counter .-toggle').first,
        track.locator('.-track-counter svg').first,
        track.locator('h4:has-text("Трек")').first,
    ]

    for toggle in toggles:
        if await toggle.count() == 0:
            continue
        try:
            await toggle.scroll_into_view_if_needed(timeout=3000)
            await toggle.click(timeout=5000)
            await asyncio.sleep(0.8)
            if await file_input.count() > 0:
                return True
        except Exception:
            pass

    return await file_input.count() > 0


async def upload_wav_tracks(page, wav_paths):
    if not wav_paths:
        logger.warning("В архиве нет WAV-файлов для загрузки треков")
        return False

    await ensure_track_slots(page, len(wav_paths))

    for index, wav_path in enumerate(wav_paths):
        tracks = page.locator('.release-track')
        if await tracks.count() <= index:
            logger.warning("Не найден блок %s-го трека", index + 1)
            return False

        track = tracks.nth(index)
        await track.scroll_into_view_if_needed(timeout=5000)
        await open_track_card(track)

        file_input = track.locator('input[type="file"][accept*="audio"]').first
        if await file_input.count() == 0:
            logger.warning("Не найден input загрузки для %s-го трека", index + 1)
            return False

        logger.info("Загружаем WAV для %s-го трека: %s", index + 1, wav_path)
        await file_input.set_input_files(wav_path)
        await asyncio.sleep(3)

    return True


async def click_additional_artist_plus(page):
    section = page.locator('.row-add:has(.ui-artists-select label p:text-is("Дополнительный исполнитель"))').last
    candidates = [
        section.locator('button:has(svg), button:has-text("+"), .plus, [class*="plus"]').last,
        page.locator('button:has-text("+"), button[aria-label*="Добав" i], button[title*="Добав" i]').last,
    ]

    for candidate in candidates:
        if await candidate.count() == 0:
            continue
        try:
            if await candidate.is_visible():
                await candidate.click(timeout=3000)
                await asyncio.sleep(0.5)
                return True
        except Exception:
            pass

    logger.warning("Не удалось нажать плюс дополнительного исполнителя")
    return False


async def set_artist_with_create_fallback(page, label_text: str, artist_name: str, occurrence: int = 0):
    if not artist_name:
        return

    prompt_map = {
        "Исполнитель": "Введите основного исполнителя",
        "Дополнительный исполнитель": "Введите доп. исполнителя",
        "При участии (feat.)": "Введите feat. исполнителя",
    }
    prompt = prompt_map.get(label_text, "")

    ok = await fill_select_input_by_field_label(page, label_text, artist_name, press_enter=True, occurrence=occurrence)
    if not ok and prompt:
        ok = await fill_input_by_prompt(page, prompt, artist_name, press_enter=True, occurrence=occurrence)
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
    wav_paths = assets.get("wav_paths", [])

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

            if len(wav_paths) >= 3:
                current_step = "select_release_type"
                await select_release_type(page, "EP / Альбом")

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
            for index, extra_artist in enumerate(extra_artists):
                if index > 0:
                    await click_additional_artist_plus(page)
                await set_artist_with_create_fallback(
                    page,
                    "Дополнительный исполнитель",
                    extra_artist,
                    occurrence=-1
                )

            current_step = "fill_release_fields"
            title_ok = await fill_input_by_prompt(page, "Введите название релиза", release_meta.get("title", ""))
            if not title_ok:
                await fill_input_by_label(page, "Название релиза", release_meta.get("title", ""))

            version_ok = await fill_input_by_prompt(page, "Введите версию релиза", release_meta.get("version", ""))
            if not version_ok:
                await fill_input_by_label(page, "Версия релиза", release_meta.get("version", ""))

            # Лейбл = первый артист; если не найден, создаем лейбл.
            label_ok = await fill_select_input_by_field_label(page, "Лейбл", main_artist, press_enter=True)
            if not label_ok:
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
                await set_original_release_date(page, date_value)

            # Свой EAN/UPC
            upc = release_meta.get("upc", "")
            if upc:
                await enable_toggle_by_text(page, "У меня есть свой EAN/UPC")
                upc_ok = await fill_input_by_prompt(page, "EAN/UPC", upc)
                if not upc_ok:
                    await fill_input_by_label(page, "EAN/UPC", upc)

            if wav_paths:
                current_step = "upload_tracks"
                if await go_to_tracks_step(page):
                    tracks_uploaded = await upload_wav_tracks(page, wav_paths)
                    if not tracks_uploaded:
                        raise RuntimeError("Не удалось загрузить WAV-треки")
                else:
                    raise RuntimeError("Не удалось перейти к загрузке треков")

            current_step = "return_releases"
            await page.reload(timeout=90000, wait_until="domcontentloaded")
            await page.goto("https://app.musicalligator.ru/releases", timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_selector('button:has-text("Новый релиз")', timeout=90000)

            await browser.close()
            no_cover_note = " (без обложки)" if not cover_path else ""
            tracks_note = f", WAV-треков: {len(wav_paths)}" if wav_paths else ""
            return {"status": "success", "message": f"Релиз отправлен в кабинет {account['note']} ({account['email']}){no_cover_note}{tracks_note}"}
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
                async def visible_zip_buttons():
                    candidates = [
                        page.get_by_role("button", name=re.compile(r"(Скачать|Загрузить).*ZIP", re.I)),
                        page.get_by_role("link", name=re.compile(r"(Скачать|Загрузить).*ZIP", re.I)),
                        page.locator('button:has-text("Скачать ZIP"), a:has-text("Скачать ZIP")'),
                        page.locator('button:has-text("ZIP"), a:has-text("ZIP"), a[href*="zip" i], a[href*="download" i]'),
                    ]

                    buttons = []
                    for candidate in candidates:
                        count = await candidate.count()
                        for index in range(count):
                            button = candidate.nth(index)
                            try:
                                if await button.is_visible():
                                    buttons.append(button)
                            except Exception:
                                pass
                    return buttons

                async def click_and_catch_download(button, timeout=15000):
                    try:
                        async with page.expect_download(timeout=timeout) as download_info:
                            await button.click(timeout=5000)
                        return await download_info.value
                    except PlaywrightTimeoutError:
                        return None

                deadline = asyncio.get_running_loop().time() + 600
                archive_started = False

                while asyncio.get_running_loop().time() < deadline:
                    body_text = " ".join((await page.locator("body").inner_text()).split())
                    progress_match = re.search(r"Подготовка архива[^0-9]*(\d+)%", body_text, flags=re.I)
                    if progress_match:
                        archive_started = True
                        logger.info("Подготовка архива: %s%%", progress_match.group(1))
                        await asyncio.sleep(10)
                        continue

                    buttons = await visible_zip_buttons()
                    for button in buttons:
                        try:
                            button_text = " ".join((await button.inner_text()).split())
                        except Exception:
                            button_text = ""

                        if re.search(r"Подготовка|архив.*\d+%", button_text, flags=re.I):
                            archive_started = True
                            continue

                        logger.info("Найдена кнопка скачивания ZIP")
                        download = await click_and_catch_download(button)
                        if download:
                            return download

                        archive_started = True
                        logger.info("ZIP еще не скачивается, ожидаем подготовку архива")
                        break

                    if not buttons and not archive_started:
                        await asyncio.sleep(2)
                    else:
                        await asyncio.sleep(10)

                raise RuntimeError("ZIP не подготовился/не скачался за 10 минут")

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

            details_info = await page.locator("body").inner_text()
            form_info = await page.locator("body").evaluate(
                """(body) => {
                    const controls = Array.from(body.querySelectorAll('input, textarea, select'));
                    const lines = [];

                    for (const control of controls) {
                        const value = control.value || control.getAttribute('value') || '';
                        if (!value.trim()) continue;

                        const id = control.id;
                        const explicitLabel = id ? body.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                        const wrappingLabel = control.closest('label');
                        const containerLabel = control.closest('div, section, article')?.querySelector('label, p, span');
                        const label = (
                            explicitLabel?.innerText ||
                            wrappingLabel?.innerText ||
                            control.getAttribute('aria-label') ||
                            control.getAttribute('placeholder') ||
                            control.getAttribute('name') ||
                            containerLabel?.innerText ||
                            ''
                        ).trim();

                        if (label) {
                            lines.push(`${label}: ${value.trim()}`);
                        }
                    }

                    return lines.join('\\n');
                }"""
            )
            download = await download_zip_from_details()
            file_path = os.path.join(downloads_path, download.suggested_filename)
            await download.save_as(file_path)
            
            await browser.close()
            return {
                "status": "success", 
                "info": f"{row_info}\n{details_info}\n{form_info}".strip(), 
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

async def process_release_line(status_msg, artist: str, release: str, prefix: str = ""):
    result = await scrape_ordistribution(artist.strip(), release.strip())

    if result["status"] == "error":
        error_file = os.path.join(BASE_DIR, "error_debug.png")
        if os.path.exists(error_file):
            await bot.send_photo(status_msg.chat.id, FSInputFile(error_file), caption=f"❌ {prefix}{result['message']}")
        else:
            await status_msg.edit_text(f"❌ {prefix}{result['message']}")
        return {"status": "error", "message": result["message"]}

    file_path = result["file_path"]
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)

    release_meta = parse_release_info(
        result["info"],
        fallback_artist=artist.strip(),
        fallback_release=release.strip()
    )
    upload_result = await upload_to_musicalligator(release_meta, file_path)
    if upload_result["status"] == "error":
        await status_msg.edit_text(
            f"⚠️ {prefix}ZIP скачан, но автозагрузка в MusicAlligator не завершилась.\n\n"
            f"`{upload_result['message']}`\n\n"
            f"Файл: `{os.path.basename(file_path)}`\n"
            f"Размер: `{file_size_mb:.1f} MB`\n"
            f"Путь: `{file_path}`"
        )
        return {"status": "error", "message": upload_result["message"]}

    return {
        "status": "success",
        "message": upload_result["message"],
        "zip": os.path.basename(file_path),
        "info": result["info"],
    }


@dp.message(F.text)
async def handle_request(message: types.Message):
    if not is_authorized(message):
        await message.answer("⛔ Доступ запрещен")
        return

    release_lines = []
    for line in (message.text or "").splitlines():
        line = line.strip()
        if " - " not in line:
            continue
        artist, release = line.split(" - ", 1)
        artist = artist.strip()
        release = release.strip()
        if artist and release:
            release_lines.append((artist, release))

    if not release_lines:
        return

    status_msg = await message.answer(f"🔍 В очереди релизов: {len(release_lines)}")
    results = []

    for index, (artist, release) in enumerate(release_lines, start=1):
        prefix = f"[{index}/{len(release_lines)}] {artist} - {release}: "
        await status_msg.edit_text(f"🔍 {prefix}ищу в OR...")
        result = await process_release_line(status_msg, artist, release, prefix=prefix)
        results.append((artist, release, result))

        if result["status"] == "error":
            if len(release_lines) == 1:
                return
            await asyncio.sleep(1)
            continue

        await status_msg.edit_text(f"✅ {prefix}готово\n{result['message']}")

    if len(release_lines) == 1:
        artist, release, result = results[0]
        if result["status"] == "success":
            await status_msg.edit_text(
                "✅ Готово: релиз обработан и отправлен в MusicAlligator.\n\n"
                f"{result['message']}\n"
                f"Файл ZIP: `{result['zip']}`"
            )
        return

    lines = ["Готово по очереди:"]
    for index, (artist, release, result) in enumerate(results, start=1):
        mark = "✅" if result["status"] == "success" else "❌"
        lines.append(f"{mark} {index}. {artist} - {release}: {result['message']}")
    await status_msg.edit_text("\n".join(lines))

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
