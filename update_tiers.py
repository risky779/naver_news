import json, sqlite3
from datetime import datetime, timedelta

conn = sqlite3.connect('C:/Users/admin/naver_monitor.db')
cutoff = (datetime.now() - timedelta(days=730)).isoformat()
scores = {row[0]: row[1] for row in conn.execute(
    'SELECT press_code, COALESCE(SUM(score),0) FROM articles WHERE first_seen >= ? GROUP BY press_code',
    (cutoff,)
).fetchall()}
conn.close()

TIER_CONFIG = [(15.0, 50), (8.0, 20), (0.0, 10)]

def get_tier(s):
    for threshold, count in TIER_CONFIG:
        if s >= threshold:
            return count
    return 10

data = json.load(open('C:/Users/admin/press_ranking.json', encoding='utf-8'))
updated = 0
for entry in data:
    code = entry['code']
    s = scores.get(code, 0.0)
    new_max = get_tier(s)
    old_max = entry.get('max_articles', 10)
    if new_max != old_max:
        name = entry['name']
        print(f'  {name}: {old_max}건 -> {new_max}건 (누적 {s:.1f}점)')
        entry['max_articles'] = new_max
        updated += 1

json.dump(data, open('C:/Users/admin/press_ranking.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'완료: {updated}개사 티어 갱신')
