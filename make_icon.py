"""生成应用图标 app.ico（电池 + 闪电主题，多尺寸）。"""
from PIL import Image, ImageDraw

SIZE = 256


def draw_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0

    def I(v):
        return int(round(v * s))

    # 电池外框（圆角矩形）
    body = [I(18), I(70), I(200), I(186)]
    d.rounded_rectangle(body, radius=I(18), outline=(80, 200, 120, 255), width=I(12))

    # 电池正极帽
    d.rounded_rectangle([I(208), I(105), I(228), I(151)], radius=I(5),
                        fill=(80, 200, 120, 255))

    # 电量填充（绿色，约 70%）
    fill = [I(26), I(78), I(168), I(178)]
    d.rounded_rectangle(fill, radius=I(12), fill=(80, 200, 120, 255))

    # 闪电符号（黄色，居中偏右）
    lx = I(118)
    points = [
        (lx + I(6), I(88)),
        (lx - I(22), I(138)),
        (lx + I(2), I(138)),
        (lx - I(6), I(176)),
        (lx + I(26), I(122)),
        (lx + I(2), I(122)),
    ]
    d.polygon(points, fill=(255, 220, 60, 255))

    return img


img = draw_icon(SIZE)
img.save("app.png")
img.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("已生成 app.ico 和 app.png")
