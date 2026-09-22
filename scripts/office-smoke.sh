#!/usr/bin/env bash
# office-test 镜像功能冒烟：模拟 agent 容器资源限制（2g mem / 2 cpu）
set -euxo pipefail
cd /tmp && mkdir -p smoke && cd smoke

echo "== 1. 版本与关键依赖 =="
hermes --version 2>&1
himalaya --version
/opt/hermes/.venv/bin/python - <<'EOF'
import httpx, openai, pydantic, PIL
print("httpx", httpx.__version__, "| openai", openai.__version__,
      "| pydantic", pydantic.__version__, "| pillow", PIL.__version__)
EOF
# 模拟 agent 会话的干净 profile PATH：默认 python3/pip 必须落在办公 venv 上
env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  python3 -c "import pptx, pandas, fitz; import sys; assert sys.prefix == '/opt/hermes/.venv', sys.prefix; print('默认 python3 -> 办公 venv OK')"
env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin pip3 --version

echo "== 2. 中文 docx -> PDF（LibreOffice）-> PyMuPDF 抽取 =="
/opt/hermes/.venv/bin/python - <<'EOF'
from docx import Document
d = Document()
d.add_heading("季度工作报告", level=1)
d.add_paragraph("办公处理镜像测试：合同编号2026-001，金额壹佰万元整。")
d.add_paragraph("Second paragraph in English.")
d.save("test.docx")
EOF
soffice --headless -env:UserInstallation=file:///tmp/lo_profile --convert-to pdf test.docx --outdir . >/dev/null 2>&1
test -f test.pdf && echo "docx->pdf OK ($(stat -c%s test.pdf) bytes)"
/opt/hermes/.venv/bin/python - <<'EOF'
import fitz
doc = fitz.open("test.pdf")
text = doc[0].get_text()
assert "合同编号2026-001" in text.replace(" ", ""), f"中文抽取失败: {text!r}"
print("pdf 中文抽取 OK:", text.strip().splitlines()[0])
EOF

echo "== 3. pptx 生成 -> PDF =="
/opt/hermes/.venv/bin/python - <<'EOF'
from pptx import Presentation
p = Presentation()
s = p.slides.add_slide(p.slide_layouts[0])
s.shapes.title.text = "测试演示文稿"
s.placeholders[1].text = "副标题：office overlay"
p.save("test.pptx")
EOF
soffice --headless -env:UserInstallation=file:///tmp/lo_profile --convert-to pdf test.pptx --outdir . >/dev/null 2>&1
test -f test.pdf && echo "pptx->pdf OK ($(stat -c%s test.pdf) bytes)"

echo "== 4. xlsx 读写 =="
/opt/hermes/.venv/bin/python - <<'EOF'
import openpyxl, pandas as pd
wb = openpyxl.Workbook(); ws = wb.active; ws["A1"] = "报表"; ws["A2"] = 42
wb.save("t.xlsx")
df = pd.read_excel("t.xlsx", header=None)
assert df.iloc[0,0] == "报表"
print("xlsx 读写 OK")
EOF

echo "== 5. matplotlib 中文渲染 + PDF 转图 + OCR =="
/opt/hermes/.venv/bin/python - <<'EOF'
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
names = {f.name for f in font_manager.fontManager.ttflist}
assert any("Noto Sans CJK" in n for n in names), f"Noto CJK 缺失: {[n for n in names if 'Noto' in n]}"
plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC"]
fig, ax = plt.subplots(figsize=(6,4))
ax.bar(["一季度","二季度","三季度"], [10,25,18])
ax.set_title("销售统计图表")
fig.savefig("chart.png", dpi=120)
print("matplotlib 中文图表 OK")
EOF
/opt/hermes/.venv/bin/python -c "
from pdf2image import convert_from_path
imgs = convert_from_path('test.pdf', dpi=100)
assert imgs, 'pdf2image 转图失败'
print('pdf2image OK:', len(imgs), 'page(s)')"
/opt/hermes/.venv/bin/python - <<'EOF'
from PIL import Image, ImageDraw, ImageFont
f = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 48)
img = Image.new("RGB", (520, 100), "white")
ImageDraw.Draw(img).text((10, 20), "邮件处理自动化测试", font=f, fill="black")
img.save("ocr.png")
import pytesseract
text = pytesseract.image_to_string(Image.open("ocr.png"), lang="chi_sim+eng")
assert "邮件" in text and "测试" in text, f"OCR 识别失败: {text!r}"
print("tesseract chi_sim OCR OK:", text.strip().replace("\n", " "))
EOF

echo "== 6. 邮件链路：email 构造 + bs4/lxml/html2text/imapclient =="
/opt/hermes/.venv/bin/python - <<'EOF'
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import bs4, lxml, html2text, imapclient  # noqa: F401
m = MIMEMultipart(); m["Subject"] = "测试邮件"; m["From"] = "a@b.c"; m["To"] = "d@e.f"
m.attach(MIMEText("<h1>你好</h1><p>正文</p>", "html", "utf-8"))
md = html2text.HTML2Text().handle("<h1>你好</h1><p>正文</p>")
assert "你好" in md
print("email 构造 + html2text OK")
EOF

echo "== ALL SMOKE PASSED =="
