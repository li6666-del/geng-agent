const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, ImageRun, AlignmentType,
  HeadingLevel, LevelFormat, BorderStyle,
} = require("docx");

const FONT = "Microsoft YaHei";
const IMG = "C:\\Users\\84475\\Documents\\耿同学agent\\docs\\overview_four_rounds.png";
const OUT = "C:\\Users\\84475\\Documents\\耿同学agent\\耿同学agent_项目概述.docx";

const p = (text, opts = {}) =>
  new Paragraph({ spacing: { after: 120, line: 300 }, children: [new TextRun({ text, ...opts })], ...(opts.para || {}) });

// paragraph built from inline runs (for bold lead-ins)
const pr = (runs, para = {}) =>
  new Paragraph({ spacing: { after: 120, line: 300 }, children: runs, ...para });

const bullet = (runs) =>
  new Paragraph({ numbering: { reference: "dots", level: 0 }, spacing: { after: 80, line: 300 }, children: Array.isArray(runs) ? runs : [new TextRun(runs)] });

const lead = (label, rest) => [new TextRun({ text: label, bold: true, color: "2E3B4E" }), new TextRun(rest)];

const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: FONT, color: "1F2733" },
        paragraph: { spacing: { before: 260, after: 140 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 25, bold: true, font: FONT, color: "2E3B4E" },
        paragraph: { spacing: { before: 180, after: 90 }, outlineLevel: 1 } },
    ],
  },
  numbering: {
    config: [
      { reference: "dots", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] },
    ],
  },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } } },
    children: [
      // ---- Title ----
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 },
        children: [new TextRun({ text: "耿同学 agent 项目概述", bold: true, size: 40, color: "1F2733" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "3B6FB0", space: 6 } },
        children: [new TextRun({ text: "通信领域论文 · 工程复现可信度审查工具", size: 22, color: "5A6577" })] }),

      // ---- Overview ----
      new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("一、这个项目在做什么")] }),
      p("耿同学 agent 是一个在本地运行的命令行工具，用来审查“通信领域论文的工程复现可信度”。它不直接判定论文真假，而是把一篇论文一步步变成可追溯、可运行、可核对的东西：工程事实 → 复现任务 → 复现项目 → 运行结果 → 复现风险报告。"),
      pr([new TextRun({ text: "一句话记住它：", bold: true }), new TextRun({ text: "LLM 负责“创作”，本地程序负责“把关”。", bold: true, color: "3B6FB0" })]),
      bullet([new TextRun({ text: "LLM 负责", bold: true }), new TextRun("：抽取事实、设计任务、写代码、修代码、审查结果。")]),
      bullet([new TextRun({ text: "本地程序负责", bold: true }), new TextRun("：结构校验、安全扫描、运行验证、风险汇总——论文 / 日志 / 代码全部按“不可信数据”处理。")]),
      bullet([new TextRun({ text: "只给“复现风险”，不下“造假”结论", bold: true }), new TextRun("；并且“能跑通”不等于“已复现”。")]),

      // ---- Architecture diagram ----
      new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("二、四轮工作整体架构")] }),
      p("整个流程分成四轮，每一轮都是“LLM 先创作、本地再把关”，前一轮的产出喂给后一轮："),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 },
        children: [new ImageRun({ type: "png", data: fs.readFileSync(IMG), transformation: { width: 540, height: 795 },
          altText: { title: "四轮架构图", description: "耿同学 agent 四轮工作整体架构", name: "four_rounds" } })] }),

      // ---- Per-round ----
      new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("三、每一轮的核心技术与创新点")] }),

      new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("第一轮 · 抽取工程事实")] }),
      pr(lead("做什么：", "让 LLM 通读论文，抽出信道、调制、指标、仿真参数等“工程事实”，每一条都标注原文出处。")),
      pr(lead("创新点：", "本地“归一化 + 部分接受 + 截断抢救”三重修复。LLM 偶尔会把某个枚举值写错一字、漏一个字段，或输出被截断；过去严格校验会“一票否决”整篇、退回质量很差的关键词兜底。现在本地先把“差一点”的地方自动纠正（例如把 BER 归位成 bit_error_rate），只丢真正修不好的单条，并能从被截断的输出里抢救出完整事实。")),
      pr(lead("产出：", "engineering_facts.json（带出处的工程事实）。")),

      new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("第二轮 · 设计复现任务")] }),
      pr(lead("做什么：", "把工程事实翻译成可执行的“复现任务”——要复现哪张图、用什么指标公式、输出哪些列、期望什么趋势、和谁对比。")),
      pr(lead("创新点：", "本地校验任务引用的事实是否真实存在、指标与趋势是否合法；并把第一轮的“归一化 + 部分接受”同样用到了这一轮（近期新增）。")),
      pr(lead("产出：", "repro_tasks.json（复现任务清单）。")),

      new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("第三轮 · 生成复现项目")] }),
      pr(lead("做什么：", "让 LLM 按计划“逐个文件”生成一个可以直接运行的 Python 复现项目。")),
      pr(lead("创新点：", "分文件生成（避免一次性输出过长被截断）+ 清单归一化（剥掉多余字段）+ 路径安全（防越权写文件）+ 语法编译检查。")),
      pr(lead("产出：", "repro_project/（一套可运行的代码）。")),

      new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun("第四轮 · 运行与结果级审查")] }),
      pr(lead("做什么：", "在“受限沙箱”里真正把代码跑起来，再用多模态 LLM 把本地跑出的图表与论文原图逐一对比。")),
      pr(lead("创新点：", "①安全沙箱：依赖白名单、静态扫描（禁网络 / 危险调用 / 反射式动态执行）、干净环境、运行隔离；②科学完整性闸：拒绝“能跑但结果退化”的产物（含新增的“近常数 / 低方差”判据，能抓出 BER≈0.5 这种随机猜测结果）；③运行失败时自动修复代码；④把差异汇总成复现风险结论。")),
      pr(lead("产出：", "result_review、risk_report 与复现性风险结论。")),

      // ---- Progress ----
      new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("四、当前进展")] }),
      bullet("第一、二轮都加上了“归一化 / 部分接受 / 截断抢救”，大幅减少“被迫退兜底”的情况。"),
      bullet("第四轮科学完整性闸新增“近常数 / 低方差”判据，堵住“BER≈0.5 随机结果蒙混过关”的盲区。"),
      bullet("监督层补全：把 LLM 的“提示词调整”真正接入重试（此前是不起作用的死字段）。"),
      bullet("安全加固：静态扫描新增拦截 eval / exec / __import__ / getattr 等反射式动态执行。"),
      bullet("已用一篇真实通信论文（arXiv:1404.2302）端到端跑通全流程；当生成代码质量不佳时，系统诚实判为 high_reproducibility_risk，而不是谎报成功。"),
      bullet("以上改动均有自动化测试覆盖（当前 129 项测试全部通过）。"),

      new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("五、一句话总结")] }),
      p("我们在做的，是让“用 AI 复现论文”这件事变得可追溯、可运行、可信任：AI 大胆创作，本地严格把关，最终只给出复现风险，而不替人下结论。"),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("wrote:", OUT, buf.length, "bytes"); });
