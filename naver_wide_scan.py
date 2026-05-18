"""
네이버 뉴스 전체 언론사 와이드 스캔
- media.naver.com/press 에서 전체 언론사 목록 수집
- 언론사당 3건 스캔 → 위반율 순 랭킹
"""

import asyncio
import json
import re
import sys
import io
from collections import Counter, defaultdict
from datetime import datetime
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

MAX_ARTICLES  = 3     # 언론사당 스캔 기사 수
HEADLESS      = True
TIER_ARTS     = [50, 20, 10]   # 상위 1/3, 중위 1/3, 하위 1/3 최대 수집 건수

PHOTO_PATTERN = re.compile(r"\[포토|포토\]|\[사진\]|포토多이슈", re.IGNORECASE)

ITEM_LABELS = {
    "B_clickbait":      "B.클릭베이트",
    "C_byline_missing": "C.바이라인 없음",
    "D_ai_undisclosed": "D.AI생성 미표시",
    "E_sensational":    "E.선정성",
    "F_title_mismatch": "F.제목-본문 불일치",
    "L_keyword_abuse":  "L.키워드 반복남용",
}
ITEM_WEIGHTS = {
    "B_clickbait":      0.5,
    "C_byline_missing": 1.0,
    "D_ai_undisclosed": 0.5,
    "E_sensational":    1.0,
    "F_title_mismatch": 0.5,
    "L_keyword_abuse":  1.5,
}

SENSATIONAL_WORDS = [
    "성기", "유두", "유륜", "항문", "둔부", "음모", "전라",
    "성행위", "성교", "성기구", "체벌", "강간", "성폭행",
    "살인", "토막", "엽기", "자살 방법", "자해",
]
CLICKBAIT_PATTERNS = [
    (r"충격[적]?",      "충격적 표현"),
    (r"경악",           "경악 표현"),
    (r"발칵",           "발칵 표현"),
    (r"알고\s*보니",    "알고보니 패턴"),
    (r"사실은\??",      "사실은 패턴"),
    (r"무슨\s*일[이?]", "무슨일 패턴"),
    (r"이유가\??",      "이유가? 패턴"),
    (r"\.\.\.+$",       "말줄임 제목"),
    (r"\?$",            "의문형 제목"),
]
AI_GENERATION_WORDS  = ["ai가 작성", "ai가 생성", "인공지능이 작성", "chatgpt", "gpt-4", "gemini"]
AI_DISCLOSURE_WORDS  = ["ai 활용", "ai 생성", "인공지능 활용", "[ai]", "(ai)", "ai기술", "생성형 ai"]
KEYWORD_REPEAT_TITLE = 2
KEYWORD_REPEAT_BODY  = 8


# ── 전체 언론사 목록 수집 ────────────────────────────────────────────────────
async def get_press_list(page) -> list[tuple[str, str]]:
    await page.goto("https://media.naver.com/press/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)

    links = await page.query_selector_all("a[href*='/press/']")
    seen, result = set(), []
    for link in links:
        href = await link.get_attribute("href") or ""
        m = re.search(r'/press/(\d{3,4})', href)
        if not m:
            continue
        code = m.group(1).zfill(3)
        if code in seen:
            continue
        seen.add(code)
        # 이름은 img alt 속성에 있음
        name = ""
        img = await link.query_selector("img")
        if img:
            name = (await img.get_attribute("alt") or "").strip()
        if not name:
            name = (await link.inner_text()).strip()
        if name:
            result.append((code, name))

    return result


# ── 기사 목록 수집 ───────────────────────────────────────────────────────────
async def get_article_list(page, press_code: str, max_count: int) -> list[dict]:
    url = f"https://news.naver.com/main/list.naver?mode=LPOD&mid=sec&oid={press_code}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    candidates = []
    for sel in [
        ".list_body .type06_headline li dt:not(.photo) a",
        ".list_body .type06_headline li a",
        ".list_body .type06 li dt:not(.photo) a",
        ".list_body .type06 li a",
        "ul.type06_headline li dt a",
        "ul.type06 li dt a",
    ]:
        links = await page.query_selector_all(sel)
        for link in links:
            title = (await link.inner_text()).strip()
            href  = await link.get_attribute("href")
            if title and href and "article" in href:
                candidates.append({"title": title, "url": href})
        if candidates:
            break

    seen, unique = set(), []
    for a in candidates:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)
    return unique[:max_count]


