"""生成一张用于演示 / 测试的模拟试卷图片（印刷题干 + 蓝色"手写"答案）。"""
import sys
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 1600
ROWS = [("1", "12 + 8 =", "20"), ("2", "15 - 6 =", "8"), ("3", "7 x 6 =", "42"),
        ("4", "1/2 + 1/4 =", "0.75"), ("5", "81 / 9 =", "9")]


def make(path, answers=None):
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=44)
    d.text((W // 2, 90), "Math Quiz - Unit 3", fill="black", font=ImageFont.load_default(size=56), anchor="mm")
    d.text((120, 170), "Name: ________", fill="black", font=ImageFont.load_default(size=32))
    for i, (qid, stem, ans) in enumerate(ROWS):
        y = 300 + i * 220
        d.text((120, y), f"{qid}.  {stem}", fill="black", font=f)
        d.line((520, y + 56, 760, y + 56), fill="black", width=2)
        a = (answers or {}).get(qid, ans)
        d.text((560, y - 4), a, fill=(30, 50, 160), font=ImageFont.load_default(size=50))
    img.save(path)


if __name__ == "__main__":
    make(sys.argv[1] if len(sys.argv) > 1 else "sample.png")
