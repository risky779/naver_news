"""
비제휴 언론사 기사 수집기
  Google News RSS (site:domain) → Playwright URL 추출 → trafilatura 본문 스크래핑 → DB 저장
"""
from pathlib import Path
import asyncio
import sys
import io
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import aiohttp
import trafilatura
from playwright.async_api import async_playwright

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

DB_FILE        = str(Path(__file__).parent / "naver_monitor.db")
UA_PC          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MAX_PER_OUTLET = 20     # 언론사당 최대 수집 기사 수
DAYS_LOOKBACK  = 3      # 최근 N일치
RESOLVE_CONC   = 4      # Playwright 동시 탭 수
SCRAPE_DELAY   = 1.0    # trafilatura 스크래핑 간격 (초)

GOOGLE_RSS_TMPL = "https://news.google.com/rss/search?q=site:{domain}&hl=ko&gl=KR&ceid=KR:ko"

# ── 비제휴 언론사 목록 ─────────────────────────────────────────────────────────
OUTLETS = [
    # 지역지
    {"name": "경남신문",      "domain": "knnews.co.kr"},
    {"name": "경남도민일보",   "domain": "idomin.com"},
    {"name": "울산매일",      "domain": "ulsanmeil.com"},
    {"name": "울산신문",      "domain": "ulsanpress.net"},
    {"name": "광주일보",      "domain": "kwangju.co.kr"},
    {"name": "전남일보",      "domain": "jnilbo.com"},
    {"name": "남도일보",      "domain": "namdonews.com"},
    {"name": "전북일보",      "domain": "jjan.kr"},
    {"name": "전북도민일보",   "domain": "domin.co.kr"},
    {"name": "새전북신문",    "domain": "sjbnews.com"},
    {"name": "전라일보",      "domain": "jeollailbo.com"},
    {"name": "대구일보",      "domain": "idaegu.co.kr"},
    {"name": "영남일보",      "domain": "yeongnam.com"},
    {"name": "경북일보",      "domain": "kyongbuk.co.kr"},
    {"name": "경북매일",      "domain": "kbmaeil.com"},
    {"name": "충청일보",      "domain": "ccdailynews.com"},
    {"name": "충청투데이",    "domain": "cctoday.co.kr"},
    {"name": "충남일보",      "domain": "chungnamilbo.com"},
    {"name": "충북일보",      "domain": "inews365.com"},
    {"name": "충청매일",      "domain": "cmnews.co.kr"},
    {"name": "제주일보",      "domain": "jejunews.com"},
    {"name": "한라일보",      "domain": "ihalla.com"},
    {"name": "제주신보",      "domain": "jejusori.net"},
    {"name": "인천일보",      "domain": "incheonilbo.com"},
    {"name": "경인일보",      "domain": "kyeongin.com"},
    {"name": "기호일보",      "domain": "kihoilbo.co.kr"},
    {"name": "경기신문",      "domain": "kgnews.co.kr"},
    {"name": "수원일보",      "domain": "suwon.com"},
    {"name": "중부일보",      "domain": "joongboo.com"},
    {"name": "강원매일",      "domain": "kwnews.co.kr"},
    # 온라인/시사
    {"name": "뉴데일리",      "domain": "newdaily.co.kr"},
    {"name": "펜앤드마이크",   "domain": "pennmike.com"},
    {"name": "아시아투데이",   "domain": "asiatoday.co.kr"},
    {"name": "민중의소리",    "domain": "vop.co.kr"},
    {"name": "뉴스토마토",    "domain": "newstomato.com"},
    {"name": "이투데이",      "domain": "etoday.co.kr"},
    {"name": "뉴스핌",        "domain": "newspim.com"},
    {"name": "글로벌이코노믹", "domain": "g-enews.com"},
    {"name": "브레이크뉴스",   "domain": "breaknews.com"},
    {"name": "폴리뉴스",      "domain": "polinews.co.kr"},
    {"name": "서울의소리",    "domain": "amn.kr"},
    {"name": "열린뉴스통신",   "domain": "onews.tv"},
    {"name": "위클리오늘",    "domain": "weeklytoday.com"},
    {"name": "시장경제",      "domain": "meconomynews.com"},
    {"name": "굿모닝경제",    "domain": "goodnews1.com"},
    # 경제/IT
    {"name": "에너지경제신문",  "domain": "ekn.kr"},
    {"name": "전기신문",       "domain": "electimes.com"},
    {"name": "건설경제신문",   "domain": "cnews.co.kr"},
    {"name": "파이낸스투데이",  "domain": "fntoday.co.kr"},
    {"name": "식품음료신문",   "domain": "thinkfood.co.kr"},
    {"name": "농수축산신문",   "domain": "amnews.co.kr"},
    {"name": "물류신문",      "domain": "klnews.co.kr"},
    {"name": "IT조선",        "domain": "it.chosun.com"},
    {"name": "테크월드",      "domain": "techworld.co.kr"},
    {"name": "아이티데일리",   "domain": "itdaily.kr"},
    {"name": "팍스넷뉴스",    "domain": "paxnetnews.com"},
    # 의료/법률
    {"name": "청년의사",      "domain": "docdocdoc.co.kr"},
    {"name": "메디칼타임즈",   "domain": "medicaltimes.com"},
    {"name": "의학신문",      "domain": "bosa.co.kr"},
    {"name": "약업신문",      "domain": "yakup.com"},
    {"name": "법률신문",      "domain": "lawtimes.co.kr"},
    {"name": "의사신문",      "domain": "doctorstimes.com"},
    # 추가 온라인/시사
    {"name": "천지일보",      "domain": "newscj.com"},
    {"name": "브릿지경제",    "domain": "viva100.com"},
    {"name": "비즈니스포스트", "domain": "businesspost.co.kr"},
    {"name": "위키트리",      "domain": "wikitree.co.kr"},
    {"name": "미디어스",      "domain": "medias.or.kr"},
    {"name": "레디앙",        "domain": "redian.org"},
    {"name": "오피니언뉴스",  "domain": "opinionnews.co.kr"},
    {"name": "뉴스포스트",    "domain": "newspost.kr"},
    {"name": "뉴시안",        "domain": "newsian.co.kr"},
    {"name": "시사오늘",      "domain": "sisaon.co.kr"},
    {"name": "시사위크",      "domain": "sisaweek.com"},
    {"name": "조세금융신문",  "domain": "tfnews.co.kr"},
    {"name": "뉴스투데이",    "domain": "news2day.co.kr"},
    {"name": "한스경제",      "domain": "hansbiz.co.kr"},
    {"name": "데일리임팩트",  "domain": "dailyimpact.net"},
    # 추가 경제/금융
    {"name": "파이낸셜투데이", "domain": "ftoday.co.kr"},
    {"name": "벤처스퀘어",    "domain": "venturesquare.net"},
    {"name": "플래텀",        "domain": "platum.kr"},
    {"name": "테크42",        "domain": "tech42.co.kr"},
    {"name": "한국금융신문",  "domain": "fntimes.com"},
    {"name": "세계파이낸스",  "domain": "segyefn.com"},
    {"name": "더스탁",        "domain": "thestock.co.kr"},
    {"name": "이코노믹리뷰",  "domain": "econovill.com"},
    {"name": "스타트업투데이", "domain": "startuptoday.co.kr"},
    # 스포츠/엔터
    {"name": "스포츠조선",    "domain": "sportschosun.com"},
    {"name": "스포츠서울",    "domain": "sportsseoul.com"},
    {"name": "스포츠한국",    "domain": "sportshankook.co.kr"},
    {"name": "뉴스엔",        "domain": "newsen.com"},
    {"name": "조이뉴스24",    "domain": "joynews24.com"},
    {"name": "톱스타뉴스",    "domain": "topstarnews.com"},
    # 추가 IT/기술
    {"name": "ZDNet Korea",   "domain": "zdnet.co.kr"},
    {"name": "디지털투데이",  "domain": "digitaltoday.co.kr"},
    {"name": "인공지능신문",  "domain": "aitimes.kr"},
    {"name": "로봇신문",      "domain": "irobotnews.com"},
    # 추가 의료/건강
    {"name": "히트뉴스",      "domain": "hitnews.co.kr"},
    {"name": "메디파나",      "domain": "medipana.com"},
    {"name": "팜뉴스",        "domain": "pharmnews.com"},
    {"name": "데일리팜",      "domain": "dailypharm.com"},
    {"name": "메디게이트뉴스", "domain": "medigatenews.com"},
    # 추가 지역지
    {"name": "광주드림",      "domain": "gjdream.com"},
    {"name": "무등일보",      "domain": "mdilbo.com"},
    {"name": "경남매일",      "domain": "gnmaeil.com"},
    {"name": "전민일보",      "domain": "jeonmin.co.kr"},
    {"name": "충청신문",      "domain": "csnews.co.kr"},
    {"name": "강원신문",      "domain": "kwsn.net"},
    {"name": "경기매일",      "domain": "kgmaeil.com"},
    {"name": "경남일보",      "domain": "gnnews.net"},
    {"name": "대구신문",      "domain": "dnews.co.kr"},
    {"name": "전남매일",      "domain": "jnmaeil.com"},
]


