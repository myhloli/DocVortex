"""生成可公开的密集表格压力样本，不依赖本地外部 PDF 或业务内容。"""

from pathlib import Path
import sys

from reportlab.pdfgen.canvas import Canvas


def generate(path: Path) -> None:
    """用固定元数据和 120 行七列表格复现字符密集、横线区间多的计算负载。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(path), pagesize=(595, 842), invariant=1)
    canvas.setLineWidth(0.3)
    left, top, width, step = 30, 800, 76, 6
    for row in range(121):
        y = top - row * step
        canvas.line(left, y, left + 7 * width, y)
    for column in range(8):
        x = left + column * width
        canvas.line(x, top, x, top - 120 * step)
    canvas.setFont("Helvetica", 5)
    for row in range(120):
        for column in range(7):
            canvas.drawString(left + column * width + 2, top - row * step - 4.5, f"{row:03d}-{column}-{row * 7 + column:04d}")
    canvas.drawString(left, 65, "Synthetic benchmark. All values are generated.")
    canvas.save()


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
