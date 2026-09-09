import asyncio
import datetime
import json
import logging
import os
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Optional, TextIO

from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger: logging.Logger = logging.getLogger("OzonParser")

JS_EXTRACTOR: str = """
() => {
    const results = [];
    const seenUrls = new Set();

    const parsePrice = (str) => {
        const clean = str.replace(/[^0-9]/g, '');
        return clean ? parseInt(clean, 10) : null;
    };

    let container = document.querySelector('[data-widget="searchResultsV2"]') 
        || document.querySelector('[data-widget="megaPaginator"]')
        || document.querySelector('[data-widget="tileGrid"]');

    if (!container) {
        const allWidgets = Array.from(document.querySelectorAll('[data-widget]'));
        for (const w of allWidgets) {
            const name = (w.getAttribute('data-widget') || '').toLowerCase();
            if (name.includes('carousel') || name.includes('banner') || name.includes('recom')) {
                continue;
            }
            const productLinks = w.querySelectorAll('a[href*="/product/"]');
            if (productLinks.length >= 8) {
                container = w;
                break;
            }
        }
    }

    if (!container) {
        return results;
    }

    let cards = Array.from(container.querySelectorAll('div[data-index]'));
    if (cards.length === 0) {
        const pLinks = Array.from(container.querySelectorAll('a[href*="/product/"]'));
        const cardSet = new Set();
        for (const a of pLinks) {
            let cur = a;
            while (cur && cur.parentElement && cur.parentElement !== container && cur.offsetHeight < 280) {
                cur = cur.parentElement;
            }
            if (cur && cur !== container) {
                cardSet.add(cur);
            }
        }
        cards = Array.from(cardSet);
    }

    for (const card of cards) {
        try {
            const a = card.querySelector('a[href*="/product/"]');
            if (!a) continue;

            const href = a.getAttribute('href');
            if (!href) continue;

            const cleanUrl = href.startsWith('http') 
                ? href.split('?')[0] 
                : 'https://www.ozon.ru' + href.split('?')[0];

            if (seenUrls.has(cleanUrl)) continue;

            const cardText = card.innerText || '';
            if (!cardText.includes('₽')) continue;

            const priceMatches = [...cardText.matchAll(/(?:^|[^0-9])([0-9\\s\\u00a0\\u2009]{4,})\\s*₽(?!\\s*\\/)/g)];
            if (priceMatches.length === 0) continue;

            const prices = priceMatches
                .map(m => parsePrice(m[1]))
                .filter(p => p !== null && p >= 10000);

            if (prices.length === 0) continue;

            const priceCurrent = prices[0];
            const priceOriginal = prices.length > 1 ? prices[1] : priceCurrent;

            let title = '';
            const img = card.querySelector('img[alt]');
            if (img && img.getAttribute('alt') && img.getAttribute('alt').trim().length > 10) {
                const altText = img.getAttribute('alt').trim();
                if (!altText.toLowerCase().includes('ozon') && !altText.toLowerCase().includes('логотип')) {
                    title = altText;
                }
            }

            if (!title) {
                const linkSpans = Array.from(card.querySelectorAll('a[href*="/product/"] span, a[href*="/product/"]'));
                for (const el of linkSpans) {
                    const txt = el.innerText ? el.innerText.trim() : '';
                    if (txt.length > 15 && !txt.includes('₽') && !txt.toLowerCase().includes('отзыв')) {
                        title = txt;
                        break;
                    }
                }
            }

            if (!title) continue;

            const lowerTitle = title.toLowerCase();
            const isLaptopCandidate = 
                lowerTitle.includes('ноутбук') ||
                lowerTitle.includes('laptop') ||
                lowerTitle.includes('macbook') ||
                lowerTitle.includes('ultrabook') ||
                lowerTitle.includes('vivobook') ||
                lowerTitle.includes('ideapad') ||
                lowerTitle.includes('thinkpad') ||
                lowerTitle.includes('zenbook') ||
                lowerTitle.includes('matebook') ||
                lowerTitle.includes('intel') ||
                lowerTitle.includes('ryzen') ||
                lowerTitle.includes('geforce') ||
                lowerTitle.includes('celeron') ||
                lowerTitle.includes('pentium');

            if (!isLaptopCandidate) continue;

            const idMatch = cleanUrl.match(/-([0-9]+)\\/?$/) || cleanUrl.match(/\\/product\\/([0-9]+)/);
            const id = idMatch ? idMatch[1] : '';

            let rating = null;
            const ratingMatch = cardText.match(/(?:^|\\s)([1-5][.,][0-9])(?:\\s|$)/);
            if (ratingMatch) {
                rating = parseFloat(ratingMatch[1].replace(',', '.'));
            }

            let reviewsCount = null;
            const revMatch = cardText.match(/([0-9\\s\\u00a0\\u2009]+)\\s*(?:отзыв|оцен)/i);
            if (revMatch) {
                const parsedRev = parsePrice(revMatch[1]);
                if (parsedRev !== null && parsedRev < 500000) {
                    reviewsCount = parsedRev;
                }
            }

            const discount = priceOriginal > priceCurrent 
                ? Math.round((1 - priceCurrent / priceOriginal) * 100) 
                : 0;

            seenUrls.add(cleanUrl);
            results.push({
                id: id,
                title: title,
                price_current: priceCurrent,
                price_original: priceOriginal >= priceCurrent ? priceOriginal : priceCurrent,
                discount_percent: discount,
                rating: rating,
                reviews_count: reviewsCount,
                url: cleanUrl
            });
        } catch (err) {}
    }

    return results;
}
"""


