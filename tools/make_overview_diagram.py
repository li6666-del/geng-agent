"""Generate a beginner-friendly four-round architecture diagram for the overview doc."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

BLUE_F, BLUE_E = "#DCE8FB", "#3B6FB0"      # LLM creates
GREEN_F, GREEN_E = "#DCF0E1", "#3C9A5F"    # local verifies
GRAY_F, GRAY_E = "#ECEFF4", "#7A8699"      # input / parse
AMBER_F, AMBER_E = "#FBEFD6", "#C9912E"    # output / reports
INK = "#1F2733"

fig, ax = plt.subplots(figsize=(11, 16.2), dpi=132)
ax.set_xlim(0, 100)
ax.set_ylim(0, 132)
ax.axis("off")


def box(x, y, w, h, fc, ec, rad=1.6, lw=1.8):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={rad}",
                                fc=fc, ec=ec, lw=lw, mutation_aspect=1))


def text(x, y, s, size, color=INK, weight="normal", ha="center", va="center"):
    ax.text(x, y, s, fontsize=size, color=color, fontweight=weight, ha=ha, va=va, linespacing=1.45)


def down_arrow(x, y0, y1, label=None):
    ax.add_patch(FancyArrowPatch((x, y0), (x, y1), arrowstyle="-|>", mutation_scale=22,
                                 lw=2.2, color="#5A6577"))
    if label:
        ax.text(x + 1.5, (y0 + y1) / 2, label, fontsize=12.5, color="#3A4250", ha="left", va="center",
                fontweight="bold")


def round_block(y_top, no, title, llm_lines, local_lines, accent):
    h = 17.5
    x, w = 6, 88
    # outer card
    box(x, y_top - h, w, h, "#FFFFFF", "#C9D2DE", rad=1.8, lw=1.6)
    # accent stripe + round badge
    box(x, y_top - h, 2.4, h, accent, accent, rad=1.0, lw=0)
    ax.add_patch(plt.Circle((x + 7.5, y_top - 3.4), 2.6, fc=accent, ec=accent))
    text(x + 7.5, y_top - 3.4, f"{no}", 16, "#FFFFFF", "bold")
    text(x + 13, y_top - 3.4, title, 16.5, INK, "bold", ha="left")
    # two columns
    colw = 39
    lx, rx = x + 5, x + 47
    box(lx, y_top - h + 1.6, colw, 9.3, BLUE_F, BLUE_E, rad=1.2, lw=1.4)
    box(rx, y_top - h + 1.6, colw, 9.3, GREEN_F, GREEN_E, rad=1.2, lw=1.4)
    header_y = y_top - 8.4
    body_y = y_top - 12.7
    text(lx + colw / 2, header_y, "LLM 负责（创作）", 12.5, BLUE_E, "bold")
    text(rx + colw / 2, header_y, "本地程序负责（把关）", 12.5, GREEN_E, "bold")
    text(lx + colw / 2, body_y, "\n".join(llm_lines), 11.5, INK)
    text(rx + colw / 2, body_y, "\n".join(local_lines), 11.5, INK)


# ---- principle banner ----
box(6, 123, 88, 7.4, "#2E3B4E", "#2E3B4E", rad=1.6, lw=0)
text(50, 128.2, "耿同学 agent · 四轮工作整体架构", 18, "#FFFFFF", "bold")
text(50, 124.6, "核心原则：LLM 负责“创作”，本地程序负责“把关”；论文 / 日志 / 代码一律视为“不可信数据”，只评估复现风险，不下造假结论",
     11.5, "#D8E0EC")

# ---- input ----
box(28, 112.5, 44, 7.6, GRAY_F, GRAY_E, rad=1.4)
text(50, 117.6, "输入：通信论文 PDF / TXT / Markdown", 13, INK, "bold")
text(50, 114.2, "本地解析成带出处(chunk_id/页/段)的文本块", 11.5, "#3A4250")
down_arrow(50, 112.2, 110.4)

# ---- four rounds ----
round_block(109.5, 1, "抽取工程事实",
            ["读论文，抽取信道 / 调制 /", "指标 / 参数等“工程事实”"],
            ["枚举归一化 · 部分接受 ·", "截断抢救 · 结构校验 ·", "每条事实回指原文出处"],
            "#3B6FB0")
down_arrow(50, 92.0, 88.0, "engineering_facts.json")

round_block(87.0, 2, "设计复现任务",
            ["把事实变成可执行的", "复现任务"],
            ["校验指标公式 / 输出列 /", "期望趋势 / 基线引用", "归一化 · 部分接受"],
            "#8E5BB5")
down_arrow(50, 69.5, 65.5, "repro_tasks.json")

round_block(64.5, 3, "生成复现项目",
            ["按计划逐个文件生成", "可运行的 Python 复现代码"],
            ["清单归一化 · 路径安全 ·", "语法编译检查 · 截断抢救"],
            "#2F8F86")
down_arrow(50, 47.0, 43.0, "repro_project/（可运行项目）")

round_block(42.0, 4, "运行与结果级审查",
            ["多模态比对本地图表与", "论文图；失败时修复代码"],
            ["受限沙箱(白名单/静态扫描/", "隔离) · 科学完整性闸 ·", "风险汇总"],
            "#C0603A")
down_arrow(50, 24.5, 20.4)

# ---- output ----
box(16, 11.5, 68, 8.6, AMBER_F, AMBER_E, rad=1.6)
text(50, 17.4, "产出：review.md / .docx · risk_report.json · result_review", 12.5, INK, "bold")
text(50, 13.6, "复现性风险结论（如 high_reproducibility_risk）+ 全程审计留痕", 11.5, "#5A4520")

# ---- legend ----
box(8.5, 3.0, 3.2, 3.2, BLUE_F, BLUE_E, rad=0.7, lw=1.4)
text(13, 4.6, "蓝 = LLM 生成", 11.5, INK, ha="left")
box(37, 3.0, 3.2, 3.2, GREEN_F, GREEN_E, rad=0.7, lw=1.4)
text(41.5, 4.6, "绿 = 本地校验 / 守护", 11.5, INK, ha="left")
box(72, 3.0, 3.2, 3.2, AMBER_F, AMBER_E, rad=0.7, lw=1.4)
text(76.5, 4.6, "黄 = 最终产出", 11.5, INK, ha="left")

fig.subplots_adjust(left=0.01, right=0.99, top=0.995, bottom=0.005)
out = r"C:\Users\84475\Documents\耿同学agent\docs\overview_four_rounds.png"
fig.savefig(out, dpi=132, facecolor="white")
print("saved:", out)
