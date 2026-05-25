"""v.daum.net URL → 실제 언론사 원문 URL 추출 및 DB 업데이트

Daum 뷰어 페이지에서 og:url 또는 원문 링크를 추출해 source_url에 저장.
살아있는 기사만 처리 가능 (404인 경우 건너뜀).
"""
import asyncio, aiohttp, sqlite3, re, sys, io
from urllib.parse import urlparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_FILE     = "C:/Users/admin/naver_monitor.db"
CONCURRENCY = 10
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def extract_original_from_html(html: str) -> str:
    """Daum 뷰어 HTML에서 원문 URL 추출"""
    # og:url (daum이 아닌 경우)
    m = re.search(r'property="og:url"[^>]+content="([^"]+)"', html)
    if m and "daum.net" not in m.group(1):
        return m.group(1)
    m = re.search(r'content="([^"]+)"[^>]+property="og:url"', html)
    if m and "daum.net" not in m.group(1):
        return m.group(1)

    # JSON에서 linkUrl / originalUrl / sourceUrl
    for key in ("linkUrl", "originalUrl", "sourceUrl", "contentUrl"):
        m = re.search(rf'"{key}"\s*:\s*"(https?://[^"]+)"', html)
        if m and "daum.net" not in m.group(1):
            return m.group(1)

    # 첫 번째 외부 언론사 article href (cp=du 파라미터 포함 = Daum 추적 링크)
    hrefs = re.findall(r'href="(https?://(?!(?:[^"]*\.)?daum)[^"]{15,})"', html)
    for href in hrefs:
        # 도메인만 있는 링크 제외, 경로가 있는 링크 선택
        parsed = urlparse(href)
        if parsed.path and parsed.path != "/" and len(parsed.path) > 3:
            return href.split("&cp=")[0]  # Daum 추적 파라미터 제거

    return ""


async def resolve_one(session, sem, url: str) -> str:
    async with sem:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=12),
                allow_redirects=True,
                headers={"User-Agent": UA},
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return ""
                html = await resp.text(errors="replace")
                return extract_original_from_html(html)
        except Exception:
            return ""


async def main():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("""
        SELECT url FROM articles
        WHERE url LIKE '%v.daum.net%'
          AND (source_url IS NULL OR source_url = '')
        ORDER BY article_date DESC
    """).fetchall()

    if not rows:
        print("처리 대상 없음")
        conn.close()
        return

    urls = [r[0] for r in rows]
    print(f"v.daum.net 기사 {len(urls)}건 원문 추출 중...")

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False)
    updated = skipped = failed = 0

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [(url, asyncio.create_task(resolve_one(session, sem, url))) for url in urls]
        for url, task in tasks:
            orig = await task
            if orig:
                conn.execute(
                    "UPDATE articles SET source_url=? WHERE url=?",
                    (orig, url)
                )
                updated += 1
                domain = urlparse(orig).netloc
                print(f"  ✓ {domain}  ← {url[-30:]}")
            else:
                failed += 1

    conn.commit()
    conn.close()
    print(f"\n완료: 원문 추출 {updated}건 / 실패(404등) {failed}건")


if __name__ == "__main__":
    asyncio.run(main())
