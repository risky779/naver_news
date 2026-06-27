"""
naver_monitor.db → index.html 생성
GitHub Pages용 정적 리포트
"""
from pathlib import Path
import sqlite3
import json
from datetime import datetime, timedelta

DB_FILE       = str(Path(__file__).parent / "naver_monitor.db")
RANKING_FILE  = str(Path(__file__).parent / "press_ranking.json")
PARTNER_FILE  = str(Path(__file__).parent / "partner_oids.json")
OUT_FILE      = str(Path(__file__).parent / "docs" / "index.html")
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


def load_deleted():
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("""
        SELECT press_name, title, byline, article_date,
               checks_json, score, violation_text, url, is_exclusive, body,
               COALESCE(delete_type, 3), source_url
        FROM articles
        WHERE is_deleted = 1
        ORDER BY article_date DESC, score DESC
    """).fetchall()
    conn.close()
    deleted = []
    for r in rows:
        try:
            checks = json.loads(r[4] or "{}")
        except Exception:
            checks = {}
        tags = [f"{ITEM_LABELS.get(k, k)} {ITEM_WEIGHTS.get(k, 0)}점"
                for k, v in checks.items() if isinstance(v, dict) and v.get("violated")]
        deleted.append({
            "press":       r[0] or "",
            "title":       r[1] or "(제목 없음)",
            "byline":      r[2] or "(없음)",
            "date":        r[3] or "",
            "score":       round(float(r[5] or 0), 1),
            "tags":        tags,
            "url":         r[7] or "",
            "exclusive":   bool(r[8]),
            "body":        r[9] or "",
            "deleteType":  int(r[10] or 3),
            "sourceUrl":   r[11] or "",
        })
    return deleted


def load_data():
    import json as _json
    conn = sqlite3.connect(DB_FILE)
    cutoff = (datetime.now() - timedelta(days=730)).isoformat()

    earliest = conn.execute("SELECT MIN(first_seen) FROM articles").fetchone()[0]

    db_scores = {row[0]: (row[1], row[2]) for row in conn.execute("""
        SELECT press_code,
               COALESCE(SUM(score), 0) as total_score,
               COUNT(*) as article_count
        FROM articles
        WHERE first_seen >= ?
        GROUP BY press_code
    """, (cutoff,)).fetchall()}

    try:
        with open(RANKING_FILE, encoding="utf-8") as f:
            ranking = _json.load(f)
    except FileNotFoundError:
        ranking = []

    try:
        with open(PARTNER_FILE, encoding="utf-8") as f:
            partner_oids = set(json.load(f))
    except FileNotFoundError:
        partner_oids = {e["code"] for e in ranking}  # 파일 없으면 전체 제휴로 간주

    rows = []
    for entry in ranking:
        code = entry["code"]
        name = entry["name"]
        if code not in partner_oids:
            name = f"[비제휴] {name}"
        score, count = db_scores.get(code, (0.0, 0))
        rows.append((name, code, score, count))
    rows.sort(key=lambda x: -x[2])

    raw_articles = conn.execute("""
        SELECT press_name, press_code, title, byline, article_date,
               checks_json, score, violation_text, url, is_exclusive, ai_score
        FROM articles
        WHERE first_seen >= ? AND score > 0
        ORDER BY article_date DESC, score DESC
    """, (cutoff,)).fetchall()
    conn.close()

    # press_code 기준으로 기사 데이터를 JS용 dict로 변환
    art_data = {}
    for a in raw_articles:
        code = a[1]
        try:
            checks = json.loads(a[5] or "{}")
        except Exception:
            checks = {}
        tags = [f"{ITEM_LABELS.get(k, k)} {ITEM_WEIGHTS.get(k, 0)}점"
                for k, v in checks.items() if isinstance(v, dict) and v.get("violated")]
        vio_lines = [l.strip() for l in (a[7] or "").split("\n") if l.strip()]
        reasons = {k: v.get("reason", "") for k, v in checks.items()
                   if isinstance(v, dict) and v.get("violated")}
        art_data.setdefault(code, []).append({
            "title":     a[2] or "",
            "byline":    a[3] or "(없음)",
            "date":      a[4] or "",
            "score":     round(float(a[6] or 0), 1),
            "tags":      tags,
            "vioText":   vio_lines,
            "reasons":   reasons,
            "url":       a[8] or "#",
            "exclusive": bool(a[9]),
            "aiScore":   round(float(a[10] or 0), 1) if a[10] is not None else None,
        })

    return rows, art_data, earliest


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
    return (f'<div class="bar-wrap">'
            f'<div class="bar" style="width:{pct:.1f}%;background:{color}"></div>'
            f'<span class="bar-label">{score:.1f}점</span></div>')