# ── 기사 본문 추출 ───────────────────────────────────────────────────────────
async def get_article_content(page, url: str) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1000)

    async def text(*sels):
        for sel in sels:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                if t:
                    return t
        return ""

    title    = await text("#title_area span", ".media_end_head_headline span")
    body     = await text("#newsct_article", "#articeBody", ".go_trans._article_content")
    byline_r = await text(".media_end_head_journalist_name", ".byline_s", ".journalist_name")
    category = await text(".media_end_categorize_item", ".Nnews_category")
    date_str = await text(".media_end_head_info_datestamp_time", "._article_date_time")

    byline = re.sub(r"\s*(기자|특파원|기자\s*=).*|@\S+|\s+", " ", byline_r).strip()
    return {"title": title, "body": body, "byline": byline,
            "category": category, "date": date_str}


# ── 규칙 분석 ────────────────────────────────────────────────────────────────
def analyze_rules(article: dict) -> dict:
    title    = article.get("title", "")
    body     = article.get("body",  "")
    byline   = article.get("byline", "")
    body_l   = body.lower()
    is_photo = bool(PHOTO_PATTERN.search(title))
    results  = {}

    hits = [(label, pat) for pat, label in CLICKBAIT_PATTERNS if re.search(pat, title)]
    results["B_clickbait"] = {
        "violated": bool(hits),
        "reason": f"패턴 감지: {', '.join(l for l,_ in hits[:3])}" if hits else "정상",
    }

    c_bad = not byline or len(byline) < 2
    results["C_byline_missing"] = {
        "violated": c_bad,
        "reason": "기자명 없음 또는 식별 불가" if c_bad else f"기자: {byline}",
    }

    has_ai   = any(w in body_l for w in AI_GENERATION_WORDS)
    has_disc = any(w in body_l for w in AI_DISCLOSURE_WORDS)
    results["D_ai_undisclosed"] = {
        "violated": has_ai and not has_disc,
        "reason": "AI 생성 정황 있으나 표시 없음" if (has_ai and not has_disc) else "정상",
    }

    hit_words = [w for w in SENSATIONAL_WORDS if w in title or w in body[:500]]
    results["E_sensational"] = {
        "violated": bool(hit_words),
        "reason": f"선정어 감지: {', '.join(hit_words[:4])}" if hit_words else "정상",
    }

    if is_photo or not body:
        results["F_title_mismatch"] = {"violated": False, "reason": "포토/본문없음 제외"}
    else:
        t_words = [w for w in re.findall(r"[가-힣a-zA-Z]{2,}", title) if len(w) >= 2]
        if t_words:
            missing    = [w for w in t_words if w not in body]
            miss_ratio = len(missing) / len(t_words)
            bad = miss_ratio > 0.6 and len(t_words) >= 3
            results["F_title_mismatch"] = {
                "violated": bad,
                "reason": f"제목어 {len(missing)}/{len(t_words)}개 본문 부재: {missing[:4]}" if bad else "정상",
            }
        else:
            results["F_title_mismatch"] = {"violated": False, "reason": "판단 불가"}

    t_words  = re.findall(r"[가-힣a-zA-Z]{2,}", title)
    t_abused = [w for w, c in Counter(t_words).items() if c >= KEYWORD_REPEAT_TITLE]
    b_abused = [] if is_photo else [
        w for w, c in Counter(re.findall(r"[가-힣a-zA-Z]{2,}", body)).items()
        if c >= KEYWORD_REPEAT_BODY
    ][:5]
    reasons = []
    if t_abused: reasons.append(f"제목: {t_abused}")
    if b_abused: reasons.append(f"본문: {b_abused}")
    results["L_keyword_abuse"] = {
        "violated": bool(t_abused or b_abused),
        "reason": " / ".join(reasons) if reasons else "정상",
    }

    return results


def article_score(checks: dict) -> float:
    return sum(ITEM_WEIGHTS.get(k, 0) for k, v in checks.items()
               if isinstance(v, dict) and v.get("violated"))


