"""
news.naver.com 메인에서 제휴언론사 OID 목록 수집 → press_ranking.json 에 is_partner 필드 추가
"""
import asyncio, json, re
from playwright.async_api import async_playwright

RANKING_FILE = "press_ranking.json"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        partner_codes = set()

        # 1) 뉴스 메인 — 언론사 구독 목록 페이지
        for url in [
            "https://news.naver.com/",
            "https://media.naver.com/press/",
        ]:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            hrefs = await page.evaluate("""() =>
                Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
            """)
            for href in hrefs:
                m = re.search(r'(?:oid=|/press/)(\d{3,4})', href)
                if m:
                    partner_codes.add(m.group(1).zfill(3))

        await browser.close()

    print(f"제휴언론사 OID 수집: {len(partner_codes)}개")
    print(sorted(partner_codes))

    # press_ranking.json 업데이트
    with open(RANKING_FILE, encoding="utf-8") as f:
        ranking = json.load(f)

    matched = 0
    for entry in ranking:
        is_p = entry["code"] in partner_codes
        entry["is_partner"] = is_p
        if is_p:
            matched += 1

    print(f"\npress_ranking.json ({len(ranking)}개) 중 제휴: {matched}개, 비제휴: {len(ranking)-matched}개")

    non_partner = [e["name"] for e in ranking if not e["is_partner"]]
    print("비제휴 목록:", non_partner)

    with open(RANKING_FILE, "w", encoding="utf-8") as f:
        json.dump(ranking, f, ensure_ascii=False, indent=2)
    print(f"\npress_ranking.json 업데이트 완료")

if __name__ == "__main__":
    asyncio.run(main())