@dataclass(slots=True, frozen=True)
class ParserConfig:
    ozon_url: str
    max_pages: int
    output_file: str
    headless: bool
    page_load_timeout_ms: int
    scroll_count: int
    scroll_delay_sec: float
    proxy_url: Optional[str]
    browser_channel: str
    user_data_dir: str
    append_mode: bool

    @classmethod
    def from_env(cls) -> "ParserConfig":
        url: str = os.getenv("OZON_URL", "https://www.ozon.ru/category/noutbuki-15690/").strip()
        pages: int = int(os.getenv("MAX_PAGES", "2"))
        output: str = os.getenv("OUTPUT_FILE", "ozon_laptops.json")
        is_headless: bool = os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")
        timeout: int = int(os.getenv("PAGE_LOAD_TIMEOUT_MS", "60000"))
        scrolls: int = int(os.getenv("SCROLL_COUNT", "6"))
        delay: float = float(os.getenv("SCROLL_DELAY_SEC", "1.5"))
        raw_proxy: str = os.getenv("PROXY_URL", "")
        proxy: Optional[str] = raw_proxy if raw_proxy.strip() else None
        channel: str = os.getenv("BROWSER_CHANNEL", "chrome").strip()
        data_dir: str = os.getenv("USER_DATA_DIR", "./ozon_profile").strip()
        is_append: bool = os.getenv("APPEND_MODE", "false").lower() in ("true", "1", "yes")
        return cls(
            ozon_url=url,
            max_pages=pages,
            output_file=output,
            headless=is_headless,
            page_load_timeout_ms=timeout,
            scroll_count=scrolls,
            scroll_delay_sec=delay,
            proxy_url=proxy,
            browser_channel=channel,
            user_data_dir=data_dir,
            append_mode=is_append,
        )


@dataclass(slots=True, frozen=True)
class LaptopItem:
    id: str
    title: str
    price_current: int
    price_original: int
    discount_percent: int
    rating: Optional[float]
    reviews_count: Optional[int]
    url: str


def build_page_url(base_url: str, page_number: int) -> str:
    parsed_url: urllib.parse.ParseResult = urllib.parse.urlparse(base_url)
    query_params: dict[str, list[str]] = urllib.parse.parse_qs(parsed_url.query)
    if page_number > 1:
        query_params["page"] = [str(page_number)]
    else:
        query_params.pop("page", None)
    new_query: str = urllib.parse.urlencode(query_params, doseq=True)
    target_url: str = urllib.parse.urlunparse(parsed_url._replace(query=new_query))
    return target_url


async def handle_error_modal_if_present(page: Page) -> bool:
    try:
        btn_locator: Any = page.locator('button:has-text("Обновить страницу")')
        btn_count: int = await btn_locator.count()
        if btn_count > 0:
            is_vis: bool = await btn_locator.first.is_visible()
            if is_vis:
                logger.info("Обнаружена кнопка 'Обновить страницу'. Повторная попытка...")
                await btn_locator.first.click()
                await asyncio.sleep(4.0)
                return True
    except Exception as click_err:
        logger.warning("Ошибка обработки кнопки обновления: %s", click_err)
    return False


