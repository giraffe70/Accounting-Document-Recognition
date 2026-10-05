"""把 agent 列表的 rowspan 分類欄改成整列分組標題（rowspan 欄被擠成直排）。"""
import pathlib
import re

p = pathlib.Path(pathlib.Path(__file__).resolve().parent.parent / "docs/manual/index.html")
s = p.read_text(encoding="utf-8")

# 表頭：去掉「分類」欄
old_head = ("      <tr><th>分類</th><th>Agent（key）</th><th>任務與專屬規則</th>"
            "<th>欄位數</th><th>明細列</th><th>出框策略</th></tr>")
new_head = ("      <tr><th>Agent（key）</th><th>任務與專屬規則</th>"
            "<th>欄位數</th><th>明細列</th><th>出框策略</th></tr>")
assert s.count(old_head) == 1
s = s.replace(old_head, new_head)

# 每個 rowspan 分類儲存格 → 獨立的整列分組標題
def repl(m):
    return f'      <tr><th colspan="5">{m.group(2)}</th></tr>\n      <tr>'

s2, n = re.subn(r'      <tr><td rowspan="(\d+)">([^<]+)</td>', repl, s)
assert n == 4, n
p.write_text(s2, encoding="utf-8")
print("group header rows:", n)
