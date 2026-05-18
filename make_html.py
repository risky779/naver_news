"""
naver_monitor.db → index.html 생성
GitHub Pages용 정적 리포트
"""
import sqlite3
import json
from datetime import datetime, timedelta

DB_FILE      = "C:/Users/admin/naver_monitor.db"
RANKING_FILE = "C:/Users/admin/press_ranking.json"
OUT_FILE     = "C:/Users/admin/docs/index.html"
CANCEL_THRESHOLD = 10.0
CANCEL_WARNING   = 7.0

ITEM_LABELS = {
    "B_clickbait":      "B.클릭베이트",
    "C_byline_missing": "C.바이라인 없음",
    "D_ai_undisclosed": "D.AI생성 미표시",
    "E_sensational":    "E.선정성",
    "J_duplicate":      "J.중복기사",
    "L_keyword_abuse":  "L.키워드남용",
    "Q_paid_article":   "Q.유가기사",
    "R_commercial":     "R.광고성상품",
}
ITEM_WEIGHTS = {
    "B_clickbait": 0.5, "C_byline_missing": 1.0,
    "D_ai_undisclosed": 0.5, "E_sensational": 1.0,
    "J_duplicate": 1.5, "L_keyword_abuse": 1.5,
    "Q_paid_article": 1.5, "R_commercial": 1.0,
}

def load_data():
    import json as _json
    conn = sqlite3.connect(DB_FILE)
    cutoff = (datetime.now() - timedelta(days=730)).isoformat()

    # 실제 DB 데이터 시작일
    earliest = conn.execute("SELECT MIN(first_seen) FROM articles").fetchone()[0]

    # DB 누적 점수 (기사가 없는 언론사는 0점)
    db_scores = {row[0]: (row[1], row[2]) for row in conn.execute("""
        SELECT press_code,
               COALESCE(SUM(score), 0) as total_score,
               COUNT(*) as article_count
        FROM articles
        WHERE first_seen >= ?
        GROUP BY press_code
    """, (cutoff,)).fetchall()}

    # press_ranking.json 기준 84개사 전체 목록
    try:
        with open(RANKING_FILE, encoding="utf-8") as f:
            ranking = _json.load(f)
    except FileNotFoundError:
        ranking = []

    rows = []
    for entry in ranking:
        code = entry["code"]
        name = entry["name"]
        score, count = db_scores.get(code, (0.0, 0))
        rows.append((name, code, score, count))
    rows.sort(key=lambda x: -x[2])

    articles = conn.execute("""
        SELECT press_name, press_code, title, byline, article_date,
               checks_json, score, violation_text, url
        FROM articles
        WHERE first_seen >= ? AND score > 0
        ORDER BY press_name, score DESC
    """, (cutoff,)).fetchall()

    conn.close()
    return rows, articles, earliest

def status_badge(score):
    if score >= CANCEL_THRESHOLD:
        return '<span class="badge danger">해지권고</span>'
    elif score >= CANCEL_WARNING:
        return '<span class="badge warning">경고</span>'
    else:
        return '<span class="badge ok">정상</span>'

def score_bar(score, max_score=20):
    pct = min(score / max_score * 100, 100)
    color = "#e74c3c" if score >= CANCEL_THRESHOLD else "#f39c12" if score >= CANCEL_WARNING else "#27ae60"
    return f'<div class="bar-wrap"><div class="bar" style="width:{pct:.1f}%;background:{color}"></div><span class="bar-label">{score:.1f}점</span></div>'

