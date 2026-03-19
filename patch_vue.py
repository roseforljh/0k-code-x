import re
with open(r"C:\Users\33039\Desktop\0k-code-x\webui\frontend\src\App.vue", "r", encoding="utf-8") as f:
    text = f.read()

# 1. Dashboard: Move stats up and wrap grid
pattern = r'(<div class="grid">)\s*(<section class="card form-card">[\s\S]*?</section>)\s*(<section class="card stats-card">[\s\S]*?</section>)'
repl = r'<div class="dashboard-top-section">\n            \3\n          </div>\n\n          \1\n            \2'
text = re.sub(pattern, repl, text)

# 2. Add page-container around dynamic pages
text = text.replace('<div :key="`${page}-${pageAnimKey}`">', '<div class="page-container" :key="`${page}-${pageAnimKey}`">')

# 3. Rename action bar for accounts
text = text.replace('<div class="actions action-bar">', '<div class="actions action-bar staggered-fade">')
text = text.replace('<div class="table-wrap">', '<div class="table-wrap staggered-fade" style="animation-delay: 0.1s">')
text = text.replace('<div class="account-summary-grid">', '<div class="account-summary-grid staggered-fade">')

with open(r"C:\Users\33039\Desktop\0k-code-x\webui\frontend\src\App.vue", "w", encoding="utf-8") as f:
    f.write(text)
