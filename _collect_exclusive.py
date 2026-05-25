"""
[단독] 기사 수집 → DB 저장 + URL 삭제 여부 검사
  소스 1: 네이버 뉴스 모바일 검색  → n.news.naver.com URL
  소스 2: Google News RSS         → 언론사 원본 URL
- 언론사 제한 없이 전체 수집
- is_exclusive=1 마킹, 404 시 is_deleted=1
"""
import asyncio
import aiohttp
import re
import sqlite3
import sys
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

DB_FILE = "C:/Users/admin/naver_monitor.db"
TIMEOUT = 10
UA_PC   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
UA_MOB  = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

NAVER_SEARCH = "https://m.search.naver.com/search.naver?where=m_news&query=%5B%EB%8B%A8%EB%8F%85%5D&sort=1&start={start}"
GOOGLE_RSS   = "https://news.google.com/rss/search?q=%5B%EB%8B%A8%EB%8F%85%5D&hl=ko&gl=KR&ceid=KR:ko"
NAVER_PAGES  = 10
GOOGLE_RESOLVE_CONCURRENCY = 5   # Playwright 브라우저 탭 동시 수


def normalize_naver_url(url: str) -> str | None:
    m = re.search(r"/article/(\d+)/(\d+)", url)
    if m and "naver.com" in url:
        return f"https://n.news.naver.com/mnews/article/{m.group(1)}/{m.group(2)}"
    return None


def load_press_map(conn) -> dict:
    rows = conn.execute("SELECT DISTINCT press_code, press_name FROM articles").fetchall()
    return {code: name for code, name in rows}


def name_to_code(conn) -> dict:
    rows = conn.execute("SELECT DISTINCT press_code, press_name FROM articles").fetchall()
    return {name: code for code, name in rows}


async def check_url_deleted(session, url: str) -> bool:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                               allow_redirects=True, headers={"User-Agent": UA_PC}, ssl=False) as r:
            final = str(r.url)
            return (
                r.status == 404
                or "n.news.naver.com/error" in final
                or final.rstrip("/") == "https://n.news.naver.com"
                or "deletedArticle" in final
            )
    except Exception:
        return False