def make_html(rows, articles, earliest=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if earliest:
        start_label = earliest[:10]
        delta = datetime.now() - datetime.fromisoformat(earliest)
        period_label = f"{start_label} ~ 현재 ({delta.days}일)"
    else:
        period_label = "데이터 없음"

    danger  = [r for r in rows if r[2] >= CANCEL_THRESHOLD]
    warning = [r for r in rows if CANCEL_WARNING <= r[2] < CANCEL_THRESHOLD]
    normal  = [r for r in rows if r[2] < CANCEL_WARNING]

    # 언론사별 위반 기사 그룹핑
    art_by_press = {}
    for a in articles:
        press_name = a[0]
        if press_name not in art_by_press:
            art_by_press[press_name] = []
        art_by_press[press_name].append(a)

    # 랭킹 테이블 행
    rank_rows = ""
    for i, (name, code, score, count) in enumerate(rows, 1):
        badge = status_badge(score)
        bar   = score_bar(score)
        rank_rows += f"""
        <tr onclick="toggleDetail('{code}')" class="press-row">
          <td class="rank">{i}</td>
          <td class="name">{name} {badge}</td>
          <td>{bar}</td>
          <td class="center">{count}건</td>
        </tr>
        <tr id="detail-{code}" class="detail-row" style="display:none">
          <td colspan="4">
            {make_violation_detail(name, art_by_press.get(name, []))}
          </td>
        </tr>"""

    # 요약 카드
    summary_cards = f"""
    <div class="cards">
      <div class="card danger-card">
        <div class="card-num">{len(danger)}</div>
        <div class="card-label">해지권고 ({CANCEL_THRESHOLD:.0f}점↑)</div>
      </div>
      <div class="card warning-card">
        <div class="card-num">{len(warning)}</div>
        <div class="card-label">경고 ({CANCEL_WARNING:.0f}점↑)</div>
      </div>
      <div class="card ok-card">
        <div class="card-num">{len(normal)}</div>
        <div class="card-label">정상</div>
      </div>
      <div class="card total-card">
        <div class="card-num">{len(rows)}</div>
        <div class="card-label">총 언론사</div>
      </div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>네이버 뉴스 품질 모니터링</title>
<style>
  :root {{
    --danger: #e74c3c; --warning: #f39c12; --ok: #27ae60;
    --bg: #f5f6fa; --card-bg: #fff; --border: #dfe4ea;
    --text: #2f3542; --sub: #747d8c;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Noto Sans KR', sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }}
  header {{ background: #2f3542; color: #fff; padding: 20px 32px; }}
  header h1 {{ font-size: 20px; font-weight: 700; }}
  header .meta {{ font-size: 12px; color: #a4b0be; margin-top: 4px; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 16px; }}
  .cards {{ display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }}
  .card {{ flex: 1; min-width: 120px; background: var(--card-bg); border-radius: 8px;
           padding: 16px; text-align: center; border-top: 4px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  .danger-card {{ border-color: var(--danger); }}
  .warning-card {{ border-color: var(--warning); }}
  .ok-card {{ border-color: var(--ok); }}
  .total-card {{ border-color: #576574; }}
  .card-num {{ font-size: 32px; font-weight: 700; }}
  .card-label {{ font-size: 12px; color: var(--sub); margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card-bg);
           border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  th {{ background: #f1f2f6; padding: 10px 14px; text-align: left; font-size: 12px; color: var(--sub); border-bottom: 1px solid var(--border); }}
  td {{ padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
  .press-row {{ cursor: pointer; transition: background .15s; }}
  .press-row:hover {{ background: #f1f2f6; }}
  .rank {{ width: 40px; font-weight: 700; color: var(--sub); }}
  .name {{ font-weight: 600; }}
  .center {{ text-align: center; }}
  .badge {{ display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 4px; margin-left: 6px; font-weight: 600; }}
  .badge.danger {{ background: #ffeaea; color: var(--danger); }}
  .badge.warning {{ background: #fff8e1; color: var(--warning); }}
  .badge.ok {{ background: #e8f8ee; color: var(--ok); }}
  .bar-wrap {{ display: flex; align-items: center; gap: 8px; }}
  .bar {{ height: 8px; border-radius: 4px; min-width: 2px; transition: width .3s; }}
  .bar-label {{ font-size: 12px; color: var(--sub); white-space: nowrap; }}
  .detail-row td {{ background: #f8f9fc; padding: 12px 16px; }}
  .vio-list {{ list-style: none; }}
  .vio-item {{ border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; background: #fff; }}
  .vio-title {{ font-weight: 600; font-size: 13px; margin-bottom: 4px; }}
  .vio-meta {{ font-size: 11px; color: var(--sub); margin-bottom: 6px; }}
  .vio-tag {{ display: inline-block; font-size: 11px; padding: 2px 6px; border-radius: 3px;
              background: #ffeaea; color: var(--danger); margin-right: 4px; margin-bottom: 2px; }}
  .vio-text {{ font-size: 11px; color: #576574; margin-top: 4px; border-left: 3px solid var(--border); padding-left: 8px; }}
  .no-vio {{ color: var(--sub); font-size: 13px; padding: 8px 0; }}
  footer {{ text-align: center; color: var(--sub); font-size: 12px; padding: 32px 16px; }}
  @media (max-width: 600px) {{ .cards {{ gap: 8px; }} .card {{ min-width: 80px; padding: 12px; }} .card-num {{ font-size: 24px; }} }}
</style>
</head>
<body>
<header>
  <h1>네이버 뉴스 품질 모니터링</h1>
  <div class="meta">마지막 업데이트: {now} &nbsp;|&nbsp; 누적 데이터 기간: {period_label} &nbsp;|&nbsp; 규정: 제14조 제10항 (24개월 누적 10점 이상 → 해지권고)</div>
</header>
<div class="container">
  {summary_cards}
  <table>
    <thead>
      <tr>
        <th>순위</th>
        <th>언론사</th>
        <th>24개월 누적 점수</th>
        <th>분석기사</th>
      </tr>
    </thead>
    <tbody>
      {rank_rows}
    </tbody>
  </table>
</div>
<footer>네이버 뉴스 제휴 심사 및 운영 평가 규정 (2026.02.11) 기준 &nbsp;|&nbsp; 해지 기준: 24개월 누적 10점 이상</footer>
<script>
function toggleDetail(code) {{
  const el = document.getElementById('detail-' + code);
  el.style.display = el.style.display === 'none' ? 'table-row' : 'none';
}}
</script>
</body>
</html>"""


def make_violation_detail(press_name, arts):
    if not arts:
        return '<p class="no-vio">위반 의심 기사 없음</p>'

    items_html = ""
    for a in arts[:20]:
        title    = a[2] or ""
        byline   = a[3] or "(없음)"
        date_str = a[4] or ""
        checks_json = a[5] or "{}"
        score    = a[6] or 0
        vio_text = a[7] or ""
        url      = a[8] or "#"

        try:
            checks = json.loads(checks_json)
        except Exception:
            checks = {}

        tags = "".join(
            f'<span class="vio-tag">{ITEM_LABELS.get(k, k)} {ITEM_WEIGHTS.get(k, 0)}점</span>'
            for k in checks
        )

        vio_lines = ""
        if vio_text:
            for line in vio_text.split("\n"):
                if line.strip():
                    vio_lines += f'<div class="vio-text">{line.strip()}</div>'

        items_html += f"""
        <li class="vio-item">
          <div class="vio-title"><a href="{url}" target="_blank" style="color:inherit;text-decoration:none">{title}</a></div>
          <div class="vio-meta">기자: {byline} &nbsp;|&nbsp; {date_str} &nbsp;|&nbsp; 감점: {score:.1f}점</div>
          <div>{tags}</div>
          {vio_lines}
        </li>"""

    return f'<ul class="vio-list">{items_html}</ul>'


if __name__ == "__main__":
    import os
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    rows, articles, earliest = load_data()
    if not rows:
        print("DB에 데이터가 없습니다.")
    else:
        html = make_html(rows, articles, earliest)
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"생성 완료: {OUT_FILE}  ({len(rows)}개 언론사)")