async def ensure_laptop_catalog(page: Page) -> None:
    current_url: str = page.url.lower().rstrip("/")
    if current_url in ("https://www.ozon.ru", "http://www.ozon.ru"):
        logger.info("Браузер находится на главной странице. Выполняется переход в каталог через поиск...")
        try:
            search_input: Any = page.locator('input[name="text"]')
            if await search_input.count() == 0:
                search_input = page.locator('input[placeholder*="Искать"]')
            if await search_input.count() > 0:
                await search_input.first.click()
                await search_input.first.fill("ноутбук")
                await search_input.first.press("Enter")
                await asyncio.sleep(4.0)
        except Exception as search_err:
            logger.warning("Не удалось выполнить поисковый переход: %s", search_err)


async def wait_for_catalog_ready(page: Page, timeout_sec: float) -> bool:
    start_time: float = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start_time) < timeout_sec:
        if page.is_closed():
            return False

        await handle_error_modal_if_present(page)

        catalog_found: bool = await page.evaluate(
            """
            () => {
                const widget = document.querySelector('[data-widget="searchResultsV2"]') 
                    || document.querySelector('[data-widget="megaPaginator"]');
                if (!widget) return false;
                const cards = widget.querySelectorAll('div[data-index]');
                return cards.length > 0;
            }
            """
        )
        if catalog_found:
            logger.info("Сетка каталога ноутбуков успешно обнаружена.")
            return True

        await asyncio.sleep(1.5)

    logger.warning("Ожидание появления каталога ноутбуков завершено.")
    return False


async def scroll_page_natively(page: Page, scroll_count: int, delay_sec: float) -> None:
    scroll_idx: int
    for scroll_idx in range(scroll_count):
        try:
            if page.is_closed():
                break
            await page.mouse.move(500, 400)
            await page.mouse.wheel(0, 750)
            await asyncio.sleep(delay_sec)
        except Exception as scroll_error:
            logger.warning("Ошибка при прокрутке страницы: %s", scroll_error)
            break


async def parse_ozon_page(page: Page, url: str, settings: ParserConfig) -> list[LaptopItem]:
    logger.info("Загрузка страницы каталога: %s", url)
    page_items: list[LaptopItem] = []
    try:
        if page.is_closed():
            logger.error("Страница была закрыта.")
            return page_items

        response: Optional[Response] = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=settings.page_load_timeout_ms,
            referer="https://www.ozon.ru/",
        )
        if response is not None:
            logger.info("Ответ сервера: HTTP %d", response.status)

        await ensure_laptop_catalog(page)
        await wait_for_catalog_ready(page, 25.0)
        await scroll_page_natively(page, settings.scroll_count, settings.scroll_delay_sec)

        if page.is_closed():
            return page_items

        raw_items: list[dict[str, Any]] = await page.evaluate(JS_EXTRACTOR)
        raw: dict[str, Any]
        for raw in raw_items:
            try:
                item: LaptopItem = LaptopItem(
                    id=str(raw.get("id", "")),
                    title=str(raw.get("title", "")),
                    price_current=int(raw.get("price_current", 0)),
                    price_original=int(raw.get("price_original", 0)),
                    discount_percent=int(raw.get("discount_percent", 0)),
                    rating=float(raw["rating"]) if raw.get("rating") is not None else None,
                    reviews_count=int(raw["reviews_count"]) if raw.get("reviews_count") is not None else None,
                    url=str(raw.get("url", "")),
                )
                if item.title and item.price_current > 0 and item.url:
                    page_items.append(item)
            except (ValueError, TypeError) as conv_error:
                logger.debug("Пропуск некорректной записи: %s", conv_error)
                continue

    except Exception as page_error:
        logger.error("Ошибка при обработке страницы %s: %s", url, page_error)

    logger.info("Успешно извлечено ноутбуков со страницы: %d", len(page_items))
    return page_items


