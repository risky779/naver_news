"""
네이버 뉴스 품질 평가 모니터링 스크립트 (규칙 기반 — API 키 불필요)
규정: 네이버 뉴스 제휴 심사 및 운영 평가 규정 (2026.02.11)
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import io
from collections import Counter, defaultdict
from datetime import datetime, date, timedelta
from playwright.async_api import async_playwright

try:
    import requests as _requests
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
    _DATALAB_AVAILABLE = True
except ImportError:
    _DATALAB_AVAILABLE = False

def _setup_utf8_stdout():
    """Windows 터미널 UTF-8 출력 강제 (line_buffering=True: 줄 단위 즉시 flush)"""
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# ── 설정 ────────────────────────────────────────────────────────────────────
RANKING_FILE  = "press_ranking.json"
DB_FILE       = "naver_monitor.db"
TIER_CONFIG        = [(15.0, 50), (8.0, 20), (0.0, 10)]
HEADLESS           = True
MONITOR_START      = date(2026, 4, 1)
CANCEL_THRESHOLD   = 10.0   # 제14조 제10항: 24개월 누적 이 이상 → 제휴 해지 권고
CANCEL_WARNING     = 7.0    # 70% 도달 시 사전 경고


def load_press_ranking() -> list[dict]:
    """press_ranking.json 로드. 없으면 빈 목록 반환."""
    try:
        with open(RANKING_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"  [경고] {RANKING_FILE} 없음 — naver_wide_scan.py 먼저 실행하세요")
        return []


# ── DB (중복 감점 방지) ──────────────────────────────────────────────────────
def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            url             TEXT PRIMARY KEY,
            press_code      TEXT,
            press_name      TEXT,
            title           TEXT,
            article_date    TEXT,
            byline          TEXT,
            first_seen      TEXT,
            checks_json     TEXT,
            score           REAL,
            violation_text  TEXT
        )
    """)
    # 기존 DB에 컬럼이 없으면 추가
    cols = [row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()]
    if "violation_text" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN violation_text TEXT")
    if "body" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN body TEXT")
    conn.commit()


def db_has_article(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone() is not None


def get_cumulative_score_24m(conn: sqlite3.Connection, press_code: str) -> float:
    """24개월 이내 해당 언론사의 누적 부정 평가 점수 합산 (제14조 제10항)"""
    cutoff = (datetime.now() - timedelta(days=730)).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(score), 0.0) FROM articles WHERE press_code=? AND first_seen >= ?",
        (press_code, cutoff)
    ).fetchone()
    return float(row[0]) if row else 0.0


