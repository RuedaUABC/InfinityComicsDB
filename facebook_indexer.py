#!/usr/bin/env python3
"""Indexa publicaciones de una página de Facebook y sus enlaces MEGA/MediaFire.

El script solo lee el DOM visible de la página y guarda metadatos en SQLite.
No abre, visita ni descarga los enlaces externos que encuentra.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# Keep Playwright's browser installation beside this script so the service does
# not depend on a browser cached under a particular user's home directory.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(Path(__file__).resolve().parent / ".playwright-browsers"),
)

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    async_playwright,
)


DEFAULT_URL = "https://www.facebook.com/Allnewinfinity2.0/"
DEFAULT_PAGE_NAME = "All New Infinity"
DEFAULT_WHITELIST = ["Batman", "Spider-Man"]
TARGET_DOMAINS = ("mega.nz", "mediafire.com")
POST_SELECTOR = '[role="article"]'

POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_key        TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    page_url        TEXT NOT NULL,
    post_url        TEXT NOT NULL DEFAULT '',
    post_date_text  TEXT NOT NULL DEFAULT '',
    raw_text        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL CHECK (
        status IN ('pendiente', 'ignorado_whitelist', 'error')
    ),
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS post_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    host        TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    UNIQUE(post_id, url)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    posts_seen      INTEGER NOT NULL DEFAULT 0,
    links_found     INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT
);
"""


