"""출처 미확인(delete_type IS NULL or 3) 기사를 3단계로 재분류

단계:
  1. 네이버 검색 → 나오면 delete_type=5 (링크 삭제 — URL만 죽고 Naver에 존재)
  2. 언론사 도메인 파악 후 Google RSS site:도메인 검색 → 나오면 delete_type=1 (네이버만 삭제)
  3. 둘 다 없으면 delete_type=2 (완전 삭제)

도메인 파악 순서:
  ① DB의 같은 press_code 기사에서 source_url 추출
  ② 없으면 Naver 검색 API로 해당 언론사 최근 기사 originallink에서 추출
"""
import asyncio, aiohttp, sqlite3, re, sys, io, os
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse
from dotenv import load_dotenv

load_dotenv("C:/Users/admin/.env")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_FILE = "C:/Users/admin/naver_monitor.db"
TAG_RE  = re.compile(r"<[^>]+>")


def get_domain_from_db(conn, press_code: str) -> str:
    """같은 press_code 기사 중 source_url이 있는 것에서 도메인 추출"""
    row = conn.execute(
        "SELECT source_url FROM articles "
        "WHERE press_code=? AND source_url IS NOT NULL AND source_url != '' LIMIT 1",
        (press_code,)
    ).fetchone()
    if row:
        parsed = urlparse(row[0])
        return parsed.netloc  # e.g. "www.bloter.net"
    return ""


async def get_domain_from_naver(session, press_name: str, client_id: str, client_secret: str) -> str:
    """Naver 검색으로 해당 언론사 최근 기사 originallink에서 도메인 추출"""
    try:
        async with session.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": press_name, "display": 10, "sort": "date"},
            headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
            for item in data.get("items", []):
                orig = item.get("originallink", "")
                if not orig or not orig.startswith("http"):
                    continue
                netloc = urlparse(orig).netloc
                if netloc and "naver.com" not in netloc:
                    return netloc
    except Exception:
        pass
    return ""


async def search_naver(session, title: str, press: str, client_id: str, client_secret: str) -> bool:
    """제목으로 네이버 뉴스 검색 — 있으면 True"""
    try:
        async with session.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": title[:40], "display": 5, "sort": "date"},
            headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
            timeout=aiohttp.ClientTimeout(total=6),
        ) as resp:
            if resp.status != 200:
                return False
            data = await resp.json()
            t_clean = TAG_RE.sub("", title).strip()
            for item in data.get("items", []):
                i_title = TAG_RE.sub("", item.get("title", "")).strip()
                i_link  = item.get("originallink", "") + item.get("link", "")
                if t_clean[:15] in i_title or press in i_link:
                    return True
    except Exception:
        pass
    return False


async def search_outlet_direct(session, title: str, domain: str) -> bool:
    """Google RSS로 site:도메인 검색 — 언론사 직접 확인"""
    query = f'site:{domain} "{title[:30]}"'
    url   = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "Mozilla/5.0"},
            ssl=False,
        ) as resp:
            if resp.status != 200:
                return False
            text = await resp.text()
            root = ET.fromstring(text)
            t_clean = title[:15]
            for item in root.iter("item"):
                i_title = item.findtext("title") or ""
                i_link  = item.findtext("link") or ""
                if t_clean in i_title or domain in i_link:
                    return True
    except Exception:
        pass
    return False


async def search_google_rss_fallback(session, title: str, press: str) -> bool:
    """도메인 파악 실패 시 제목만으로 Google RSS 검색"""
    query = f'"{title[:30]}"'
    url   = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "Mozilla/5.0"},
            ssl=False,
        ) as resp:
            if resp.status != 200:
                return False
            text = await resp.text()
            root = ET.fromstring(text)
            t_clean = title[:20]
            for item in root.iter("item"):
                i_title = item.findtext("title") or ""
                i_src   = item.findtext("source") or ""
                if t_clean in i_title or press in i_src:
                    return True
    except Exception:
        pass
    return False


async def main():
    client_id     = os.getenv("NAVER_SEARCH_CLIENT_ID")
    client_secret = os.getenv("NAVER_SEARCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("네이버 검색 API 키 없음")
        return

    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT url, press_name, press_code, title FROM articles "
        "WHERE is_deleted=1 AND (delete_type IS NULL OR delete_type=3) "
        "ORDER BY article_date DESC"
    ).fetchall()

    if not rows:
        print("재분류 대상 없음")
        conn.close()
        return

    print(f"재분류 대상 {len(rows)}건 — 3단계 검색 시작...")
    print(f"  1단계: 네이버 검색      → 링크삭제(5)")
    print(f"  2단계: 언론사 직접 확인 → 네이버만삭제(1)")
    print(f"  3단계: 없으면          → 완전삭제(2)\n")

    # press_code별 도메인 캐시
    domain_cache: dict[str, str] = {}

    counts = {5: 0, 1: 0, 2: 0}

    async with aiohttp.ClientSession() as session:
        for url, press, press_code, title in rows:
            if not title:
                conn.execute("UPDATE articles SET delete_type=2 WHERE url=?", (url,))
                counts[2] += 1
                continue

            # ── 1단계: 네이버 검색 ──────────────────────────────────────────
            on_naver = await search_naver(session, title, press, client_id, client_secret)
            await asyncio.sleep(0.12)

            if on_naver:
                conn.execute("UPDATE articles SET delete_type=5 WHERE url=?", (url,))
                counts[5] += 1
                print(f"  [링크삭제]  [{press}] {title[:45]}")
                continue

            # ── 2단계: 언론사 도메인 파악 후 직접 확인 ──────────────────────
            # 도메인 캐시 확인
            if press_code not in domain_cache:
                domain = get_domain_from_db(conn, press_code)
                if not domain:
                    domain = await get_domain_from_naver(session, press, client_id, client_secret)
                    await asyncio.sleep(0.12)
                domain_cache[press_code] = domain

            domain = domain_cache[press_code]

            if domain:
                on_outlet = await search_outlet_direct(session, title, domain)
                method = f"site:{domain}"
            else:
                on_outlet = await search_google_rss_fallback(session, title, press)
                method = "Google RSS(도메인미확인)"
            await asyncio.sleep(0.3)

            if on_outlet:
                conn.execute("UPDATE articles SET delete_type=1 WHERE url=?", (url,))
                counts[1] += 1
                print(f"  [네이버만]  [{press}] {title[:45]}  ({method})")
            else:
                conn.execute("UPDATE articles SET delete_type=2 WHERE url=?", (url,))
                counts[2] += 1
                print(f"  [완전삭제]  [{press}] {title[:45]}  ({method})")

    conn.commit()
    conn.close()

    total = len(rows)
    print(f"\n완료 ({total}건):")
    print(f"  링크 삭제    : {counts[5]}건")
    print(f"  네이버만 삭제 : {counts[1]}건")
    print(f"  완전 삭제    : {counts[2]}건")


if __name__ == "__main__":
    asyncio.run(main())