def save_to_db(conn: sqlite3.Connection, url: str, art: dict,
               press_code: str, press_name: str, checks: dict) -> None:
    # J항목은 배치마다 재계산하므로 저장하지 않음, violated=true 항목만 저장
    checks_to_save = {k: v for k, v in checks.items()
                      if k != "J_duplicate" and isinstance(v, dict) and v.get("violated")}
    score = sum(ITEM_WEIGHTS.get(k, 0) for k, v in checks_to_save.items()
                if isinstance(v, dict) and v.get("violated"))
    # 위반 항목별 triggering text 수집
    vt_lines = []
    for k, v in checks_to_save.items():
        if isinstance(v, dict) and v.get("violated") and v.get("text"):
            label = ITEM_LABELS.get(k, k)
            vt_lines.append(f"[{label}] {v['text']}")
    violation_text = "\n".join(vt_lines) if vt_lines else None
    conn.execute("""
        INSERT OR IGNORE INTO articles
            (url, press_code, press_name, title, article_date, byline,
             first_seen, checks_json, score, violation_text, body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (url, press_code, press_name,
          art.get("title", ""), art.get("date", ""), art.get("byline", ""),
          datetime.now().isoformat(),
          json.dumps(checks_to_save, ensure_ascii=False),
          score, violation_text, art.get("body", "")))
    conn.commit()

# 포토기사 제목 패턴 (L항목 예외 처리)
PHOTO_PATTERN = re.compile(r"\[포토|포토\]|\[사진\]|포토多이슈", re.IGNORECASE)


def get_quarter_range() -> tuple[date, date]:
    """현재 분기의 시작일~종료일 반환 (규정 기준: 분기 내 기사만 평가)"""
    today = date.today()
    q = (today.month - 1) // 3          # 0=1Q, 1=2Q, 2=3Q, 3=4Q
    q_start = date(today.year, q * 3 + 1, 1)
    q_end_month = q * 3 + 3
    if q_end_month == 12:
        q_end = date(today.year, 12, 31)
    else:
        q_end = date(today.year, q_end_month + 1, 1) - timedelta(days=1)
    return q_start, q_end


def parse_article_date(date_str: str) -> date | None:
    """기사 날짜 문자열에서 date 객체 추출 (예: '2026.05.03. 오전 11:14')"""
    m = re.search(r'(\d{4})\.(\d{2})\.(\d{2})', date_str)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


_q_start, QUARTER_END = get_quarter_range()
QUARTER_START = MONITOR_START if MONITOR_START else _q_start
# ────────────────────────────────────────────────────────────────────────────

ITEM_LABELS = {
    "B_clickbait":      "B.클릭베이트",
    "C_byline_missing": "C.바이라인 없음",
    "D_ai_undisclosed": "D.AI생성 미표시",
    "E_sensational":    "E.선정성",
    "J_duplicate":      "J.중복·유사기사 재전송",
    "L_keyword_abuse":  "L.키워드 반복남용",
    "Q_paid_article":   "Q.유가기사 전송",
    "R_commercial":     "R.광고성 상품정보 명시",
}

# 표.16 부정 평가 점수 (규정 기준)
# F항목(기사 보기 방해 광고)은 텍스트 분석으로 감지 불가 → 제외
ITEM_WEIGHTS = {
    "B_clickbait":      0.5,
    "C_byline_missing": 1.0,
    "D_ai_undisclosed": 0.5,
    "E_sensational":    1.0,
    "J_duplicate":      1.5,
    "L_keyword_abuse":  1.5,
    "Q_paid_article":   1.5,
    "R_commercial":     1.0,
}

# ── 규칙 사전 ────────────────────────────────────────────────────────────────

# Q. 유가기사: 명백한 상업적 홍보 신호 (공시 없이 2개 이상 존재 시 의심)
# 규정: 경제적 이해관계를 공시하지 않고 대가를 받은 기사를 전송
Q_PROMOTIONAL_SIGNALS = [
    "이벤트 참여", "구매하기", "할인 혜택", "특가", "기획전",
    "할인쿠폰", "구매 링크", "한정 수량", "선착순", "무료 증정", "사은품",
]
Q_DISCLOSURE_WORDS = [
    "협찬", "광고", " pr ", "(pr)", "[pr]", "제공", "후원",
    "스폰서", "브랜디드", "광고성", "유료광고", "유료기사",
    "협찬기사", "pr기사", "advertorial",
]

# R. 광고성 상품정보: 가격 패턴 + 구매 유도 단어 2개 이상 + 상품 신호
# 규정: 주된 목적이 상품/서비스 구매 유도임을 명시하지 않음
R_PRICE_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})*원|\d+만\s*원|\d+천\s*원")
R_CTA_WORDS = ["구매", "주문", "신청", "예약", "가입", "구독", "결제", "할인"]
R_PRODUCT_SIGNALS = ["제품", "상품", "출시", "판매", "정가", "정품", "모델명"]
# 하드뉴스 문맥(법원·노조·정치·사건)이면 R항 제외
R_NEWS_CONTEXT = [
    # 법률·사법
    "법원", "판결", "선고", "기소", "검찰", "경찰", "재판", "소송", "고소",
    "이행강제금", "가처분", "가압류", "강제집행", "손해배상",
    # 노동·사회
    "노조", "파업", "위원장", "단체협약", "노사",
    # 재난·사고
    "사망", "부상", "화재", "사고", "지진", "홍수",
    # 정치·행정
    "대통령", "국회", "정부", "장관", "의원", "선거",
    # 정책·복지 지원 (정부 지원금·보조금은 광고성 상품이 아님)
    "지원금", "보조금", "지원사업", "지원금액", "지원정책",
    "복지", "수당", "급여", "연금", "보험료",
    "지자체", "공모", "공공기관",
]

# B. 클릭베이트: 제목에 충분하지 않은 정보로 이용자 오해 유발 (규정 제11조 B항)
# "충분하지 않는 정보나 단어를 이용하여 이용자 오해를 유발" — 정보 은폐형 표현
# 경악·발칵 등 충격 표현은 E항(성적·폭력적 호기심 자극)으로 분류
CLICKBAIT_PATTERNS = [
    # "알고보니"가 제목 끝이거나 뒤에 모호한 감정어만 있을 때 = 정보 은폐형
    # "알고보니 포항시장 예비 후보" 처럼 구체적 사실이 따라오면 제외
    (r"알고\s*보니\s*(충격|반전|대박|경악|황당|소름|놀라움|사실|이유)?\s*[!?…]*\s*$",
     "알고보니(정보은폐)"),
    (r"뒤늦게\s*(밝혀|알려)",  "뒤늦게 반전"),
    (r"사실은\s*이랬다",       "사실은이랬다 패턴"),
    (r"(?:반전|놀라운)\s*근황", "반전 근황"),
]

# E. 선정성: 성적·폭력적 호기심 자극 표현 — 제목에 한해 검사 (규정 제11조 E항)
# "성적·폭력적 호기심을 자극하는 표현이 사용된 경우"
SENSATIONAL_TITLE_WORDS = [
    "성기", "유두", "유륜", "항문", "전라",
    "성행위", "성교", "성기구", "섹스",
    "토막살인", "엽기살인", "엽기적",
]
# "음모"는 '음모론/음모자/음모설' 등 정치·사회 용어와 구별 필요 → 별도 패턴으로 처리
_EUMMO_RE = re.compile(r"음모(?!론|자|설|론자)")  # 음모론·음모자·음모설 제외
SENSATIONAL_TITLE_PATTERNS = [
    (r"경악",                                          "경악 표현"),
    # "발칵": 정책·재정·사회 뉴스의 공분 표현과 구별 → 인물·사생활·연예 맥락과 결합 시만 탐지
    (r"발칵.{0,15}(열애|사생활|불륜|스캔들|폭로|충격|사망|자살|은퇴|탈퇴|임신|결혼|이혼)", "발칵+선정맥락"),
    (r"(열애|사생활|불륜|스캔들|폭로|충격|사망|자살|은퇴|탈퇴|임신|결혼|이혼).{0,15}발칵", "선정맥락+발칵"),
    (r"충격\s*(반전|폭로|고백|공개|근황|데뷔|은퇴)",   "충격+강조어 조합"),
]

# C. 바이라인: 부서명/팀명만 기재 → 1점 (개인 기자명 부재)
# 규정: 작성자 식별 정보 허위·부재 시 1점(부재)/4점(허위)
# 예외: 공동취재·특별취재팀·풀단 등 기한과 목적이 명확한 통합 바이라인은 적용 제외
C_EXEMPT_PATTERN = re.compile(
    r"공동\s*취재팀?|특별\s*취재팀?|풀단|Pool\s*Group",
    re.IGNORECASE
)
# AI 자동생성 기사 공시 문구 — 작성자=알고리즘임을 명시한 경우 C항 예외
ROBONEWS_PATTERN = re.compile(
    r"자동\s*생성\s*알고리즘|로봇\s*(기자|뉴스|저널리즘)|AI\s*(기자|뉴스)|알고리즘에\s*의해\s*(실시간으로\s*)?작성",
    re.IGNORECASE
)
DEPT_PATTERN = re.compile(
    r"(온라인|디지털|편집|인터넷|모바일|소셜|뉴미디어).*(팀|부|국|센터)|"
    r"(보도|기획)팀|기자단|편집국|미디어팀|뉴스룸|"
    r"봇$|"                                        # 로봇/자동생성 바이라인 (예: C-APT봇)
    r"^(KBS|YTN|MBC|SBS|JTBC|채널A|TV조선|MBN|C-APT)|"  # 방송사·기관명만 기재
    r"(Herald|Times|News|Tribune)\s+[a-z]{3,}$"   # 매체명+이메일ID (예: Korea Herald khnews)
)

# 개인 기자명으로 인정되는 바이라인 패턴 (C항 판단용)
# - 한국어 이름 2~5자 뒤에 공백·'('·'·'·'[' 또는 문자열 끝 (이메일 접미사 허용)
# - 영문 한국식 이름: Byun Hye-jin, Lee Hyun-sang (하이픈 포함 Given-name)
# - BY NAME 형식: 코리아헤럴드 스타일 (BY CHO MUN-GYU [...])
_BYLINE_PERSONAL_RE = re.compile(
    r"^[가-힣]{2,5}([\s(·\[]|$)|"
    r"^[A-Z][a-z]+\s+[A-Z][a-z]+-[a-z]+|"
    r"^BY\s+[A-Z]"
)

AI_GENERATION_WORDS   = ["ai가 작성", "ai가 생성", "인공지능이 작성"]
# AI 도구명은 단독 언급(인터뷰·기사 소재)과 구별하기 위해 생성 문맥 필요
AI_TOOL_GENERATION_RE = re.compile(
    r"(chatgpt|gpt[-\s]?\d|claude|gemini|copilot)\s*(가|로|을|를|이|으로)?\s*(작성|생성|제작|썼|쓴|쓰다|만든|만들)",
    re.IGNORECASE,
)
AI_DISCLOSURE_WORDS   = ["ai 활용", "ai 생성", "인공지능 활용", "[ai]", "(ai)", "ai기술", "생성형 ai"]

# L. 규정: "연속·반복적으로 과도하게" — 단순 언급과 구별하기 위해 높은 임계값 적용
KEYWORD_REPEAT_TITLE  = 3   # 제목 내 동일 단어 3회+ (2자 이상)
KEYWORD_REPEAT_BODY   = 20  # 본문 내 동일 단어 20회+ (2자 이상)
BODY_LEAD_CHARS       = 1000 # 본문 첫 N자 내 등장 단어 = 주제어/인명으로 간주, L항 제외

# L항 불용어: 검색 조작 목적과 무관한 일반 서술어·조사·부사 제외
L_STOPWORDS = {
    # 한국어 불용어 — 서술어
    "있다", "없다", "하다", "되다", "이다", "아니다", "같다", "보다",
    "받다", "주다", "가다", "오다", "나다", "들다", "알다", "모르다",
    "말하다", "밝히다", "전하다", "설명하다", "강조하다", "지적하다",
    # 구어체 서술어 활용형 (토론·방송 텍스트에서 반복)
    "있어", "없어", "있죠", "없죠", "있는", "없는", "있고", "없고",
    "하는", "하고", "했고", "했죠", "했어", "됩니다", "됩니까",
    # 접속·전환 표현 (담화 표지어)
    "위해", "통해", "대해", "관해", "따라", "위한", "관련", "대한",
    "이후", "이전", "현재", "최근", "지난", "다음", "이번", "당시",
    "모든", "이런", "이러한", "그런", "그러한", "이같은", "이같이",
    "또한", "하지만", "그러나", "그리고", "따라서", "때문", "만큼",
    "경우", "상황", "문제", "내용", "방법", "방식", "계획", "예정",
    # 구어체 접속·담화 표지 (방송·토론 텍스트에 빈번)
    "그런데", "그래서", "그러면", "그러니", "그러니까", "그래도",
    "지금", "아까", "이제", "아직", "벌써", "정말", "사실", "물론",
    "어떤", "무슨", "어떻게", "왜냐", "왜냐면", "그냥", "이렇게",
    "그렇게", "저렇게", "이렇게", "그리고는", "그러다", "결국", "다시",
    "때문에", "때문", "위해서", "통해서", "대해서",
    # 영어 불용어 (영문 기사 대응 — 소문자로만 등록, 체크 시 .lower() 적용)
    "the", "and", "that", "this", "with", "for", "are", "was", "were",
    "has", "have", "had", "not", "but", "from", "they", "will", "been",
    "its", "his", "her", "our", "their", "said", "also", "which", "who",
    "more", "than", "into", "when", "about", "would", "could", "should",
    "can", "may", "all", "one", "two", "new", "out", "any", "some",
    "an", "in", "on", "at", "by", "of", "to", "is", "it", "be", "as",
    # 대명사·접속사 (영문 기고문에서 자주 반복)
    "we", "me", "you", "she", "he", "us", "my", "your", "them", "her",
    "do", "did", "does", "been", "were", "what", "how", "why", "where",
    "there", "then", "here", "these", "those", "such", "just", "even",
    "like", "after", "before", "while", "since", "if", "so", "up", "or",
    "am", "do", "go", "no", "yet", "too", "very", "each", "both", "few",
    # 단위 약어 (키·거리·무게·금액 등 반복 표기는 키워드 남용 아님)
    "cm", "mm", "km", "mg", "kg", "ml", "kcal", "GHz", "MHz",
    "만원", "억원", "조원", "만달러", "억달러",
}


# ── 네이버 DataLab 트렌드 검증 (L항) ────────────────────────────────────────
TREND_CACHE_FILE  = "naver_trend_cache.json"
TREND_THRESHOLD   = 10.0  # DataLab ratio 이 이상이면 실제 인기 검색어로 판단 (100점 기준)

_trend_cache: dict = {}   # {keyword: {"ratio": float, "date": "YYYY-MM-DD"}}


def _load_trend_cache() -> None:
    global _trend_cache
    if _trend_cache:
        return
    try:
        with open(TREND_CACHE_FILE, encoding="utf-8") as f:
            _trend_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _trend_cache = {}


def _save_trend_cache() -> None:
    with open(TREND_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(_trend_cache, f, ensure_ascii=False)


def fetch_keyword_trends(keywords: list) -> dict:
    """
    키워드 목록의 DataLab 트렌드 ratio를 반환. {keyword: max_ratio}
    API 미사용 가능 시 또는 오류 시 빈 dict 반환.
    캐시: 당일 조회 결과를 TREND_CACHE_FILE에 저장.
    """
    if not _DATALAB_AVAILABLE or not keywords:
        return {}

    client_id     = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        return {}

    _load_trend_cache()
    today     = date.today().isoformat()
    to_fetch  = [kw for kw in keywords if _trend_cache.get(kw, {}).get("date") != today]

    # API: 최대 5개 keywordGroups per request
    BATCH = 5
    end_date   = today
    start_date = (date.today() - timedelta(days=6)).isoformat()  # 최근 7일
    headers = {
        "X-Naver-Client-Id":     client_id,
        "X-Naver-Client-Secret": client_secret,
        "Content-Type":          "application/json",
    }

    for i in range(0, len(to_fetch), BATCH):
        batch = to_fetch[i:i + BATCH]
        body  = {
            "startDate":    start_date,
            "endDate":      end_date,
            "timeUnit":     "date",
            "keywordGroups": [{"groupName": kw, "keywords": [kw]} for kw in batch],
        }
        try:
            r = _requests.post(
                "https://openapi.naver.com/v1/datalab/search",
                headers=headers,
                json=body,
                timeout=10,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            for result in data.get("results", []):
                kw    = result["title"]
                ratio = max((p["ratio"] for p in result.get("data", [])), default=0.0)
                _trend_cache[kw] = {"ratio": ratio, "date": today}
        except Exception:
            pass

    _save_trend_cache()

    return {kw: _trend_cache[kw]["ratio"] for kw in keywords if kw in _trend_cache}


# ── 기사 목록 수집 ───────────────────────────────────────────────────────────
async def get_article_list(page, press_code: str, max_count: int = 30) -> list[dict]:
    url = f"https://news.naver.com/main/list.naver?mode=LPOD&mid=sec&oid={press_code}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)

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
    await page.wait_for_timeout(1500)

    async def text(*sels: str) -> str:
        for sel in sels:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                if t:
                    return t
        return ""

    title    = await text("#title_area span", ".media_end_head_headline span")
    body     = await text("#newsct_article", "#articeBody", ".go_trans._article_content")
    byline_r = await text(".media_end_head_journalist_name", ".byline_s", ".byline", ".journalist_name")
    category = await text(".media_end_categorize_item", ".Nnews_category")
    date_str = await text(".media_end_head_info_datestamp_time", "._article_date_time")

    byline = re.sub(r"\s*(기자|특파원|기자\s*=).*|@\S+|\s+", " ", byline_r).strip()

    # 바이라인 태그 없을 때 본문 앞/뒤에서 폴백 추출
    if not byline and body:
        all_lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
        # 마지막 5줄: "홍길동 기자", "MBN 문화부 이상주기자"
        for line in reversed(all_lines[-5:]):
            m = re.search(r"([가-힣]{2,6})\s*(기자|특파원)$", line)
            if m:
                byline = m.group(1)
                break
        # 마지막 5줄: "김동기 청담 총괄셰프 paychey@naver.com" (이름+직책+이메일)
        if not byline:
            for line in reversed(all_lines[-5:]):
                m = re.match(r"^([가-힣]{2,4}[\s가-힣A-Za-z·]+?)\s+\S+@\S+$", line)
                if m:
                    byline = m.group(1).strip()
                    break
        # 마지막 3줄 + 첫 3줄: 외부 기고 서명 "김만기 KAIST 교수", "[신율 명지대 교수]"
        if not byline:
            EXPERT_TITLE = re.compile(
                r"교수|원장|소장|대표|위원장|이사장|이사|연구원|연구위원|센터장|회장|처장|박사|전문위원|논설위원|칼럼니스트|장관|차관|청장|국장|부장|팀장|위원"
            )
            # 대괄호 허용: "[신율 명지대 교수]" → ^[\[]?[가-힣]
            _expert_re = re.compile(r"^\[?[가-힣]{2,4}[\s·]")
            for line in list(reversed(all_lines[-3:])) + all_lines[:3]:
                if EXPERT_TITLE.search(line) and _expert_re.match(line):
                    byline = re.sub(r"[\[\]]", "", line).strip()
                    break
        # 첫 3줄: 영문 기고자명 ("Lee Hyun-sang") + 다음 줄에 "author" 언급
        if not byline and len(all_lines) >= 2:
            for i, line in enumerate(all_lines[:3]):
                next_line = all_lines[i + 1] if i + 1 < len(all_lines) else ""
                if re.match(r"^[A-Z][a-z]+ [A-Z][a-z\-]+$", line) and \
                   re.search(r"author|columnist|writer|reporter|correspondent", next_line, re.I):
                    byline = line
                    break

    return {"title": title, "body": body, "byline": byline,
            "category": category, "date": date_str}


def extract_context(text: str, keyword: str, window: int = 60) -> str:
    """keyword 주변 window자를 잘라 반환 (문장 단편)"""
    idx = text.find(keyword)
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end   = min(len(text), idx + len(keyword) + window)
    snippet = text[start:end].strip()
    prefix  = "…" if start > 0 else ""
    suffix  = "…" if end < len(text) else ""
    return prefix + snippet + suffix


# ── 규칙 기반 품질 분석 ──────────────────────────────────────────────────────
def analyze_rules(article: dict) -> dict:
    title    = article.get("title", "")
    body     = article.get("body",  "")
    byline   = article.get("byline","")
    body_l   = body.lower()
    is_photo = bool(PHOTO_PATTERN.search(title))

    results = {}

    # B. 클릭베이트 (0.5점) — 과장·왜곡으로 이용자 오인 유발 (규정 제11조 B항)
    # 정보 은폐형 표현 패턴 (알고보니, 반전 근황 등)
    hits = [(label, pat) for pat, label in CLICKBAIT_PATTERNS if re.search(pat, title)]
    results["B_clickbait"] = {
        "violated": bool(hits),
        "reason": f"패턴 감지: {', '.join(l for l,_ in hits[:3])}" if hits else "정상",
        "text":   title if hits else "",
    }

    # C. 바이라인 (1점) — 작성자 식별 정보 부재 (규정 제11조 C항)
    # 예외: 속보 기사, 공동취재팀·특별취재팀·풀단, AI 자동생성 공시 기사
    is_breaking  = bool(BREAKING_PATTERN.search(title))
    is_robonews  = bool(ROBONEWS_PATTERN.search(body))
    c_absent     = not byline or len(byline) < 2
    c_exempt     = bool(byline) and bool(C_EXEMPT_PATTERN.search(byline))
    # DEPT_PATTERN을 먼저 평가: 부서명/방송사명이 개인명 패턴보다 우선
    c_dept       = bool(byline) and not c_exempt and bool(DEPT_PATTERN.search(byline))
    has_personal = bool(byline) and not c_dept and bool(_BYLINE_PERSONAL_RE.search(byline))
    c_bad        = (c_absent or c_dept) and not is_breaking and not is_robonews
    results["C_byline_missing"] = {
        "violated": c_bad,
        "reason": ("기자명 없음" if c_absent
                   else f"부서명만 기재(개인 식별 불가): {byline}" if c_dept
                   else f"기자: {byline}"),
        "text":   byline if c_dept else ("(바이라인 없음)" if c_absent else ""),
    }

    # D. AI 미표시 (0.5점) — AI 생성·활용 표시 의무 위반 (규정 제11조 D항)
    _tool_m   = AI_TOOL_GENERATION_RE.search(body)
    has_ai    = any(w in body_l for w in AI_GENERATION_WORDS) or bool(_tool_m)
    has_disc  = any(w in body_l for w in AI_DISCLOSURE_WORDS)
    ai_kw     = next((w for w in AI_GENERATION_WORDS if w in body_l), _tool_m.group() if _tool_m else "")
    results["D_ai_undisclosed"] = {
        "violated": has_ai and not has_disc,
        "reason":  "AI 생성 정황 있으나 표시 없음" if (has_ai and not has_disc) else "정상",
        "text":    extract_context(body, ai_kw) if (has_ai and not has_disc) else "",
    }

    # E. 선정성 (1점) — 성적·폭력적 호기심 자극 표현 (규정 제11조 E항)
    hit_words    = [w for w in SENSATIONAL_TITLE_WORDS if w in title]
    if _EUMMO_RE.search(title):
        hit_words.append("음모")
    hit_patterns = [(label, pat) for pat, label in SENSATIONAL_TITLE_PATTERNS if re.search(pat, title)]
    e_violated   = bool(hit_words or hit_patterns)
    e_reasons    = ([f"선정어: {', '.join(hit_words[:4])}"] if hit_words else []) + \
                   ([f"충격패턴: {', '.join(l for l,_ in hit_patterns[:2])}"] if hit_patterns else [])
    results["E_sensational"] = {
        "violated": e_violated,
        "reason":   " / ".join(e_reasons) if e_violated else "정상",
        "text":     title if e_violated else "",
    }

    # L. 키워드 남용 (1.5점) — 연속·반복적으로 과도하게 특정 검색어 남용 (규정 제11조 L항)
    t_words       = [w for w in re.findall(r"[가-힣a-zA-Z]{2,}", title) if w.lower() not in L_STOPWORDS]
    title_wordset = set(t_words)
    lead_wordset  = set(re.findall(r"[가-힣a-zA-Z]{2,}", body[:BODY_LEAD_CHARS]))
    # 본문 전체 최다 등장 상위 10단어도 주제어로 추가 (제목·리드에 없어도 핵심 주제 인식)
    _body_freq = Counter(w for w in re.findall(r"[가-힣a-zA-Z]{2,}", body) if w.lower() not in L_STOPWORDS)
    lead_wordset |= {w for w, _ in _body_freq.most_common(10)}
    # 양방향 prefix 매칭으로 조사 결합 처리
    # 정방향: 본문단어가 토픽단어+조사 (교사 → 교사는)
    # 역방향: 본문단어가 토픽단어의 어근 (검찰 ⊂ 검찰이/검찰에)
    def is_topic_word(w: str) -> bool:
        if w in title_wordset or w in lead_wordset:
            return True
        for tw in title_wordset | lead_wordset:
            if len(tw) >= 2 and w.startswith(tw) and len(w) - len(tw) <= 2:
                return True
            if len(w) >= 2 and tw.startswith(w) and len(tw) - len(w) <= 2:
                return True
        return False
    # 제목 반복 후보: lead_wordset에 있으면 기사 주제어이므로 제외 (수사적 강조 표현 오탐 방지)
    t_abused_raw = [w for w, c in Counter(t_words).items()
                    if c >= KEYWORD_REPEAT_TITLE and w not in lead_wordset]
    b_abused_raw = [] if is_photo else [
        w for w, c in Counter(
            w for w in re.findall(r"[가-힣a-zA-Z]{2,}", body)
            if w.lower() not in L_STOPWORDS and not is_topic_word(w)
        ).items()
        if c >= KEYWORD_REPEAT_BODY
    ][:5]

    # DataLab 트렌드 검증: 반복 단어가 실제 인기 검색어일 때만 L항 적용
    # ratio < TREND_THRESHOLD 이면 일반 주제어이므로 제외
    all_candidates = list(set(t_abused_raw + b_abused_raw))
    if all_candidates:
        trends = fetch_keyword_trends(all_candidates)
        t_abused = [w for w in t_abused_raw if trends.get(w, 0.0) >= TREND_THRESHOLD]
        b_abused = [w for w in b_abused_raw if trends.get(w, 0.0) >= TREND_THRESHOLD]
    else:
        t_abused = t_abused_raw
        b_abused = b_abused_raw

    l_reasons = []
    l_texts   = []
    if t_abused:
        l_reasons.append(f"제목 반복({KEYWORD_REPEAT_TITLE}회+): {t_abused}")
        l_texts.append(f"제목: {title}")
    if b_abused:
        l_reasons.append(f"본문 반복({KEYWORD_REPEAT_BODY}회+): {b_abused}")
        l_texts.append(extract_context(body, b_abused[0]))
    results["L_keyword_abuse"] = {
        "violated": bool(t_abused or b_abused),
        "reason":   " / ".join(l_reasons) if l_reasons else "정상",
        "text":     " | ".join(l_texts) if l_texts else "",
    }

    # Q. 유가기사 전송 (1.5점) — 경제적 이해관계 미공시 (규정 제11조 Q항)
    body_lower  = body.lower()
    promo_hits  = [s for s in Q_PROMOTIONAL_SIGNALS if s in body]
    has_q_disc  = any(d in body_lower for d in Q_DISCLOSURE_WORDS)
    q_violated  = len(promo_hits) >= 2 and not has_q_disc
    results["Q_paid_article"] = {
        "violated": q_violated,
        "reason":  (f"홍보신호 {len(promo_hits)}개 있으나 공시 없음: {promo_hits[:3]}"
                    if q_violated else "정상"),
        "text":    extract_context(body, promo_hits[0]) if q_violated else "",
    }

    # R. 광고성 상품정보 (1점) — 구매 유도 기사임을 미명시 (규정 제11조 R항)
    has_price  = bool(R_PRICE_PATTERN.search(body))
    cta_hits   = [w for w in R_CTA_WORDS if w in body]
    prod_hits  = [w for w in R_PRODUCT_SIGNALS if w in body]
    has_r_disc = any(d in body_lower for d in Q_DISCLOSURE_WORDS)
    is_hard_news = any(w in body for w in R_NEWS_CONTEXT)
    r_violated = has_price and len(cta_hits) >= 2 and bool(prod_hits) and not has_r_disc and not is_hard_news
    r_price_m  = R_PRICE_PATTERN.search(body)
    results["R_commercial"] = {
        "violated": r_violated,
        "reason":  (f"가격노출+구매유도({cta_hits[:2]})+상품정보({prod_hits[:2]})"
                    if r_violated else "정상"),
        "text":    extract_context(body, r_price_m.group()) if r_violated and r_price_m else "",
    }

    return results


def _title_words(title: str) -> set:
    return set(re.findall(r"[가-힣a-zA-Z]{2,}", title))


def get_recent_db_titles(conn: sqlite3.Connection, press_code: str, days: int = 30) -> list[str]:
    """DB에서 해당 언론사의 최근 N일 기사 제목 목록 반환 (J항목 중복 비교용)"""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT title FROM articles WHERE press_code=? AND first_seen >= ?",
        (press_code, cutoff)
    ).fetchall()
    return [row[0] for row in rows if row[0]]


BREAKING_PATTERN = re.compile(r"^\[?속보\]?", re.IGNORECASE)


def check_duplicate_articles(results: list[dict],
                              db_conn: sqlite3.Connection = None,
                              press_code: str = None) -> None:
    """J. 중복·유사 기사 재전송 (1.5점) — 배치 내 제목 Jaccard ≥ 0.5
    예외: 속보성 후속 보도(사실이 추가·변경된 경우) — [속보] 태그 기사 제외
    """
    titles_words = [_title_words(r["article"].get("title", "")) for r in results]

    for i, r in enumerate(results):
        title_i = r["article"].get("title", "")
        # 속보성 후속 보도 예외 (규정 J항: 사실이 추가·변경된 경우 제외)
        if BREAKING_PATTERN.search(title_i):
            r["checks"]["J_duplicate"] = {"violated": False, "reason": "속보 기사 예외"}
            continue
        best_sim   = 0.0
        best_label = ""
        wi = titles_words[i]
        if not wi:
            r["checks"]["J_duplicate"] = {"violated": False, "reason": "정상"}
            continue

        # 배치 내 비교만 수행
        for j, wj in enumerate(titles_words):
            if i == j or not wj:
                continue
            sim = len(wi & wj) / len(wi | wj)
            if sim > best_sim:
                best_sim   = sim
                best_label = results[j]["article"].get("title", "")[:30]

        if best_sim >= 0.5:
            r["checks"]["J_duplicate"] = {
                "violated": True,
                "reason": f"유사기사 감지(유사도 {best_sim:.2f}): '{best_label}'",
            }
        else:
            r["checks"]["J_duplicate"] = {"violated": False, "reason": "정상"}


# ── 리포트: 언론사별 상세 ────────────────────────────────────────────────────
def article_score(checks: dict) -> float:
    return sum(ITEM_WEIGHTS.get(k, 0) for k, v in checks.items()
               if isinstance(v, dict) and v.get("violated"))


def print_press_detail(press_name: str, press_code: str, all_results: list[dict],
                       cum_score: float = 0.0):
    line = "-" * 65
    print(f"\n{'='*65}")
    print(f"  [{press_name}]  (코드: {press_code})")
    # 24개월 누적 점수 경고
    if cum_score >= CANCEL_THRESHOLD:
        print(f"  ⚠️  [제휴해지 위험] 24개월 누적 {cum_score:.1f}점 — 10점 초과 (제14조 제10항)")
    elif cum_score >= CANCEL_WARNING:
        print(f"  ⚡ [경고] 24개월 누적 {cum_score:.1f}점 — 해지 기준({CANCEL_THRESHOLD:.0f}점)의 {cum_score/CANCEL_THRESHOLD*100:.0f}% 도달")
    else:
        print(f"  24개월 누적 점수: {cum_score:.1f}점 / {CANCEL_THRESHOLD:.0f}점")
    print(f"{'='*65}")

    flagged = 0
    total_score = 0.0
    for r in all_results:
        art   = r["article"]
        chks  = r["checks"]
        items = [k for k, v in chks.items() if isinstance(v, dict) and v.get("violated")]
        score = article_score(chks)
        total_score += score

        if not items:
            print(f"  [OK] {art['title'][:52]}")
            continue

        flagged += 1
        print(f"\n  [!!] {art['title'][:52]}  ({score:.1f}점)")
        print(f"       기자: {art.get('byline') or '(없음)'}  |  {art.get('date','')}")
        for k in items:
            print(f"       ▸ {ITEM_LABELS.get(k,k)}({ITEM_WEIGHTS.get(k,0)}점): {chks[k]['reason']}")

    clean = len(all_results) - flagged
    avg   = total_score / len(all_results) if all_results else 0.0
    print(f"\n  {line}")
    print(f"  분석 {len(all_results)}건  |  위반의심 {flagged}건  |  정상 {clean}건")
    print(f"  누적 감점 {total_score:.1f}점  |  기사당 평균 {avg:.2f}점")


# ── 리포트: 전체 비교표 ──────────────────────────────────────────────────────
def print_summary_table(press_summary: list[dict]):
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    keys = list(ITEM_LABELS.keys())

    # 컬럼 너비 계산 (감점 컬럼 추가)
    name_w   = max(len(d["name"]) for d in press_summary) + 2
    col_w    = 6
    score_w  = 7
    total_w  = name_w + col_w * (len(keys) + 2) + score_w + 3

    header_items = [ITEM_LABELS[k].split(".")[0] for k in keys]  # B, C, D, E, F, L

    print(f"\n\n{'#'*total_w}")
    print(f"  네이버 뉴스 품질 모니터링 — 언론사 비교 요약")
    print(f"  분석 시각: {now}")
    print(f"  배점: B=0.5 C=1.0 D=0.5 E=1.0 J=1.5 L=1.5 Q=1.5 R=1.0 (규정 표.16 기준)")
    print(f"{'#'*total_w}\n")

    # 헤더
    hdr = (f"  {'언론사':<{name_w}}"
           + "".join(f"{h:>{col_w}}" for h in header_items)
           + f"{'위반':>{col_w}}{'총기사':>{col_w}}{'감점합':>{score_w}}")
    print(hdr)
    print("  " + "-" * (total_w - 2))

    for d in press_summary:
        wscore = sum(d["counts"].get(k, 0) * ITEM_WEIGHTS.get(k, 0) for k in keys)
        row  = f"  {d['name']:<{name_w}}"
        row += "".join(f"{d['counts'].get(k,0):>{col_w}}" for k in keys)
        row += f"{d['flagged']:>{col_w}}{d['total']:>{col_w}}{wscore:>{score_w}.1f}"
        print(row)

    print("  " + "-" * (total_w - 2))

    # 합계
    totals = {k: sum(d["counts"].get(k, 0) for d in press_summary) for k in keys}
    total_wscore = sum(totals[k] * ITEM_WEIGHTS.get(k, 0) for k in keys)
    row  = f"  {'합계':<{name_w}}"
    row += "".join(f"{totals[k]:>{col_w}}" for k in keys)
    row += f"{sum(d['flagged'] for d in press_summary):>{col_w}}"
    row += f"{sum(d['total'] for d in press_summary):>{col_w}}"
    row += f"{total_wscore:>{score_w}.1f}"
    print(row)

    print(f"\n  항목: B=클릭베이트 C=바이라인없음 D=AI미표시 E=선정성 J=중복기사 L=키워드남용 Q=유가기사 R=광고성상품정보")
    print(f"  감점합: 해당 언론사 기사들의 가중 점수 합산 (높을수록 규정 위반 심각)")
    print(f"{'#'*total_w}\n")


def print_cancel_risk_summary(risk_list: list[dict]):
    """제휴 취소 위험·경고 언론사 요약 (제14조 제10항)"""
    danger  = [d for d in risk_list if d["cum_score"] >= CANCEL_THRESHOLD]
    warning = [d for d in risk_list if CANCEL_WARNING <= d["cum_score"] < CANCEL_THRESHOLD]

    if not danger and not warning:
        print("\n  [제휴 취소 위험 없음] 모든 언론사 24개월 누적 점수 정상 범위")
        return

    print(f"\n\n{'#'*65}")
    print(f"  제14조 제10항 — 24개월 누적 부정 평가 점수 현황")
    print(f"  해지 기준: {CANCEL_THRESHOLD:.0f}점 이상  |  경고 기준: {CANCEL_WARNING:.0f}점 이상")
    print(f"{'#'*65}")

    if danger:
        print(f"\n  ⚠️  [제휴해지 권고 대상] {len(danger)}개사")
        print(f"  {'언론사':<20} {'24개월누적':>10} {'상태':>12}")
        print(f"  {'-'*44}")
        for d in sorted(danger, key=lambda x: -x["cum_score"]):
            print(f"  {d['name']:<20} {d['cum_score']:>10.1f}점  {'→ 해지 권고':>10}")

    if warning:
        print(f"\n  ⚡ [경고 대상] {len(warning)}개사")
        print(f"  {'언론사':<20} {'24개월누적':>10} {'잔여여유':>10}")
        print(f"  {'-'*44}")
        for d in sorted(warning, key=lambda x: -x["cum_score"]):
            remain = CANCEL_THRESHOLD - d["cum_score"]
            print(f"  {d['name']:<20} {d['cum_score']:>10.1f}점  {remain:>8.1f}점 여유")

    print(f"\n{'#'*65}\n")


# ── 메인 ─────────────────────────────────────────────────────────────────────
async def main():
    press_ranking = load_press_ranking()
    if not press_ranking:
        return

    print(f"\n{'='*65}")
    print(f"  네이버 뉴스 품질 모니터링  |  언론사 {len(press_ranking)}곳 비교")
    print(f"  평가 기간: {QUARTER_START} ~ {QUARTER_END}  (현재 분기)")
    print(f"  계층: 감점 15↑=50건 / 8↑=20건 / 기타=10건")
    print(f"{'='*65}")

    all_press_results = []   # [{name, code, results}, ...]
    press_summary     = []   # 요약표용
    risk_list         = []   # 24개월 누적 점수 위험 목록

    db_conn = sqlite3.connect(DB_FILE)
    init_db(db_conn)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        for idx, entry in enumerate(press_ranking, 1):
            code        = entry["code"]
            name        = entry["name"]
            max_articles = entry["max_articles"]
            fetch_limit  = max(30, max_articles * 3)

            print(f"\n[{idx}/{len(press_ranking)}] {name} (코드:{code}) — 최대 {max_articles}건 (감점 {entry['wscore']:.1f})")
            try:
                candidates = await get_article_list(page, code, max_count=fetch_limit)
                print(f"  후보 {len(candidates)}건 발견 → 분기 필터 적용 ({QUARTER_START}~{QUARTER_END})")
            except Exception as e:
                print(f"  목록 수집 실패: {e}")
                continue

            results = []
            skipped_date = 0
            skipped_db   = 0
            for i, art in enumerate(candidates, 1):
                if len(results) >= max_articles:
                    break
                url = art["url"]
                if db_has_article(db_conn, url):
                    skipped_db += 1
                    continue
                try:
                    content = await get_article_content(page, url)
                    art.update(content)
                    # 제목 없으면 동영상 전용 또는 크롤링 실패 → 건너뜀
                    if not art.get("title", "").strip():
                        continue
                    # 분기 내 기사 필터
                    art_date = parse_article_date(art.get("date", ""))
                    if art_date and not (QUARTER_START <= art_date <= QUARTER_END):
                        skipped_date += 1
                        continue
                    checks = analyze_rules(art)
                    save_to_db(db_conn, url, art, code, name, checks)
                    results.append({"article": art, "checks": checks})
                except Exception as e:
                    print(f"    [{i}] 오류: {e}")

            print(f"  분기 내 {len(results)}건 확보 "
                  f"(분기 외 {skipped_date}건 / DB중복 {skipped_db}건 제외)")

            # J항목: 배치 내 + DB 30일 이내 중복·유사 기사 감지
            if len(results) >= 2:
                check_duplicate_articles(results, db_conn=db_conn, press_code=code)
            else:
                for r in results:
                    r["checks"]["J_duplicate"] = {"violated": False, "reason": "단일기사(비교불가)"}

            all_press_results.append({"name": name, "code": code, "results": results})

            # 요약 집계
            counts  = defaultdict(int)
            flagged = 0
            for r in results:
                items = [k for k, v in r["checks"].items()
                         if isinstance(v, dict) and v.get("violated")]
                if items:
                    flagged += 1
                for k in items:
                    counts[k] += 1
            press_summary.append({"name": name, "code": code,
                                   "counts": dict(counts),
                                   "flagged": flagged, "total": len(results)})

            # 24개월 누적 점수 (제14조 제10항)
            cum_score = get_cumulative_score_24m(db_conn, code)
            risk_list.append({"name": name, "code": code, "cum_score": cum_score})
            risk_tag = ""
            if cum_score >= CANCEL_THRESHOLD:
                risk_tag = f"  ⚠️ 24개월누적 {cum_score:.1f}점 — 제휴해지 위험!"
            elif cum_score >= CANCEL_WARNING:
                risk_tag = f"  ⚡ 24개월누적 {cum_score:.1f}점 — 경고"
            print(f"  완료 — 위반의심 {flagged}/{len(results)}건{risk_tag}")

        await browser.close()

    db_conn.close()

    # 언론사별 상세 출력
    risk_map = {d["code"]: d["cum_score"] for d in risk_list}
    for pr in all_press_results:
        print_press_detail(pr["name"], pr["code"], pr["results"],
                           cum_score=risk_map.get(pr["code"], 0.0))

    # 비교 요약표
    print_summary_table(press_summary)

    # 제휴 취소 위험 요약
    print_cancel_risk_summary(risk_list)

    # JSON 저장
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = f"monitoring_multi_{ts}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_press_results, f, ensure_ascii=False, indent=2)
    print(f"  상세 결과 저장: {out}")


if __name__ == "__main__":
    _setup_utf8_stdout()
    asyncio.run(main())
