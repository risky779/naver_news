"""DB의 기사 URL을 비동기로 검사해 삭제 유형을 분류

delete_type:
  1 = 네이버만 삭제 (Google 검색 시 언론사 원문 생존)
  2 = 완전 삭제 (Naver + 언론사 모두 없음)
  3 = 출처 미확인 (임시, resolve_unknown_by_search 실행 전)
  4 = 언론사 직접 삭제 (비제휴 언론사 URL 404)
  5 = 링크 삭제 (URL만 죽었고 Naver 검색에서 기사 여전히 존재)
"""
import asyncio, aiohttp, sqlite3, csv, sys, io, re, os
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime
from dotenv import load_dotenv
load_dotenv("C:/Users/admin/.env")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

DB_FILE     = "C:/Users/admin/naver_monitor.db"
OUT_FILE    = "deleted_articles.csv"
CONCURRENCY = 30
TIMEOUT     = 10
HEADERS     = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

TAG_RE    = re.compile(r"<[^>]+>")
ORIGIN_RE = re.compile(
    r'(?:href="(https?://[^"]+)"[^>]*class="media_end_head_origin_link"'
    r'|class="media_end_head_origin_link"[^>]*href="(https?://[^"]+)")'
)


def ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "source_url" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN source_url TEXT")
    if "delete_type" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN delete_type INTEGER")
    conn.commit()


def is_naver_gone(status, final_url):
    return (
        status == 404
        or "n.news.naver.com/error" in final_url
        or final_url.rstrip("/") == "https://n.news.naver.com"
        or "deletedArticle" in final_url
    )


def extract_origin_url(data: bytes) -> str:
    html = data.decode("utf-8", errors="replace")
    m = ORIGIN_RE.search(html)
    if m:
        url = m.group(1) or m.group(2)
        if url and "naver.com" not in url:
            return url
    return ""


async def fetch_status(session, sem, url) -> int:
    async with sem:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                allow_redirects=True,
            ) as resp:
                return resp.status
        except Exception:
            return -1


async def check_article(session, sem, url, press, title, date, is_partner, stored_source_url):
    """
    Returns: (url, press, title, date, http_status, final_url,
              is_deleted, delete_type, new_source_url)
    """
    status = -1
    final_url = ""
    new_source_url = stored_source_url
    is_gone = False

    # Phase 1: 기본 URL 체크
    async with sem:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                allow_redirects=True,
            ) as resp:
                status = resp.status
                final_url = str(resp.url)

                if is_partner:
                    is_gone = is_naver_gone(status, final_url)
                    # 살아있는 Naver 기사에서 source_url 추출 (아직 없는 경우만)
                    if not is_gone and not stored_source_url and status == 200:
                        try:
                            chunk = await resp.content.read(20480)
                            extracted = extract_origin_url(chunk)
                            if extracted:
                                new_source_url = extracted
                        except Exception:
                            pass
                else:
                    is_gone = (status == 404)
        except Exception as e:
            final_url = str(e)[:80]

    # Phase 2: 삭제된 경우 delete_type 결정 (sem 밖에서 실행해 데드락 방지)
    delete_type = None
    if is_gone:
        if not is_partner:
            delete_type = 4
        elif new_source_url:
            src_status = await fetch_status(session, sem, new_source_url)
            delete_type = 1 if src_status == 200 else 2
        else:
            delete_type = 3

    return (url, press, title, date, status, final_url, is_gone, delete_type, new_source_url)


