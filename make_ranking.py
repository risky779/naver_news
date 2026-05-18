import sqlite3, sys
from datetime import datetime, timedelta

conn = sqlite3.connect('C:/Users/admin/naver_monitor.db')
cutoff = (datetime.now() - timedelta(days=730)).isoformat()
rows = conn.execute(
    "SELECT press_name, press_code, COALESCE(SUM(score),0), COUNT(*) "
    "FROM articles WHERE first_seen >= ? "
    "GROUP BY press_code ORDER BY 3 DESC",
    (cutoff,)
).fetchall()

with open('C:/Users/admin/ranking_by_score.txt', 'w', encoding='utf-8') as f:
    f.write(f"{'순위':<4} {'언론사':<22} {'24개월누적':>10} {'기사수':>6}  상태\n")
    f.write('-' * 62 + '\n')
    for i, (name, code, score, cnt) in enumerate(rows, 1):
        if score >= 10.0:
            status = 'X 해지권고'
        elif score >= 7.0:
            status = '! 경고'
        else:
            status = '정상'
        f.write(f"{i:<4} {name:<22} {score:>9.1f}점 {cnt:>5}건  {status}\n")
    f.write(f"\n총 {len(rows)}개사\n")

print("done")
