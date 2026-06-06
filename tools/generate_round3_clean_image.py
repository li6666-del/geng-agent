from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


OUT = Path("docs/harness_four_rounds_assets/round_03_codegen_runtime_clean.png")
BLUE = "#14528F"
LINE = "#1F6FB8"
FILL = "#EEF5FC"
GREEN = "#EDF8F0"
WARN = "#FFF3DE"
ORANGE = "#E49B2A"
GRAY = "#5B6775"
LIGHT = "#D8E4F0"


def font(size, bold=False):
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf" if bold else "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def node(draw, xy, text, fill=FILL, outline=LINE):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=26, fill=fill, outline=outline, width=5)
    draw.text(((x1 + x2) / 2, (y1 + y2) / 2 - 24), text, fill=BLUE, font=font(34, True), anchor="ma")


def arrow(draw, start, end, color=BLUE):
    sx, sy = start
    ex, ey = end
    draw.line((sx, sy, ex, ey), fill=color, width=7)
    if abs(ex - sx) >= abs(ey - sy):
        direction = 1 if ex > sx else -1
        points = [(ex, ey), (ex - 24 * direction, ey - 15), (ex - 24 * direction, ey + 15)]
    else:
        direction = 1 if ey > sy else -1
        points = [(ex, ey), (ex - 15, ey - 24 * direction), (ex + 15, ey - 24 * direction)]
    draw.polygon(points, fill=color)


def elbow(draw, points, color=BLUE):
    for idx, (start, end) in enumerate(zip(points, points[1:])):
        if idx == len(points) - 2:
            arrow(draw, start, end, color=color)
        else:
            draw.line((*start, *end), fill=color, width=7)


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1600, 930), "white")
    draw = ImageDraw.Draw(img)

    draw.text((70, 52), "第三轮：生成并运行复现项目", font=font(46, True), fill=BLUE)
    draw.text((72, 112), "代码必须先过本地护栏，再进入运行与修复闭环", font=font(28), fill=GRAY)
    draw.line((70, 155, 1530, 155), fill=LIGHT, width=4)

    y = 330
    h = 145
    nodes = [
        ((70, y, 255, y + h), "任务输入"),
        ((320, y, 535, y + h), "分文件生成"),
        ((600, y, 815, y + h), "本地审查"),
        ((880, y, 1095, y + h), "落地项目"),
        ((1160, y, 1495, y + h), "本地执行"),
    ]
    for xy, text in nodes:
        node(draw, xy, text, GREEN if text == "本地执行" else FILL)
    arrow(draw, (255, y + h // 2), (320, y + h // 2))
    arrow(draw, (535, y + h // 2), (600, y + h // 2))
    arrow(draw, (815, y + h // 2), (880, y + h // 2))
    arrow(draw, (1095, y + h // 2), (1160, y + h // 2))

    node(draw, (1160, 620, 1495, 760), "outputs", GREEN)
    arrow(draw, (1328, y + h), (1328, 620))

    node(draw, (455, 620, 720, 760), "失败修复", WARN, ORANGE)
    node(draw, (820, 620, 1085, 760), "重新验收", WARN, ORANGE)
    elbow(draw, [(708, y + h), (708, 555), (588, 555), (588, 620)], ORANGE)
    elbow(draw, [(1328, y + h), (1328, 555), (588, 555), (588, 620)], ORANGE)
    arrow(draw, (720, 690), (820, 690))
    elbow(draw, [(952, 620), (952, 585), (1160, 585), (1160, 690)])

    draw.rounded_rectangle((70, 830, 1530, 880), radius=18, fill="#F8FAFC", outline="#D7E0EA", width=2)
    draw.text((100, 842), "主线：生成 -> 审查 -> 执行 -> 输出；失败：修复 -> 验收 -> 重跑。", font=font(26), fill=GRAY)

    img.save(OUT)
    print(OUT.resolve())


if __name__ == "__main__":
    main()