async def main():
    conn = sqlite3.connect(DB_FILE)
    ensure_columns(conn)

    rows = conn.execute(
        "SELECT url, press_name, title, article_date, "
        "COALESCE(is_naver_partner, 1), source_url "
        "FROM articles ORDER BY press_name"
    ).fetchall()
    conn.close()

    total = len(rows)
    print(f"검사 대상: {total}건 (동시 {CONCURRENCY}개)")

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY * 2, ssl=False)

    async with aiohttp.ClientSession(headers=HEADERS, connector=connector) as session:
        tasks = [
            check_article(session, sem, r[0], r[1], r[2], r[3], bool(r[4]), r[5] or "")
            for r in rows
        ]
        results = []
        done = 0
        for coro in asyncio.as_completed(tasks):
            res = await coro
            results.append(res)
            done += 1
            if done % 500 == 0 or done == total:
                deleted_so_far = sum(1 for r in results if r[6])
                print(f"  {done}/{total} 완료 — 삭제 의심 {deleted_so_far}건")

    deleted = [r for r in results if r[6]]
    deleted.sort(key=lambda r: (r[1], r[0]))

    # DB 업데이트
    conn = sqlite3.connect(DB_FILE)
    for r in results:
        url, _, _, _, status, _, is_gone, delete_type, new_source_url = r
        if is_gone:
            conn.execute(
                "UPDATE articles SET is_deleted=1, delete_type=? WHERE url=?",
                (delete_type, url),
            )
        elif status == 200:
            conn.execute(
                "UPDATE articles SET is_deleted=0, delete_type=NULL WHERE url=?",
                (url,),
            )
        if new_source_url:
            conn.execute(
                "UPDATE articles SET source_url=? WHERE url=? AND (source_url IS NULL OR source_url='')",
                (new_source_url, url),
            )
    conn.commit()

    alive_cnt   = sum(1 for r in results if not r[6] and r[4] == 200)
    src_updated = sum(1 for r in results if r[8] and not r[5])
    print(f"  DB 업데이트 완료 — 삭제 {len(deleted)}건 / 정상 {alive_cnt}건 / source_url 신규 {src_updated}건")
    conn.close()

    # 타입별 집계
    by_type = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for r in deleted:
        t = r[7]
        if t in by_type:
            by_type[t] += 1

    print(f"\n완료 — 삭제 의심: {len(deleted)}건 / 전체 {total}건")
    print(f"  링크삭제={by_type[5]} / 네이버만={by_type[1]} / 완전삭제={by_type[2]} / 미확인={by_type[3]} / 언론사직접={by_type[4]}")

    type_labels = {1: "네이버만삭제", 2: "완전삭제", 3: "출처미확인", 4: "언론사삭제", 5: "링크삭제"}
    with open(OUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["url", "언론사", "제목", "기사날짜", "HTTP상태", "최종URL", "삭제유형"])
        for r in deleted:
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], type_labels.get(r[7], "?")])
    print(f"저장: {OUT_FILE}")

    from collections import Counter
    by_press = Counter(r[1] for r in deleted)
    print("\n언론사별 삭제 건수 (상위 15):")
    for press, cnt in by_press.most_common(15):
        print(f"  {press:15s} {cnt}건")

    # 출처 미확인 기사 3단계 검색으로 재분류
    await resolve_unknown_by_search()


async def resolve_unknown_by_search():
    """delete_type IS NULL or 3인 기사를 3단계 검색으로 재분류

    1단계: 네이버 검색 → 있으면 type=5 (링크만 삭제)
    2단계: Google RSS  → 있으면 type=1 (네이버만 삭제)
    3단계: 없으면      → type=2 (완전 삭제)
    """
    client_id     = os.getenv("NAVER_SEARCH_CLIENT_ID")
    client_secret = os.getenv("NAVER_SEARCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("\n네이버 검색 API 키 없음 — 재분류 건너뜀")
        return

    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT url, press_name, title FROM articles "
        "WHERE is_deleted=1 AND (delete_type IS NULL OR delete_type=3) "
        "ORDER BY article_date DESC"
    ).fetchall()
    conn.close()

    if not rows:
        return

    print(f"\n출처 미확인 {len(rows)}건 — 3단계 검색 재분류 중...")
    counts = {5: 0, 1: 0, 2: 0}
    conn = sqlite3.connect(DB_FILE)

    async with aiohttp.ClientSession() as session:
        for url, press, title in rows:
            if not title:
                conn.execute("UPDATE articles SET delete_type=2 WHERE url=?", (url,))
                counts[2] += 1
                continue

            # 1단계: 네이버 검색
            on_naver = await _search_naver(session, title, press, client_id, client_secret)
            await asyncio.sleep(0.12)

            if on_naver:
                conn.execute("UPDATE articles SET delete_type=5 WHERE url=?", (url,))
                counts[5] += 1
                print(f"  [링크삭제] [{press}] {title[:45]}")
                continue

            # 2단계: Google News RSS
            on_google = await _search_google_rss(session, title, press)
            await asyncio.sleep(0.3)

            if on_google:
                conn.execute("UPDATE articles SET delete_type=1 WHERE url=?", (url,))
                counts[1] += 1
                print(f"  [네이버만] [{press}] {title[:45]}")
            else:
                conn.execute("UPDATE articles SET delete_type=2 WHERE url=?", (url,))
                counts[2] += 1
                print(f"  [완전삭제] [{press}] {title[:45]}")

    conn.commit()
    conn.close()
    print(f"  → 링크삭제={counts[5]} / 네이버만={counts[1]} / 완전삭제={counts[2]}")


async def _search_naver(session, title, press, client_id, client_secret) -> bool:
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


async def _search_google_rss(session, title, press) -> bool:
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


if __name__ == "__main__":
    asyncio.run(main())