# ── 랭킹 출력 ────────────────────────────────────────────────────────────────
def print_ranking(summary: list[dict]):
    ranked = sorted(summary, key=lambda d: d["wscore"], reverse=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    keys = list(ITEM_LABELS.keys())

    print(f"\n\n{'#'*72}")
    print(f"  네이버 뉴스 와이드 스캔 — 위반 랭킹")
    print(f"  분석 시각: {now}  |  언론사당 {MAX_ARTICLES}건")
    print(f"  배점: B=0.5 C=1.0 D=0.5 E=1.0 F=0.5 L=1.5")
    print(f"{'#'*72}\n")

    hdr_letters = [ITEM_LABELS[k].split(".")[0] for k in keys]
    print(f"  {'순위':>4}  {'언론사':<12}" +
          "".join(f"{h:>5}" for h in hdr_letters) +
          f"{'위반':>5}{'총기사':>6}{'감점':>6}")
    print("  " + "-" * 68)

    for rank, d in enumerate(ranked, 1):
        row = (f"  {rank:>4}  {d['name']:<12}" +
               "".join(f"{d['counts'].get(k,0):>5}" for k in keys) +
               f"{d['flagged']:>5}{d['total']:>6}{d['wscore']:>6.1f}")
        print(row)

    print("  " + "-" * 68)
    print(f"\n  * 기사 0건 수집된 언론사는 목록에서 제외됨")
    print(f"  * 감점 상위 언론사를 naver_monitor.py PRESS_LIST에 추가 추천")
    print(f"{'#'*72}\n")

    # 상위 추천 목록
    top = [d for d in ranked if d["total"] > 0][:20]
    print("  [정밀 모니터링 추천 언론사 TOP 20]")
    for d in top:
        rate = d["flagged"] / d["total"] * 100 if d["total"] else 0
        print(f"  ({d['code']}, \"{d['name']}\"),  # 위반율 {rate:.0f}%  감점 {d['wscore']:.1f}")
    print()


# ── 메인 ─────────────────────────────────────────────────────────────────────
async def main():
    print(f"\n{'='*72}")
    print(f"  네이버 뉴스 와이드 스캔 시작")
    print(f"  언론사 목록 수집 중...")
    print(f"{'='*72}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        press_list = await get_press_list(page)
        print(f"  언론사 {len(press_list)}개 발견\n")

        summary = []
        for idx, (code, name) in enumerate(press_list, 1):
            print(f"[{idx:>3}/{len(press_list)}] {name[:14]:<14} (코드:{code})", end=" ", flush=True)
            try:
                articles = await get_article_list(page, code, MAX_ARTICLES)
                if not articles:
                    print("기사 없음 — skip")
                    continue
            except Exception as e:
                print(f"목록 오류: {e}")
                continue

            results, wscore, flagged = [], 0.0, 0
            counts = defaultdict(int)
            for art in articles:
                try:
                    content = await get_article_content(page, art["url"])
                    art.update(content)
                    checks = analyze_rules(art)
                    score  = article_score(checks)
                    wscore += score
                    items  = [k for k, v in checks.items() if isinstance(v, dict) and v.get("violated")]
                    if items:
                        flagged += 1
                    for k in items:
                        counts[k] += 1
                    results.append({"article": art, "checks": checks})
                except Exception:
                    pass

            rate = flagged / len(results) * 100 if results else 0
            print(f"위반 {flagged}/{len(results)}건  감점 {wscore:.1f}점")
            summary.append({
                "code": code, "name": name,
                "counts": dict(counts),
                "flagged": flagged, "total": len(results),
                "wscore": wscore,
                "results": results,
            })

        await browser.close()

    # 기사 수집된 언론사만 필터
    summary = [d for d in summary if d["total"] > 0]

    print_ranking(summary)

    ts  = datetime.now().strftime("%Y%m%d_%H%M")
    out = f"wide_scan_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"  상세 결과 저장: {out}")

    # 계층별 랭킹 저장 → naver_monitor.py 가 이 파일을 읽어 언론사 목록/기사수 결정
    ranked = sorted(
        [d for d in summary if d["total"] > 0],
        key=lambda d: d["wscore"], reverse=True
    )
    n = len(ranked)
    top_cut = n // 3          # 상위 1/3 마지막 인덱스 (exclusive)
    mid_cut = n - (n // 3)   # 하위 1/3 시작 인덱스
    ranking_data = []
    for i, d in enumerate(ranked):
        if i < top_cut:
            max_arts = TIER_ARTS[0]
        elif i < mid_cut:
            max_arts = TIER_ARTS[1]
        else:
            max_arts = TIER_ARTS[2]
        ranking_data.append({
            "code": d["code"],
            "name": d["name"],
            "wscore": round(d["wscore"], 2),
            "max_articles": max_arts,
        })
    with open("press_ranking.json", "w", encoding="utf-8") as f:
        json.dump(ranking_data, f, ensure_ascii=False, indent=2)
    print(f"  계층별 랭킹 저장: press_ranking.json ({len(ranking_data)}개 언론사)")


if __name__ == "__main__":
    asyncio.run(main())