async def create_browser_context(playwright: Playwright, settings: ParserConfig) -> BrowserContext:
    profile_path: str = os.path.abspath(settings.user_data_dir)
    os.makedirs(profile_path, exist_ok=True)

    browser_args: list[str] = [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
    ]

    channels_to_attempt: list[Optional[str]] = []
    if settings.browser_channel:
        channels_to_attempt.append(settings.browser_channel)
    if "msedge" not in channels_to_attempt:
        channels_to_attempt.append("msedge")
    channels_to_attempt.append(None)

    last_error: Optional[Exception] = None
    target_channel: Optional[str]
    for target_channel in channels_to_attempt:
        try:
            logger.info("Запуск браузера через канал: %s", target_channel or "bundled_chromium")
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": profile_path,
                "headless": settings.headless,
                "no_viewport": True,
                "ignore_default_args": ["--enable-automation"],
                "args": browser_args,
            }
            if target_channel is not None:
                launch_kwargs["channel"] = target_channel
            if settings.proxy_url:
                launch_kwargs["proxy"] = {"server": settings.proxy_url}

            context: BrowserContext = await playwright.chromium.launch_persistent_context(**launch_kwargs)
            return context
        except Exception as launch_error:
            last_error = launch_error
            logger.warning("Канал %s недоступен: %s", target_channel, launch_error)

    raise RuntimeError(f"Не удалось инициализировать браузерный контекст: {last_error}")


async def run_crawler(settings: ParserConfig) -> list[LaptopItem]:
    all_laptops: list[LaptopItem] = []
    processed_urls: set[str] = set()

    playwright: Playwright
    async with async_playwright() as playwright:
        context: BrowserContext = await create_browser_context(playwright, settings)

        await context.add_init_script(
            """
            if (navigator.webdriver) {
                Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {
                    get: () => undefined
                });
            }
            """
        )

        page: Page = context.pages[0] if context.pages else await context.new_page()

        try:
            page_num: int
            for page_num in range(1, settings.max_pages + 1):
                if page.is_closed():
                    logger.error("Окно браузера было закрыто до окончания работы.")
                    break

                target_url: str = build_page_url(settings.ozon_url, page_num)
                items: list[LaptopItem] = await parse_ozon_page(page, target_url, settings)
                laptop: LaptopItem
                for laptop in items:
                    if laptop.url not in processed_urls:
                        processed_urls.add(laptop.url)
                        all_laptops.append(laptop)

                if page_num < settings.max_pages:
                    await asyncio.sleep(2.5)
        finally:
            if not page.is_closed():
                await context.close()

    return all_laptops


def save_to_json(items: list[LaptopItem], file_path: str, append_mode: bool) -> None:
    existing_items_map: dict[str, dict[str, Any]] = {}

    if append_mode and os.path.exists(file_path):
        try:
            read_handle: TextIO
            with open(file_path, mode="r", encoding="utf-8") as read_handle:
                existing_data: dict[str, Any] = json.load(read_handle)
                raw_items_list: list[dict[str, Any]] = existing_data.get("items", [])
                raw_entry: dict[str, Any]
                for raw_entry in raw_items_list:
                    item_sku: str = str(raw_entry.get("id", ""))
                    if item_sku:
                        existing_items_map[item_sku] = raw_entry
            logger.info("Загружено существующих товаров из файла: %d", len(existing_items_map))
        except Exception as read_err:
            logger.warning("Не удалось прочитать существующий файл для дополнения: %s", read_err)

    new_item: LaptopItem
    for new_item in items:
        existing_items_map[new_item.id] = asdict(new_item)

    final_payload: list[dict[str, Any]] = (
        list(existing_items_map.values())
        if append_mode and existing_items_map
        else [asdict(it) for it in items]
    )

    data_to_save: dict[str, Any] = {
        "metadata": {
            "total_items": len(final_payload),
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "items": final_payload,
    }

    file_handle: TextIO
    with open(file_path, mode="w", encoding="utf-8") as file_handle:
        json.dump(data_to_save, file_handle, ensure_ascii=False, indent=4)

    logger.info("Данные успешно сохранены в: %s (Всего в базе: %d)", file_path, len(final_payload))


async def main() -> None:
    settings: ParserConfig = ParserConfig.from_env()
    logger.info("Старт сбора данных Ozon. Задано страниц: %d", settings.max_pages)
    results: list[LaptopItem] = await run_crawler(settings)
    save_to_json(results, settings.output_file, settings.append_mode)


if __name__ == "__main__":
    asyncio.run(main())