ARTICLE_EXTRACTION_JS = r"""(article) => {
    const anchors = Array.from(article.querySelectorAll('a[href]')).map((a) => ({
        href: a.href,
        text: (a.innerText || a.getAttribute('aria-label') || '').trim()
    }));
    const permalinkRank = (href) => {
        try {
            const u = new URL(href);
            const p = u.pathname.toLowerCase();
            if (p.includes('/posts/')) return 0;
            if (p.endsWith('/permalink.php')) return 1;
            if (p.includes('/photo/')) return 2;
            if (p.includes('/videos/')) return 3;
            if (p.includes('/photos/')) return 4;
        } catch (_) {}
        return 99;
    };
    const postLinks = anchors
        .filter((a) => permalinkRank(a.href) < 99)
        .sort((a, b) => permalinkRank(a.href) - permalinkRank(b.href));

    // Algunas versiones de Facebook incluyen el ID de publicación en data-ft.
    let dataPostId = '';
    for (const node of [article, ...article.querySelectorAll('[data-ft]')]) {
        const raw = node.getAttribute && node.getAttribute('data-ft');
        if (!raw) continue;
        try {
            const data = JSON.parse(raw);
            dataPostId = data.top_level_post_id || data.post_id || '';
            if (dataPostId) break;
        } catch (_) {}
    }

    return {
        text: article.innerText || '',
        anchors,
        postUrl: postLinks.length ? postLinks[0].href : '',
        postDateText: postLinks.length ? postLinks[0].text : '',
        dataPostId: String(dataPostId || '')
    };
}"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(POSTS_SCHEMA)
    return db


def normalize_line(value: str) -> str:
    value = value.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", value).strip()


def extract_title(raw_text: str, page_name: str) -> str:
    """Elige una línea probable de título; conserva un fallback útil."""
    lines = [normalize_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]

    issue_pattern = re.compile(r"(?:#\s*\d+|\b(?:vol(?:ume)?|no\.?|n[uú]m\.?)\s*\d+)", re.I)
    noise_pattern = re.compile(
        r"^(?:me gusta|comentar|compartir|ver m[aá]s|ver menos|"
        r"enlaces?:?|traductor:|maqueta:|corrector:|responder|"
        r"publicaci[oó]n|contenido patrocinado)\b",
        re.I,
    )

    candidates: list[str] = []
    page_name_folded = page_name.casefold().strip()
    for line in lines:
        lowered = line.casefold()
        if noise_pattern.search(line):
            continue
        if page_name_folded and lowered == page_name_folded:
            continue
        if page_name_folded and lowered.startswith(page_name_folded) and any(
            marker in lowered
            for marker in ("agregó una foto", "agregó una publicación", "added a new photo", "shared a link")
        ):
            continue
        if lowered.startswith(("compartido con:", "shared with:", "público", "public")):
            continue
        if len(line) < 4 or len(line) > 180:
            continue
        if lowered.startswith(("http://", "https://")):
            continue
        if re.fullmatch(r"[\d\s.:/,·-]+", line):
            continue
        candidates.append(line)

    # Issue numbers usually identify the comic more precisely than its blurb.
    for candidate in candidates:
        if issue_pattern.search(candidate):
            return candidate

    # Facebook's page name and post activity text often precede the post body.
    for candidate in candidates:
        lowered = candidate.casefold()
        if "agregó una foto" in lowered or "added a new photo" in lowered:
            continue
        if re.search(r"\b(?:hace|ayer|domingo|lunes|martes|miércoles|jueves|viernes|sábado)\b", lowered):
            continue
        return candidate

    return candidates[0] if candidates else "Publicación sin título detectado"


def unwrap_redirect(url: str) -> str:
    """Desenvuelve redirecciones habituales de Facebook sin hacer peticiones."""
    candidate = url.strip().rstrip(".,);]")
    for _ in range(3):
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if host not in {"l.facebook.com", "lm.facebook.com"}:
            break
        query = parse_qs(parsed.query)
        redirected = (query.get("u") or query.get("url") or [""])[0]
        if not redirected:
            break
        candidate = unquote(redirected)
    return candidate


def target_host(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for domain in TARGET_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return host
    return None


def canonicalize_target_url(url: str) -> tuple[str, str] | None:
    url = unwrap_redirect(url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = target_host(url)
    if not host:
        return None

    # Remove Facebook tracking parameters only; retain provider parameters and
    # fragments because they may contain the access key for a shared link.
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.casefold() not in {"fbclid", "__tn__", "__cft__"}]
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                            urlencode(query), parsed.fragment))
    return clean_url, host


def external_links(anchors: list[dict[str, str]]) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for anchor in anchors:
        url = anchor.get("href", "")
        result = canonicalize_target_url(url)
        if result:
            clean_url, host = result
            found[clean_url] = host
    return sorted(found.items())


def make_post_key(post_url: str, data_post_id: str, title: str,
                  links: list[tuple[str, str]], raw_text: str) -> str:
    identity = data_post_id or post_url
    if not identity:
        # Fallback for markup where Facebook omits both a permalink and post ID.
        identity = "\n".join([title, *(url for url, _ in links), raw_text[:1000]])
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()


def is_whitelisted(title: str, whitelist: list[str]) -> bool:
    if not whitelist:
        return True
    folded = title.casefold()
    return any(term.casefold() in folded for term in whitelist)


def save_post(
    db: sqlite3.Connection,
    *,
    page_url: str,
    post_url: str,
    post_date_text: str,
    raw_text: str,
    title: str,
    post_key: str,
    links: list[tuple[str, str]],
    whitelist: list[str],
) -> None:
    now = utc_now()
    status = "pendiente" if is_whitelisted(title, whitelist) else "ignorado_whitelist"

    with db:
        db.execute(
            """INSERT INTO posts
                   (post_key, title, page_url, post_url, post_date_text,
                    raw_text, status, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_key) DO UPDATE SET
                   title = excluded.title,
                   page_url = excluded.page_url,
                   post_url = CASE WHEN excluded.post_url != ''
                                   THEN excluded.post_url ELSE posts.post_url END,
                   post_date_text = CASE WHEN excluded.post_date_text != ''
                                         THEN excluded.post_date_text
                                         ELSE posts.post_date_text END,
                   raw_text = excluded.raw_text,
                   status = excluded.status,
                   last_seen = excluded.last_seen,
                   error_message = NULL""",
            (post_key, title, page_url, post_url, post_date_text, raw_text,
             status, now, now),
        )
        post_id = db.execute(
            "SELECT id FROM posts WHERE post_key = ?", (post_key,)
        ).fetchone()["id"]
        for url, host in links:
            db.execute(
                """INSERT OR IGNORE INTO post_links(post_id, url, host, first_seen)
                   VALUES (?, ?, ?, ?)""",
                (post_id, url, host, now),
            )


async def scan_page(
    context: BrowserContext,
    page_url: str,
    page_name: str,
    scroll_rounds: int,
    scroll_delay: float,
    db: sqlite3.Connection,
    whitelist: list[str],
) -> tuple[int, int]:
    page = await context.new_page()
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
        await page.locator(POST_SELECTOR).first.wait_for(state="visible", timeout=30_000)

        for _ in range(max(0, scroll_rounds)):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(max(0, int(scroll_delay * 1000)))

        articles = await page.locator(POST_SELECTOR).evaluate_all(
            f"(nodes) => nodes.map((node) => ({ARTICLE_EXTRACTION_JS})(node))"
        )

        posts_saved = 0
        links_saved = 0
        for article in articles:
            raw_text = article.get("text", "")
            links = external_links(article.get("anchors", []))
            if not links:
                continue

            title = extract_title(raw_text, page_name)
            post_url = article.get("postUrl", "")
            post_key = make_post_key(
                post_url,
                article.get("dataPostId", ""),
                title,
                links,
                raw_text,
            )
            save_post(
                db,
                page_url=page_url,
                post_url=post_url,
                post_date_text=article.get("postDateText", ""),
                raw_text=raw_text,
                title=title,
                post_key=post_key,
                links=links,
                whitelist=whitelist,
            )
            posts_saved += 1
            links_saved += len(links)

        return posts_saved, links_saved
    finally:
        await page.close()


def record_scan_run(
    db: sqlite3.Connection,
    started_at: str,
    status: str,
    posts_seen: int,
    links_found: int,
    error_message: str | None,
) -> None:
    with db:
        db.execute(
            """INSERT INTO scan_runs
                   (started_at, finished_at, status, posts_seen, links_found, error_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (started_at, utc_now(), status, posts_seen, links_found, error_message),
        )


