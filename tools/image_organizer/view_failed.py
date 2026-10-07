"""查看失败文件"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = sqlite3.connect(r'<project_root>\workspace\image_organize\files.db')
rows = conn.execute(
    'SELECT source_path, status, error FROM files WHERE status="failed" OR status="florence_done"'
).fetchall()
for r in rows:
    print(f"\n状态: {r[1]}")
    print(f"路径: {r[0]}")
    print(f"错误: {r[2]}")