# ── 소스 1: 네이버 모바일 검색 ──────────────────────────────────────────
async def scrape_naver(page) -> list[dict]:
    collected = []
    seen = set()
    for p in range(NAVER_PAGES):
        start = p * 15 + 1
        try:
            await page.goto(NAVER_SEARCH.format(start=start), wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(700)
        except Exception as e:
            print(f"  [네이버] 오류 (start={start}): {e}")
            break
        html = await page.content()
        data_urls = re.findall(r'data-url=["\']([^"\']+)["\']', html)
        href_urls = re.findall(r'href="([^"]*naver\.com/[^"]*article/\d+/\d+[^"]*)"', html)
        new = 0
        for raw in data_urls + href_urls:
            norm = normalize_naver_url(raw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            collected.append({"url": norm, "title": "", "press": "", "source": "naver"})
            new += 1
        print(f"  [네이버] start={start}: {new}건")
        if len(data_urls) < 5:
            break
    return collected


# ── 소스 2: Google News RSS ──────────────────────────────────────────────
async def fetch_google_rss(session) -> list[dict]:
    try:
        async with session.get(GOOGLE_RSS, timeout=aiohttp.ClientTimeout(total=20),
                               headers={"User-Agent": UA_PC}, ssl=False) as r:
            text = await r.text()
    except Exception as e:
        print(f"  [구글] RSS 오류: {e}")
        return []
    items = []
    try:
        root = ET.fromstring(text)
        for item in root.iter("item"):
            title  = re.sub(r"<[^>]+>", "", item.findtext("title") or "").strip()
            link   = (item.findtext("link") or "").strip()
            source = (item.findtext("source") or "").strip()
            pub    = item.findtext("pubDate") or ""
            try:
                dt = parsedate_to_datetime(pub)
                date_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
            except Exception:
                date_str = ""
            if link:
                items.append({"google_link": link, "title": title,
                              "press": source, "date": date_str})
    except ET.ParseError as e:
        print(f"  [구글] RSS 파싱 오류: {e}")
    return items


async def resolve_google_links(browser, rss_items: list[dict], seen_urls: set) -> list[dict]:
    """Google News RSS 링크 → 실제 언론사 URL (Playwright 병렬)"""
    sem = asyncio.Semaphore(GOOGLE_RESOLVE_CONCURRENCY)
    results = []

    async def resolve_one(item):
        async with sem:
            ctx  = await browser.new_context(user_agent=UA_PC)
            page = await ctx.new_page()
            try:
                await page.goto(item["google_link"], wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1500)
                final_url = page.url

                # v.daum.net은 원문 URL 추가 추출
                if final_url and "v.daum.net" in final_url:
                    try:
                        orig = await page.evaluate("""
                            () => {
                                // og:url meta tag
                                const og = document.querySelector('meta[property="og:url"]');
                                if (og) return og.content;
                                // 원문보기 링크
                                const a = document.querySelector('a.link_view, a.btn_originlink, a[data-original-url]');
                                if (a) return a.href || a.dataset.originalUrl;
                                return '';
                            }
                        """)
                        if orig and "daum.net" not in orig and orig.startswith("http"):
                            final_url = orig
                    except Exception:
                        pass
            except Exception:
                final_url = ""
            finally:
                await ctx.close()

        if not final_url or "google.com" in final_url:
            return
        if final_url in seen_urls:
            return
        seen_urls.add(final_url)
        results.append({
            "url":    final_url,
            "title":  item["title"],
            "press":  item["press"],
            "date":   item.get("date", ""),
            "source": "google",
        })

    await asyncio.gather(*[resolve_one(i) for i in rss_items])
    print(f"  [구글] RSS {len(rss_items)}건 → 실제 URL {len(results)}건")
    return results


# ── 공통: DB 저장 ─────────────────────────────────────────────────────────
async def save_candidates(conn, session, candidates: list[dict], oid_map: dict, n2c: dict):
    inserted = skipped = deleted_new = 0
    sem = asyncio.Semaphore(15)

    async def process(item):
        nonlocal inserted, skipped, deleted_new
        url    = item["url"]
        title  = item.get("title", "")
        press  = item.get("press", "")
        date   = item.get("date", datetime.now().strftime("%Y-%m-%d"))
        source = item.get("source", "")

        # 네이버 URL이면 OID에서 press_name 보정
        if "naver.com" in url:
            m = re.search(r"/article/(\d+)/", url)
            oid = m.group(1) if m else ""
            press_code = oid
            press_name = oid_map.get(oid) or press or f"oid:{oid}"
        else:
            # 언론사 원본 URL: press 이름으로 코드 매핑
            press_name = press
            press_code = n2c.get(press, "")

        async with sem:
            is_del = await check_url_deleted(session, url)

        is_deleted = 1 if is_del else 0
        cur = conn.execute("""
            INSERT OR IGNORE INTO articles
              (url, press_code, press_name, title, article_date,
               first_seen, checks_json, score, is_exclusive, is_deleted)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (url, press_code, press_name, title, date,
              datetime.now().isoformat(), "{}", 0.0, 1, is_deleted))
        if cur.rowcount:
            inserted += 1
            if is_deleted:
                deleted_new += 1
            flag = "❌삭제" if is_deleted else "✓신규"
        else:
            conn.execute("UPDATE articles SET is_exclusive=1 WHERE url=?", (url,))
            if is_deleted:
                conn.execute("UPDATE articles SET is_deleted=1 WHERE url=?", (url,))
                deleted_new += 1
            skipped += 1
            flag = f"기존({'❌삭제' if is_deleted else '✓'})"
        src_label = "[구글]" if source == "google" else "[네이버]"
        print(f"  {src_label} [{press_name}] {(title or url)[:45]} → {flag}")

    await asyncio.gather(*[process(c) for c in candidates])
    conn.commit()
    return inserted, skipped, deleted_new


async def main():
    conn = sqlite3.connect(DB_FILE)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    if "is_exclusive" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN is_exclusive INTEGER DEFAULT 0")
    conn.execute("UPDATE articles SET is_exclusive=1 WHERE title LIKE '%[단독]%' AND is_exclusive=0")
    conn.commit()

    oid_map = load_press_map(conn)
    n2c     = name_to_code(conn)

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # ── 소스 1: 네이버 모바일 ──
        print("=" * 50)
        print("소스 1: 네이버 뉴스 모바일 [단독] 검색")
        ctx_mob = await browser.new_context(user_agent=UA_MOB)
        pg_mob  = await ctx_mob.new_page()
        naver_candidates = await scrape_naver(pg_mob)
        await ctx_mob.close()
        seen_urls = {c["url"] for c in naver_candidates}

        # ── 소스 2: Google News RSS ──
        print()
        print("=" * 50)
        print("소스 2: Google News RSS [단독] 수집")
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            rss_items = await fetch_google_rss(session)
            print(f"  RSS 수신: {len(rss_items)}건 → URL 변환 중...")
            google_candidates = await resolve_google_links(browser, rss_items, seen_urls)

            all_candidates = naver_candidates + google_candidates
            print()
            print("=" * 50)
            print(f"전체 수집: {len(all_candidates)}건 (네이버 {len(naver_candidates)} + 구글 {len(google_candidates)})")
            print("URL 검사 + DB 저장 중...")
            inserted, skipped, deleted_new = await save_candidates(
                conn, session, all_candidates, oid_map, n2c
            )

        await browser.close()

    total_ex     = conn.execute("SELECT COUNT(*) FROM articles WHERE is_exclusive=1").fetchone()[0]
    total_ex_del = conn.execute("SELECT COUNT(*) FROM articles WHERE is_exclusive=1 AND is_deleted=1").fetchone()[0]
    conn.close()

    print(f"""
완료
  신규 삽입: {inserted}건  (삭제 확인: {deleted_new}건)
  기존 is_exclusive 업데이트: {skipped}건
  DB 전체 단독 기사: {total_ex}건
  단독 기사 중 삭제 의심: {total_ex_del}건
""")


if __name__ == "__main__":
    asyncio.run(main())