def ensure_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()]
    if "is_naver_partner" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN is_naver_partner INTEGER DEFAULT 1")
        conn.commit()
        print("is_naver_partner 컬럼 추가됨")


async def fetch_rss(session, domain: str) -> list[dict]:
    url = GOOGLE_RSS_TMPL.format(domain=domain)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20),
                               headers={"User-Agent": UA_PC}, ssl=False) as r:
            text = await r.text()
    except Exception as e:
        print(f"    RSS 오류: {e}")
        return []

    items = []
    cutoff = datetime.now() - timedelta(days=DAYS_LOOKBACK)
    try:
        root = ET.fromstring(text)
        for item in root.iter("item"):
            title = re.sub(r"<[^>]+>", "", item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            pub   = item.findtext("pubDate") or ""
            try:
                dt = parsedate_to_datetime(pub)
                dt_naive = dt.replace(tzinfo=None)
            except Exception:
                dt_naive = datetime.now()
            if dt_naive < cutoff:
                continue
            if link:
                items.append({"google_link": link, "title": title,
                              "date": dt_naive.strftime("%Y.%m.%d %H:%M")})
    except ET.ParseError:
        pass
    return items[:MAX_PER_OUTLET]


async def resolve_links(browser, rss_items: list[dict], domain: str) -> list[dict]:
    sem = asyncio.Semaphore(RESOLVE_CONC)
    results = []

    async def resolve_one(item):
        async with sem:
            ctx  = await browser.new_context(user_agent=UA_PC)
            page = await ctx.new_page()
            try:
                await page.goto(item["google_link"], wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1200)
                final_url = page.url
            except Exception:
                final_url = ""
            finally:
                await ctx.close()

        if not final_url or "google.com" in final_url:
            return
        if domain not in final_url:
            return
        results.append({"url": final_url, "title": item["title"], "date": item["date"]})

    await asyncio.gather(*[resolve_one(i) for i in rss_items])
    return results


def scrape_body(url: str) -> str | None:
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        return text.strip() if text and len(text) > 50 else None
    except Exception:
        return None


async def main():
    conn = sqlite3.connect(DB_FILE, timeout=60)
    ensure_column(conn)

    existing = set(
        r[0] for r in conn.execute(
            "SELECT url FROM articles WHERE is_naver_partner = 0"
        ).fetchall()
    )

    total_new = total_fail = total_no_rss = 0

    print("=" * 60)
    print(f"  비제휴 언론사 수집  |  {len(OUTLETS)}개사  |  최근 {DAYS_LOOKBACK}일")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            for i, outlet in enumerate(OUTLETS, 1):
                name, domain = outlet["name"], outlet["domain"]
                print(f"\n[{i}/{len(OUTLETS)}] {name}")

                rss_items = await fetch_rss(session, domain)
                if not rss_items:
                    print(f"  RSS 없음 (구글 색인 0건)")
                    total_no_rss += 1
                    continue

                print(f"  RSS: {len(rss_items)}건 → URL 추출 중...")
                resolved = await resolve_links(browser, rss_items, domain)
                new_arts = [a for a in resolved if a["url"] not in existing]
                print(f"  추출: {len(resolved)}건 / 신규: {len(new_arts)}건")

                for art in new_arts:
                    url, title, date = art["url"], art["title"], art["date"]
                    body = scrape_body(url)
                    time.sleep(SCRAPE_DELAY)

                    if body:
                        conn.execute("""
                            INSERT OR IGNORE INTO articles
                              (url, press_name, press_code, title, article_date,
                               byline, body, is_deleted, is_exclusive,
                               is_naver_partner, first_seen)
                            VALUES (?, ?, 'NP', ?, ?, NULL, ?, 0, 0, 0, ?)
                        """, (url, name, title, date, body,
                              datetime.now().strftime("%Y.%m.%d %H:%M")))
                        existing.add(url)
                        total_new += 1
                        print(f"  ✓ {title[:45]}")
                    else:
                        total_fail += 1
                        print(f"  ✗ 본문 실패: {title[:45]}")

                conn.commit()

            await browser.close()

    conn.close()
    print(f"\n{'=' * 60}")
    print(f"완료 — 신규 저장: {total_new}건 / 본문 실패: {total_fail}건 / RSS 없음: {total_no_rss}개사")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
