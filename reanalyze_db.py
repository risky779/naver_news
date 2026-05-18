"""
DB에 저장된 기사를 재분석하여 checks_json / score / violation_text 업데이트
- body 컬럼이 있으면 재크롤링 없이 즉시 재분석
- body 없으면 Playwright로 재크롤링 후 분석
"""
import asyncio
import json
import sqlite3
import sys
import io
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

DB_FILE  = "C:/Users/admin/naver_monitor.db"

# naver_monitor의 분석 함수·상수 임포트
from naver_monitor import (
    analyze_rules, ITEM_LABELS, ITEM_WEIGHTS,
    init_db, get_article_content
)


def reanalyze_from_body(conn: sqlite3.Connection) -> tuple[int, int]:
    """body 컬럼이 있는 기사는 재크롤링 없이 재분석"""
    rows = conn.execute(
        "SELECT url, title, byline, body FROM articles WHERE body IS NOT NULL AND body != ''"
    ).fetchall()

    updated = skipped = 0
    for url, title, byline, body in rows:
        art = {"title": title, "byline": byline, "body": body, "date": ""}
        checks = analyze_rules(art)
        checks_to_save = {k: v for k, v in checks.items()
                          if k != "J_duplicate" and isinstance(v, dict) and v.get("violated")}
        score = sum(ITEM_WEIGHTS.get(k, 0) for k in checks_to_save)
        vt_lines = []
        for k, v in checks_to_save.items():
            if v.get("text"):
                vt_lines.append(f"[{ITEM_LABELS.get(k, k)}] {v['text']}")
        violation_text = "\n".join(vt_lines) if vt_lines else None

        conn.execute("""
            UPDATE articles
            SET checks_json=?, score=?, violation_text=?
            WHERE url=?
        """, (json.dumps(checks_to_save, ensure_ascii=False), score, violation_text, url))
        updated += 1

    conn.commit()
    return updated, skipped


async def reanalyze_by_crawl(conn: sqlite3.Connection) -> int:
    """body 없는 기사는 재크롤링 후 분석 및 body 저장"""
    from playwright.async_api import async_playwright

    rows = conn.execute(
        "SELECT url, title, byline FROM articles WHERE body IS NULL OR body = ''"
    ).fetchall()

    if not rows:
        return 0

    print(f"  재크롤링 대상: {len(rows)}건")
    updated = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        for i, (url, stored_title, stored_byline) in enumerate(rows, 1):
            print(f"  [{i}/{len(rows)}] {(stored_title or url)[:50]}", end=" ")
            try:
                content = await get_article_content(page, url)
                title  = content.get("title") or stored_title or ""
                byline = content.get("byline") or stored_byline or ""
                body   = content.get("body", "")

                art = {"title": title, "byline": byline, "body": body, "date": content.get("date", "")}
                checks = analyze_rules(art)
                checks_to_save = {k: v for k, v in checks.items()
                                  if k != "J_duplicate" and isinstance(v, dict) and v.get("violated")}
                score = sum(ITEM_WEIGHTS.get(k, 0) for k in checks_to_save)
                vt_lines = []
                for k, v in checks_to_save.items():
                    if v.get("text"):
                        vt_lines.append(f"[{ITEM_LABELS.get(k, k)}] {v['text']}")
                violation_text = "\n".join(vt_lines) if vt_lines else None

                conn.execute("""
                    UPDATE articles
                    SET title=?, byline=?, body=?, checks_json=?, score=?, violation_text=?
                    WHERE url=?
                """, (title, byline, body,
                      json.dumps(checks_to_save, ensure_ascii=False),
                      score, violation_text, url))
                conn.commit()
                updated += 1
                print(f"→ {score:.1f}점")
            except Exception as e:
                print(f"→ 오류: {e}")

        await browser.close()

    return updated


async def main():
    conn = sqlite3.connect(DB_FILE)
    init_db(conn)

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"\n재분석 시작 — DB 총 {total}건\n")

    # 1단계: body 있는 기사 즉시 재분석
    updated_fast, _ = reanalyze_from_body(conn)
    print(f"  body 재분석 완료: {updated_fast}건 업데이트")

    # 2단계: body 없는 기사 재크롤링
    updated_crawl = await reanalyze_by_crawl(conn)
    if updated_crawl:
        print(f"  재크롤링 재분석 완료: {updated_crawl}건 업데이트")

    total_updated = updated_fast + updated_crawl
    print(f"\n완료 — 총 {total_updated}/{total}건 업데이트")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