async def run(args: argparse.Namespace) -> None:
    db = open_database(args.database.expanduser().resolve())
    whitelist = args.whitelist if args.whitelist is not None else DEFAULT_WHITELIST
    storage_state = args.storage_state.expanduser().resolve() if args.storage_state else None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context_options: dict[str, object] = {
                "locale": "es-MX",
                "viewport": {"width": 1440, "height": 1000},
            }
            if storage_state:
                if not storage_state.is_file():
                    raise FileNotFoundError(f"No existe storage state: {storage_state}")
                context_options["storage_state"] = str(storage_state)

            context = await browser.new_context(**context_options)
            try:
                logging.info("Indexando %s cada %s minutos", args.url, args.interval_minutes)
                logging.info("Whitelist: %s", ", ".join(whitelist) if whitelist else "desactivada")

                while True:
                    started_at = utc_now()
                    try:
                        post_count, link_count = await scan_page(
                            context=context,
                            page_url=args.url,
                            page_name=args.page_name,
                            scroll_rounds=args.scroll_rounds,
                            scroll_delay=args.scroll_delay,
                            db=db,
                            whitelist=whitelist,
                        )
                        record_scan_run(db, started_at, "ok", post_count, link_count, None)
                        logging.info("Revisión terminada: %s publicaciones, %s enlaces", post_count, link_count)
                    except (PlaywrightError, TimeoutError, sqlite3.Error, ValueError) as exc:
                        record_scan_run(db, started_at, "error", 0, 0, str(exc)[:1000])
                        logging.exception("Falló la revisión de Facebook")
                    except Exception as exc:
                        record_scan_run(db, started_at, "error", 0, 0, str(exc)[:1000])
                        logging.exception("Error inesperado durante la revisión")

                    await asyncio.sleep(max(1, args.interval_minutes) * 60)
            finally:
                await context.close()
                await browser.close()
    finally:
        db.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Registra publicaciones de Facebook con enlaces MEGA/MediaFire en SQLite."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="URL de la página objetivo")
    parser.add_argument("--page-name", default=DEFAULT_PAGE_NAME,
                        help="Nombre visible de la página, para omitirlo al detectar el título")
    parser.add_argument("--database", type=Path, default=Path("comics_index.db"))
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--scroll-rounds", type=int, default=8,
                        help="Cuántas veces bajar para cargar publicaciones anteriores")
    parser.add_argument("--scroll-delay", type=float, default=2.0,
                        help="Pausa en segundos entre desplazamientos")
    parser.add_argument("--storage-state", type=Path,
                        help="JSON de sesión Playwright opcional; no se solicitan credenciales")
    parser.add_argument("--whitelist", nargs="*", default=None,
                        help="Títulos permitidos; si se omite usa Batman y Spider-Man")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run(arguments))
    except KeyboardInterrupt:
        logging.info("Demonio detenido por el usuario")