def make_html(rows, art_data, earliest=None, deleted=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if earliest:
        from datetime import date as _date
        start_label = earliest[:10]
        start_date  = datetime.fromisoformat(earliest).date()
        delta_days  = (_date.today() - start_date).days
        period_label = f"{start_label} ~ 현재 ({delta_days}일)"
    else:
        period_label = "데이터 없음"

    danger  = [r for r in rows if r[2] >= CANCEL_THRESHOLD]
    warning = [r for r in rows if CANCEL_WARNING <= r[2] < CANCEL_THRESHOLD]
    normal  = [r for r in rows if r[2] < CANCEL_WARNING]

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
            <div id="vdetail-{code}">
              <div class="vio-container"></div>
              <div class="pager"></div>
            </div>
          </td>
        </tr>"""

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

    press_list_json = json.dumps(
        [{"code": code, "name": name} for name, code, score, count in rows],
        ensure_ascii=False
    )
    art_data_json  = json.dumps(art_data, ensure_ascii=False)
    deleted        = deleted or []
    deleted_json   = json.dumps(deleted, ensure_ascii=False)
    deleted_count  = len(deleted)
    excl_count     = sum(1 for d in deleted if d.get("exclusive"))
    type5_count    = sum(1 for d in deleted if d.get("deleteType") == 5)
    type1_count    = sum(1 for d in deleted if d.get("deleteType") == 1)
    type2_count    = sum(1 for d in deleted if d.get("deleteType") == 2 and not d.get("exclusive"))
    type4_count    = sum(1 for d in deleted if d.get("deleteType") == 4)
    type3_count    = sum(1 for d in deleted if d.get("deleteType") in (3, None))

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>뉴스킬러 By 대한민국 해시(#)포럼</title>
<style>
  :root {{
    --danger: #e74c3c; --warning: #f39c12; --ok: #27ae60;
    --bg: #f5f6fa; --card-bg: #fff; --border: #dfe4ea;
    --text: #2f3542; --sub: #747d8c;
    --del-red: #c0392b; --del-bg: #fff8f8; --del-border: #f5c6cb;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Noto Sans KR', sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }}
  header {{ background: #2f3542; color: #fff; padding: 16px 32px; }}
  header h1 {{ font-size: 20px; font-weight: 700; }}
  header .meta {{ font-size: 12px; color: #a4b0be; margin-top: 4px; }}
  /* ── 탭 네비게이션 ── */
  .tab-nav {{ display: flex; background: #fff; border-bottom: 2px solid var(--border); }}
  .tab-btn {{
    flex: 1; padding: 14px 20px; font-size: 15px; font-weight: 700;
    border: none; background: transparent; cursor: pointer; color: var(--sub);
    border-bottom: 3px solid transparent; margin-bottom: -2px;
    transition: color .15s, border-color .15s;
  }}
  .tab-btn:hover {{ color: var(--text); }}
  .tab-btn.active {{ color: var(--del-red); border-bottom-color: var(--del-red); }}
  .tab-btn.active.quality {{ color: #2f3542; border-bottom-color: #2f3542; }}
  .tab-badge {{
    display: inline-block; font-size: 12px; font-weight: 700;
    padding: 2px 8px; border-radius: 10px; margin-left: 8px;
    background: var(--danger); color: #fff; vertical-align: middle;
  }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  /* ── 공통 컨테이너 ── */
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 16px; }}
  /* ── 삭제 의심 요약 카드 ── */
  .del-summary {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
  .del-summary .card {{ flex: 1; min-width: 100px; background: var(--card-bg); border-radius: 8px;
           padding: 14px 10px; text-align: center; border-top: 4px solid var(--border);
           box-shadow: 0 1px 4px rgba(0,0,0,.06); cursor: pointer; transition: box-shadow .15s; }}
  .del-summary .card:hover {{ box-shadow: 0 3px 10px rgba(0,0,0,.12); }}
  .del-summary .card.active-filter {{ box-shadow: 0 0 0 2px #2f3542; }}
  .del-summary .card.all-card    {{ border-color: #576574; }}
  .del-summary .card.type5-card  {{ border-color: #27ae60; }}
  .del-summary .card.type1-card  {{ border-color: #f39c12; }}
  .del-summary .card.type2-card  {{ border-color: var(--danger); }}
  .del-summary .card.type3-card  {{ border-color: #95a5a6; }}
  .del-summary .card.type4-card  {{ border-color: #8e44ad; }}
  .del-summary .card.excl-card   {{ border-color: #d63031; }}
  .card-num {{ font-size: 28px; font-weight: 700; }}
  .card-label {{ font-size: 11px; color: var(--sub); margin-top: 4px; line-height: 1.4; }}
  /* 타입 라벨 뱃지 */
  .type-badge {{
    display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 6px;
    border-radius: 3px; margin-right: 5px; vertical-align: middle; letter-spacing: 0.3px;
  }}
  .type-badge.t5 {{ background: #e8f8ee; color: #1e8449; border: 1px solid #27ae60; }}
  .type-badge.t1 {{ background: #fff3cd; color: #856404; border: 1px solid #f39c12; }}
  .type-badge.t2 {{ background: #ffeaea; color: #c0392b; border: 1px solid var(--danger); }}
  .type-badge.t3 {{ background: #f0f0f0; color: #555; border: 1px solid #bbb; }}
  .type-badge.t4 {{ background: #f3e5ff; color: #6c3483; border: 1px solid #8e44ad; }}
  /* ── 삭제 의심 기사 목록 ── */
  .del-item {{ border: 1px solid var(--del-border); border-radius: 6px; padding: 12px 14px;
               margin-bottom: 10px; background: var(--del-bg); }}
  .del-press {{ font-size: 11px; color: #888; margin-bottom: 3px; }}
  .del-title {{ font-weight: 600; font-size: 14px; color: #555; margin-bottom: 5px; line-height: 1.5; }}
  .del-meta {{ font-size: 11px; color: var(--sub); margin-bottom: 6px; }}
  .del-article-body {{ font-size: 12px; color: #555; line-height: 1.6; margin-top: 8px; padding: 8px 10px;
               background: #fdf6f6; border-left: 3px solid var(--del-border); border-radius: 3px;
               white-space: pre-wrap; word-break: break-all; }}
  .del-pager {{ display: flex; gap: 4px; justify-content: center; padding: 10px 0 4px; flex-wrap: wrap; }}
  .del-pager button {{ border: 1px solid var(--del-border); background: #fff; padding: 4px 10px;
    border-radius: 4px; cursor: pointer; font-size: 12px; }}
  .del-pager button.active {{ background: var(--danger); color: #fff; border-color: var(--danger); }}
  .del-pager button:disabled {{ opacity: .35; cursor: default; }}
  .no-del {{ text-align: center; padding: 40px 0; color: var(--sub); font-size: 14px; }}
  /* ── 품질 모니터링 ── */
  .cards {{ display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }}
  .card {{ flex: 1; min-width: 120px; background: var(--card-bg); border-radius: 8px;
           padding: 16px; text-align: center; border-top: 4px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  .danger-card {{ border-color: var(--danger); }}
  .warning-card {{ border-color: var(--warning); }}
  .ok-card {{ border-color: var(--ok); }}
  .total-card {{ border-color: #576574; }}
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
  .vio-item {{ border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; background: #fff; }}
  .vio-title {{ font-weight: 600; font-size: 13px; margin-bottom: 4px; }}
  .vio-meta {{ font-size: 11px; color: var(--sub); margin-bottom: 6px; }}
  .vio-tag {{ display: inline-block; font-size: 11px; padding: 2px 6px; border-radius: 3px;
              background: #ffeaea; color: var(--danger); margin-right: 4px; margin-bottom: 2px; }}
  .vio-text {{ font-size: 11px; color: #576574; margin-top: 4px; border-left: 3px solid var(--border); padding-left: 8px; }}
  .no-vio {{ color: var(--sub); font-size: 13px; padding: 8px 0; }}
  .page-info {{ font-size: 12px; color: var(--sub); text-align: right; margin-bottom: 8px; }}
  .pager {{ display: flex; gap: 4px; justify-content: center; padding: 12px 0 4px; flex-wrap: wrap; }}
  .pager button {{
    border: 1px solid var(--border); background: #fff; padding: 4px 10px;
    border-radius: 4px; cursor: pointer; font-size: 13px; min-width: 32px;
    transition: background .1s;
  }}
  .pager button:hover:not(:disabled) {{ background: #f1f2f6; }}
  .pager button.active {{ background: #2f3542; color: #fff; border-color: #2f3542; }}
  .pager button:disabled {{ opacity: 0.35; cursor: default; }}
  .pager .pager-ellipsis {{ padding: 4px 6px; font-size: 13px; color: var(--sub); line-height: 1.8; }}
  footer {{ text-align: center; color: var(--sub); font-size: 12px; padding: 28px 16px; }}
  .excl-badge {{ display:inline-block; font-size:10px; font-weight:700; padding:1px 5px;
                 border-radius:3px; background:#d63031; color:#fff; margin-right:5px;
                 vertical-align:middle; letter-spacing:0.5px; }}
  .ai-badge-danger {{ display:inline-block; font-size:10px; font-weight:700; padding:1px 5px;
                 border-radius:3px; background:#e17055; color:#fff; margin-right:5px;
                 vertical-align:middle; letter-spacing:0.5px; }}
  .ai-badge-caution {{ display:inline-block; font-size:10px; font-weight:700; padding:1px 5px;
                 border-radius:3px; background:#fdcb6e; color:#333; margin-right:5px;
                 vertical-align:middle; letter-spacing:0.5px; }}
  /* ── 항목별 현황 매트릭스 ── */
  .matrix-table {{ width: 100%; border-collapse: collapse; background: var(--card-bg);
    border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.06); }}
  .matrix-table th {{ background: #f1f2f6; padding: 8px 6px; text-align: center;
    font-size: 12px; color: var(--sub); border-bottom: 1px solid var(--border); }}
  .matrix-table th:nth-child(1), .matrix-table th:nth-child(2) {{ text-align: left; padding-left: 14px; }}
  .matrix-table td {{ padding: 8px 6px; border-bottom: 1px solid var(--border); vertical-align: middle; text-align: center; }}
  .matrix-table td:nth-child(2) {{ text-align: left; padding-left: 14px; font-weight: 600; }}
  .matrix-row {{ cursor: pointer; transition: background .15s; }}
  .matrix-row:hover {{ background: #f1f2f6; }}
  .th-sort {{ cursor: pointer; user-select: none; }}
  .th-sort:hover {{ background: #e1e3ed !important; }}
  .th-sort-active {{ color: var(--text) !important; background: #e8eaf6 !important; }}
  .m-cell {{ display: inline-block; min-width: 26px; text-align: center; padding: 2px 5px;
    border-radius: 4px; font-size: 13px; font-weight: 600; }}
  .cnt0 {{ color: #ccc; font-weight: 400; }}
  .cnt1 {{ background: #fff3cd; color: #856404; }}
  .cnt3 {{ background: #ffdbb5; color: #c0711a; }}
  .cnt6 {{ background: #ffeaea; color: #c0392b; }}
  .item-detail-row td {{ padding: 0; }}
  .item-detail-inner {{ padding: 12px 16px 16px; background: #f8f9fc; border-top: 1px solid var(--border); }}
  .item-section {{ margin-bottom: 14px; }}
  .item-section-hd {{ font-weight: 700; font-size: 13px; color: var(--danger); margin-bottom: 6px;
    padding-bottom: 4px; border-bottom: 1px solid var(--del-border); }}
  .item-art {{ padding: 4px 0; font-size: 12px; border-bottom: 1px solid #f0f0f0; }}
  .item-art:last-child {{ border-bottom: none; }}
  .item-art-vio {{ font-size: 11px; color: #576574; margin-top: 2px; padding-left: 10px;
    border-left: 2px solid var(--border); }}
  .item-section-unimpl {{ opacity: .55; }}
  .reason-group-unimpl {{ opacity: .55; }}
  .unimpl-hd {{ color: #95a5a6 !important; }}
  .score-hint {{ font-size: 10px; color: #aaa; font-weight: 400; margin-left: 4px; }}
  .th-subtitle {{ display: block; font-size: 9px; font-weight: 400; color: #999; margin-top: 1px; line-height: 1.3; }}
  .sub-num {{ display: inline-block; font-size: 11px; font-weight: 700; color: #2980b9;
    min-width: 26px; }}
  .unimpl-tag {{ display: inline-block; font-size: 10px; font-weight: 400; padding: 1px 5px;
    border-radius: 3px; background: #f0f0f0; color: #95a5a6; border: 1px solid #ddd;
    vertical-align: middle; margin-left: 6px; }}
  .reason-group {{ margin: 6px 0 10px 0; padding-left: 12px; border-left: 3px solid #dfe4ea; }}
  .reason-group-hd {{ font-size: 12px; font-weight: 700; color: #2f3542; margin-bottom: 5px;
    padding: 3px 8px; background: #f1f2f6; border-radius: 3px; display: inline-block; }}
  .reason-cnt {{ font-size: 11px; font-weight: 400; color: var(--sub); margin-left: 6px; }}
  .th-unimpl {{ background: #f8f8f8 !important; color: #bbb !important; cursor: default; line-height: 1.4; }}
  .tab-btn.active.items-tab {{ color: #2980b9; border-bottom-color: #2980b9; }}
  @media (max-width: 600px) {{
    .tab-btn {{ font-size: 13px; padding: 12px 10px; }}
    .cards, .del-summary {{ gap: 8px; }}
    .card, .del-summary .card {{ min-width: 80px; padding: 12px; }}
    .card-num {{ font-size: 24px; }}
  }}
</style>
</head>
<body>
<header>
  <h1>뉴스킬러 By 대한민국 해시(#)포럼</h1>
  <div class="meta">마지막 업데이트: {now} &nbsp;|&nbsp; 누적 데이터 기간: {period_label}</div>
</header>
<div class="tab-nav">
  <button id="tab-btn-del" class="tab-btn active" onclick="switchTab('del')">
    ⚠ 삭제 의심 기사<span class="tab-badge" id="del-tab-badge">{deleted_count}</span>
  </button>
  <button id="tab-btn-quality" class="tab-btn quality" onclick="switchTab('quality')">
    📊 뉴스 품질 모니터링
  </button>
  <button id="tab-btn-items" class="tab-btn items-tab" onclick="switchTab('items')">
    📋 항목별 현황
  </button>
</div>

<!-- 탭 1: 삭제 의심 기사 -->
<div id="tab-del" class="tab-panel active">
  <div class="container">
    <div class="del-summary">
      <div class="card all-card active-filter" onclick="filterDel(0)">
        <div class="card-num">{deleted_count}</div>
        <div class="card-label">전체</div>
      </div>
      <div class="card type5-card" onclick="filterDel(5)">
        <div class="card-num">{type5_count}</div>
        <div class="card-label">링크 삭제<br><span style="font-size:10px;color:#1e8449">URL만 죽음, 기사 존재</span></div>
      </div>
      <div class="card type1-card" onclick="filterDel(1)">
        <div class="card-num">{type1_count}</div>
        <div class="card-label">네이버만 삭제<br><span style="font-size:10px;color:#856404">언론사 원문 생존</span></div>
      </div>
      <div class="card type2-card" onclick="filterDel(2)">
        <div class="card-num">{type2_count}</div>
        <div class="card-label">완전 삭제<br><span style="font-size:10px;color:#c0392b">어디서도 없음</span></div>
      </div>

      <div class="card type3-card" onclick="filterDel(3)">
        <div class="card-num">{type3_count}</div>
        <div class="card-label">미분류<br><span style="font-size:10px;color:#555">검색 전</span></div>
      </div>
      <div class="card excl-card" onclick="filterDel(99)">
        <div class="card-num">{excl_count}</div>
        <div class="card-label">단독 삭제<br><span style="font-size:10px;color:#1a5276">[단독] 기사만</span></div>
      </div>
    </div>
    <div id="del-container"></div>
    <div class="del-pager" id="del-pager"></div>
  </div>
</div>

<!-- 탭 2: 뉴스 품질 모니터링 -->
<div id="tab-quality" class="tab-panel">
  <div class="container">
    <div class="meta" style="font-size:12px;color:var(--sub);margin-bottom:16px;padding-top:4px;">
      규정: 제14조 제10항 &nbsp;|&nbsp; 해지 기준: 24개월 누적 10점 이상
    </div>
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
</div>

<!-- 탭 3: 항목별 현황 -->
<div id="tab-items" class="tab-panel">
  <div class="container">
    <div class="meta" style="font-size:12px;color:var(--sub);margin-bottom:16px;padding-top:4px;">
      신문사별 항목(B~R) 위반 건수 (24개월 누적) &nbsp;|&nbsp; 열 헤더 클릭 시 해당 항목 기준 정렬 &nbsp;|&nbsp; 행 클릭 시 기사 목록
    </div>
    <div id="item-matrix"></div>
  </div>
</div>

<footer>네이버 뉴스 제휴 심사 및 운영 평가 규정 (2026.02.11) 기준</footer>
<script>
const VDATA      = {art_data_json};
const DELETED    = {deleted_json};
const PRESS_LIST = {press_list_json};
const PAGE_SIZE = 10;
const curPage  = {{}};
let   delPage  = 1;
let   delFilter = 0;  // 0=전체, 1~4=타입별
const DEL_SIZE = 10;

const TYPE_BADGE = {{
  5: '<span class="type-badge t5">링크삭제</span>',
  1: '<span class="type-badge t1">네이버만삭제</span>',
  2: '<span class="type-badge t2">완전삭제</span>',
  3: '<span class="type-badge t3">미분류</span>',
  4: '<span class="type-badge t4">언론사삭제</span>',
}};

function filterDel(type) {{
  delFilter = type;
  // 카드 active 표시
  document.querySelectorAll('.del-summary .card').forEach((c, i) => {{
    const types = [0, 5, 1, 2, 3, 99];  // 카드 순서와 매핑
    c.classList.toggle('active-filter', types[i] === type);
  }});
  renderDel(1);
}}

function switchTab(name) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  document.getElementById('tab-btn-' + name).classList.add('active');
  if (name === 'del' && document.getElementById('del-container').innerHTML === '')
    renderDel(1);
  if (name === 'items' && !_matrix)
    renderItemMatrix('total');
}}

/* ── 항목별 현황 ── */
const ITEM_KEYS = [
  'A_court_ruling',
  'B_clickbait','C_byline_missing','D_ai_undisclosed','E_sensational',
  'F_ad_obstruct','G_tech_stability','H_ux_harm','I_url_swap',
  'J_duplicate','K_main_news_abuse','L_keyword_abuse',
  'M_category_mismatch','N_unlicensed','O_copyright','P_unfair_profit',
  'Q_paid_article','R_commercial'
];
const ITEM_UNIMPL = new Set([
  'A_court_ruling','F_ad_obstruct','G_tech_stability','H_ux_harm','I_url_swap',
  'K_main_news_abuse','M_category_mismatch','N_unlicensed','O_copyright','P_unfair_profit'
]);
const ITEM_TAG_PREFIX = {{
  'A_court_ruling':      'A.법원판결',
  'B_clickbait':         'B.클릭베이트',
  'C_byline_missing':    'C.바이라인 없음',
  'D_ai_undisclosed':    'D.AI생성 미표시',
  'E_sensational':       'E.선정성',
  'F_ad_obstruct':       'F.광고방해',
  'G_tech_stability':    'G.기술안정성',
  'H_ux_harm':           'H.이용자경험방해',
  'I_url_swap':          'I.URL바꿔치기',
  'J_duplicate':         'J.중복기사',
  'K_main_news_abuse':   'K.주요뉴스오용',
  'L_keyword_abuse':     'L.키워드남용',
  'M_category_mismatch': 'M.카테고리위반',
  'N_unlicensed':        'N.계약미포함',
  'O_copyright':         'O.저작권침해',
  'P_unfair_profit':     'P.부당이익요구',
  'Q_paid_article':      'Q.유가기사',
  'R_commercial':        'R.광고성상품',
}};
const ITEM_SHORT = {{
  'A_court_ruling':'A', 'B_clickbait':'B', 'C_byline_missing':'C',
  'D_ai_undisclosed':'D', 'E_sensational':'E',
  'F_ad_obstruct':'F', 'G_tech_stability':'G', 'H_ux_harm':'H', 'I_url_swap':'I',
  'J_duplicate':'J', 'K_main_news_abuse':'K', 'L_keyword_abuse':'L',
  'M_category_mismatch':'M', 'N_unlicensed':'N', 'O_copyright':'O', 'P_unfair_profit':'P',
  'Q_paid_article':'Q', 'R_commercial':'R',
}};
const ITEM_SUBTITLE = {{
  'A_court_ruling':      '법원판결',   'B_clickbait':         '과장·왜곡',
  'C_byline_missing':    '바이라인',   'D_ai_undisclosed':    'AI미표시',
  'E_sensational':       '선정성',     'F_ad_obstruct':       '광고방해',
  'G_tech_stability':    '기술안정성', 'H_ux_harm':           '이용자경험',
  'I_url_swap':          'URL바꿔치기','J_duplicate':         '중복기사',
  'K_main_news_abuse':   '주요뉴스오용','L_keyword_abuse':    '키워드남용',
  'M_category_mismatch': '카테고리위반','N_unlicensed':       '계약미포함',
  'O_copyright':         '저작권',     'P_unfair_profit':     '부당이익',
  'Q_paid_article':      '유가기사',   'R_commercial':        '광고상품',
}};

// 규정 공식 세부항목 정의 (num: 표시번호, match: 포함 문자열, null=catch-all, unimpl=미구현)
const ITEM_SUBITEMS = {{
  'C_byline_missing': [
    {{ num: 'C-1', label: '실재하지 않는 기자명 사용', score: 4, match: '__UNIMPL__', unimpl: true }},
    {{ num: 'C-2', label: '부서명 바이라인 사용',       score: 1, match: '부서명' }},
    {{ num: 'C-3', label: '기자·필진 식별정보 없음',    score: 1, match: null }},
  ],
  'L_keyword_abuse': [
    {{ num: 'L-1', label: '검색어 은닉 삽입',        score: 2,   match: '__UNIMPL__', unimpl: true }},
    {{ num: 'L-2', label: '키워드 반복·과도 삽입',   score: 1.5, match: null }},
  ],
  'R_commercial': [
    {{ num: 'R-1', label: '구매유도 목적 판매정보 노출',   score: 1,   match: '구매유도' }},
    {{ num: 'R-2', label: '담배·주류 등 법적 제한 품목',  score: 1,   match: '법적제한품목' }},
    {{ num: 'R-3', label: '기사 외 영역 광고 연결',       score: 1,   match: '__UNIMPL__', unimpl: true }},
    {{ num: 'R-4', label: '노골적 홍보(간접 노출)',        score: 0.5, match: '__UNIMPL__', unimpl: true }},
  ],
}};

function getSubitem(subitems, reason) {{
  for (const sub of subitems)
    if (!sub.unimpl && sub.match !== null && reason.includes(sub.match)) return sub;
  for (const sub of subitems)
    if (!sub.unimpl && sub.match === null) return sub;
  return null;
}}

let _matrix = null;
let itemSortKey = 'total';

function buildMatrix() {{
  const mat = {{}};
  for (const p of PRESS_LIST) {{
    const arts = VDATA[p.code] || [];
    const items = {{}};
    for (const k of ITEM_KEYS) items[k] = [];
    for (const art of arts) {{
      for (const tag of (art.tags || [])) {{
        for (const k of ITEM_KEYS) {{
          if (tag.startsWith(ITEM_TAG_PREFIX[k])) {{
            items[k].push(art);
            break;
          }}
        }}
      }}
    }}
    const total = ITEM_KEYS.reduce((s, k) => s + items[k].length, 0);
    if (total > 0) mat[p.code] = {{ name: p.name, items, total }};
  }}
  return mat;
}}

function cntClass(n) {{
  if (n === 0) return 'cnt0';
  if (n < 3)  return 'cnt1';
  if (n < 6)  return 'cnt3';
  return 'cnt6';
}}

function renderItemMatrix(sortKey) {{
  itemSortKey = sortKey || itemSortKey;
  if (!_matrix) _matrix = buildMatrix();

  let entries = Object.entries(_matrix);
  if (itemSortKey === 'total')
    entries.sort((a, b) => b[1].total - a[1].total);
  else
    entries.sort((a, b) => b[1].items[itemSortKey].length - a[1].items[itemSortKey].length);

  const thCols = ITEM_KEYS.map(k => {{
    const sub = ITEM_SUBTITLE[k] || '';
    if (ITEM_UNIMPL.has(k)) {{
      return `<th class="th-unimpl" title="${{ITEM_TAG_PREFIX[k]}}(미구현)">`
           + `${{ITEM_SHORT[k]}}<span class="th-subtitle">${{sub}}</span>`
           + `<span class="th-subtitle" style="color:#ccc">(미구현)</span></th>`;
    }}
    const active = itemSortKey === k ? ' th-sort-active' : '';
    return `<th class="th-sort${{active}}" onclick="renderItemMatrix('${{k}}')" title="${{ITEM_TAG_PREFIX[k]}}">`
         + `${{ITEM_SHORT[k]}}<span class="th-subtitle">${{sub}}</span></th>`;
  }}).join('');
  const totalActive = itemSortKey === 'total' ? ' th-sort-active' : '';

  const bodyRows = entries.map(([code, d], i) => {{
    const cells = ITEM_KEYS.map(k => {{
      if (ITEM_UNIMPL.has(k))
        return `<td><span class="m-cell cnt0" style="color:#e0e0e0">·</span></td>`;
      const n = d.items[k].length;
      return `<td><span class="m-cell ${{cntClass(n)}}">${{n > 0 ? n : '·'}}</span></td>`;
    }}).join('');
    return `
      <tr class="matrix-row" onclick="toggleItemDetail('${{code}}')">
        <td class="rank">${{i + 1}}</td>
        <td>${{esc(d.name)}}</td>
        ${{cells}}
        <td><strong>${{d.total}}</strong></td>
      </tr>
      <tr id="idetail-${{code}}" class="item-detail-row" style="display:none">
        <td colspan="${{ITEM_KEYS.length + 3}}">
          <div id="idetail-inner-${{code}}" class="item-detail-inner"></div>
        </td>
      </tr>`;
  }}).join('');

  document.getElementById('item-matrix').innerHTML = `
    <table class="matrix-table">
      <thead>
        <tr>
          <th style="width:40px">순위</th>
          <th>언론사</th>
          ${{thCols}}
          <th class="th-sort${{totalActive}}" onclick="renderItemMatrix('total')">합계</th>
        </tr>
      </thead>
      <tbody>${{bodyRows}}</tbody>
    </table>`;
}}

function toggleItemDetail(code) {{
  const row = document.getElementById('idetail-' + code);
  const open = row.style.display !== 'none';
  row.style.display = open ? 'none' : 'table-row';
  if (!open) renderItemDetail(code);
}}

function artRow(a) {{
  return `<div class="item-art">
    <a href="${{esc(a.url)}}" target="_blank" style="color:var(--text)">${{esc(a.title)}}</a>
    <span style="color:var(--sub);margin-left:6px;font-size:11px">${{esc((a.date||'').slice(0,10))}}</span>
  </div>`;
}}

function artList(gArts) {{
  const html = gArts.slice(0, 10).map(artRow).join('');
  const more = gArts.length > 10
    ? `<div style="font-size:11px;color:var(--sub);padding:2px 0">…외 ${{gArts.length - 10}}건</div>` : '';
  return html + more;
}}

function renderItemDetail(code) {{
  const d = _matrix[code];
  if (!d) return;

  const sections = ITEM_KEYS
    .filter(k => ITEM_UNIMPL.has(k) || d.items[k].length > 0)
    .map(k => {{
      const label = ITEM_TAG_PREFIX[k];

      // ① 전체 미구현 항목
      if (ITEM_UNIMPL.has(k)) {{
        return `<div class="item-section item-section-unimpl">
          <div class="item-section-hd unimpl-hd">${{esc(label)}} <span class="unimpl-tag">미구현</span></div>
        </div>`;
      }}

      const arts = d.items[k];

      // ② 규정 공식 세부항목이 정의된 경우 (C, L, R)
      if (ITEM_SUBITEMS[k]) {{
        const subitems = ITEM_SUBITEMS[k];
        const buckets  = {{}};
        for (const sub of subitems) buckets[sub.label] = [];

        for (const art of arts) {{
          const reason  = (art.reasons && art.reasons[k]) || '';
          const matched = getSubitem(subitems, reason);
          if (matched) buckets[matched.label].push(art);
          else {{ if (!buckets['기타']) buckets['기타'] = []; buckets['기타'].push(art); }}
        }}

        const groupHtml = subitems.map(sub => {{
          const numTag = sub.num ? `<span class="sub-num">${{esc(sub.num)}}</span> ` : '';
          if (sub.unimpl) {{
            return `<div class="reason-group reason-group-unimpl">
              <div class="reason-group-hd unimpl-hd">${{numTag}}${{esc(sub.label)}}
                <span class="score-hint">(${{sub.score}}점)</span>
                <span class="unimpl-tag">미구현</span>
              </div>
            </div>`;
          }}
          const gArts = buckets[sub.label] || [];
          return `<div class="reason-group">
            <div class="reason-group-hd">${{numTag}}${{esc(sub.label)}}
              <span class="score-hint">(${{sub.score}}점)</span>
              <span class="reason-cnt">${{gArts.length}}건</span>
            </div>
            ${{artList(gArts)}}
          </div>`;
        }}).join('');

        const extra = buckets['기타'] && buckets['기타'].length
          ? `<div class="reason-group"><div class="reason-group-hd">기타 <span class="reason-cnt">${{buckets['기타'].length}}건</span></div>${{artList(buckets['기타'])}}</div>` : '';

        return `<div class="item-section">
          <div class="item-section-hd">${{esc(label)}} (${{arts.length}}건)</div>
          ${{groupHtml}}${{extra}}
        </div>`;
      }}

      // ③ 세부항목 미정의 → reason 문자열 기준 그룹핑 (B, D, E, J, Q)
      const groups = {{}};
      for (const art of arts) {{
        const reason = (art.reasons && art.reasons[k]) || '기타';
        if (!groups[reason]) groups[reason] = [];
        groups[reason].push(art);
      }}
      const groupHtml = Object.entries(groups)
        .sort((a, b) => b[1].length - a[1].length)
        .map(([reason, gArts]) =>
          `<div class="reason-group">
            <div class="reason-group-hd">${{esc(reason)}} <span class="reason-cnt">${{gArts.length}}건</span></div>
            ${{artList(gArts)}}
          </div>`
        ).join('');

      return `<div class="item-section">
        <div class="item-section-hd">${{esc(label)}} (${{arts.length}}건)</div>
        ${{groupHtml}}
      </div>`;
    }}).join('');

  document.getElementById('idetail-inner-' + code).innerHTML =
    sections || '<div style="color:var(--sub)">위반 없음</div>';
}}

function renderDel(page) {{
  delPage = page;
  const filtered = delFilter === 0 ? DELETED
    : delFilter === 99 ? DELETED.filter(a => a.exclusive)
    : delFilter === 2 ? DELETED.filter(a => a.deleteType === 2 && !a.exclusive)
    : DELETED.filter(a => a.deleteType === delFilter);
  const total = filtered.length;
  const ctr = document.getElementById('del-container');
  if (total === 0) {{
    ctr.innerHTML = '<div class="no-del">해당 유형의 삭제 의심 기사가 없습니다.</div>';
    document.getElementById('del-pager').innerHTML = '';
    return;
  }}
  const totalPages = Math.max(1, Math.ceil(total / DEL_SIZE));
  page = Math.min(Math.max(1, page), totalPages);
  const slice = filtered.slice((page - 1) * DEL_SIZE, page * DEL_SIZE);
  ctr.innerHTML =
    `<div class="page-info">${{page}}/${{totalPages}} 페이지 &nbsp;(총 ${{total}}건)</div>` +
    slice.map(a => {{
      const tags = a.tags.map(t => `<span class="vio-tag">${{esc(t)}}</span>`).join('');
      const exclBadge = a.exclusive ? `<span class="excl-badge">단독</span>` : '';
      const aiBadge = a.aiScore >= 70 ? `<span class="ai-badge-danger">AI의심</span>`
                    : a.aiScore >= 50 ? `<span class="ai-badge-caution">AI의심</span>` : '';
      const typeBadge = TYPE_BADGE[a.deleteType] || '';
      const titleHtml = a.url
        ? `<a href="${{esc(a.url)}}" target="_blank" style="color:#c0392b">${{esc(a.title)}}</a>`
        : `<span>${{esc(a.title)}}</span>`;
      const sourceLink = a.sourceUrl
        ? `<span style="font-size:11px;color:#888;margin-left:8px;"><a href="${{esc(a.sourceUrl)}}" target="_blank" style="color:#888">원문보기</a></span>` : '';
      const bodyHtml = a.body ? `<div class="del-article-body">${{esc(a.body)}}</div>` : '';
      return `<div class="del-item">
        <div class="del-press">${{esc(a.press)}}</div>
        <div class="del-title">${{typeBadge}}${{exclBadge}}${{aiBadge}}${{titleHtml}}${{sourceLink}}</div>
        <div class="del-meta">기자: ${{esc(a.byline)}} &nbsp;|&nbsp; ${{esc(a.date)}} &nbsp;|&nbsp; 감점: ${{a.score.toFixed(1)}}점</div>
        <div>${{tags}}</div>
        ${{bodyHtml}}
      </div>`;
    }}).join('');
  const pager = document.getElementById('del-pager');
  if (totalPages <= 1) {{ pager.innerHTML = ''; return; }}
  const btn = (p, label, disabled, active) =>
    `<button onclick="renderDel(${{p}})" ${{disabled?'disabled':''}} class="${{active?'active':''}}">${{label}}</button>`;
  let html = btn(page-1,'‹',page===1,false);
  const pages = new Set([1,totalPages]);
  for (let p=Math.max(1,page-2);p<=Math.min(totalPages,page+2);p++) pages.add(p);
  Array.from(pages).sort((a,b)=>a-b).forEach(p => {{ html += btn(p,p,false,p===page); }});
  html += btn(page+1,'›',page===totalPages,false);
  pager.innerHTML = html;
}}

function esc(s) {{
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function toggleDetail(code) {{
  const row = document.getElementById('detail-' + code);
  const open = row.style.display !== 'none';
  row.style.display = open ? 'none' : 'table-row';
  if (!open) renderPage(code, curPage[code] || 1);
}}

function renderPage(code, page) {{
  curPage[code] = page;
  const arts = VDATA[code] || [];
  const total = arts.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  page = Math.min(Math.max(1, page), totalPages);

  const wrap    = document.getElementById('vdetail-' + code);
  const ctr     = wrap.querySelector('.vio-container');
  const pagerEl = wrap.querySelector('.pager');

  if (total === 0) {{
    ctr.innerHTML = '<p class="no-vio">위반 의심 기사 없음</p>';
    pagerEl.innerHTML = '';
    return;
  }}

  const slice = arts.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  ctr.innerHTML =
    `<div class="page-info">${{page}}/${{totalPages}} 페이지 &nbsp;(총 ${{total}}건)</div>` +
    slice.map(a => {{
      const tags    = a.tags.map(t => `<span class="vio-tag">${{esc(t)}}</span>`).join('');
      const viLines = a.vioText.map(l => `<div class="vio-text">${{esc(l)}}</div>`).join('');
      const exclBadge2 = a.exclusive ? `<span class="excl-badge">단독</span>` : '';
      const aiBadge2 = a.aiScore >= 70 ? `<span class="ai-badge-danger">AI의심</span>`
                     : a.aiScore >= 50 ? `<span class="ai-badge-caution">AI의심</span>` : '';
      return `<div class="vio-item">
        <div class="vio-title">${{exclBadge2}}${{aiBadge2}}<a href="${{esc(a.url)}}" target="_blank" style="color:inherit;text-decoration:none">${{esc(a.title)}}</a></div>
        <div class="vio-meta">기자: ${{esc(a.byline)}} &nbsp;|&nbsp; ${{esc(a.date)}} &nbsp;|&nbsp; 감점: ${{a.score.toFixed(1)}}점</div>
        <div>${{tags}}</div>${{viLines}}
      </div>`;
    }}).join('');

  pagerEl.innerHTML = totalPages <= 1 ? '' : buildPager(code, page, totalPages);
}}

function buildPager(code, cur, total) {{
  const btn = (p, label, disabled, active) =>
    `<button onclick="renderPage('${{code}}',${{p}})"
             ${{disabled ? 'disabled' : ''}}
             class="${{active ? 'active' : ''}}">${{label}}</button>`;

  let html = btn(cur - 1, '‹ 이전', cur === 1, false);

  // 슬라이딩 윈도우: 현재 ±2, 항상 첫·끝 페이지, 중간은 … 생략
  const pages = new Set([1, total]);
  for (let p = Math.max(1, cur - 2); p <= Math.min(total, cur + 2); p++) pages.add(p);
  const sorted = Array.from(pages).sort((a,b)=>a-b);

  let prev = 0;
  for (const p of sorted) {{
    if (prev && p - prev > 1) html += '<span class="pager-ellipsis">…</span>';
    html += btn(p, p, false, p === cur);
    prev = p;
  }}

  html += btn(cur + 1, '다음 ›', cur === total, false);
  return html;
}}

// 페이지 로드 시 삭제 의심 기사 탭 초기 렌더링
renderDel(1);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    rows, art_data, earliest = load_data()
    deleted = load_deleted()
    if not rows:
        print("DB에 데이터가 없습니다.")
    else:
        html = make_html(rows, art_data, earliest, deleted)
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"생성 완료: {OUT_FILE}  ({len(rows)}개 언론사)")
