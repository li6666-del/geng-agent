# 网页动漫插画与字体

两张 PNG 均使用内置 image_gen 生成，透明背景，原图已复制入本目录。主图和卫星小助手只作装饰，网页 aria-hidden，不代表运行状态。

## 通信研究员

文件：telecom-researcher-v1.png

Use case: illustration-story
Asset type: transparent illustration for a light blue and white Chinese communications-research website, to be displayed around 420 pixels wide beside a login form.
Primary request: an original charming anime-style young university communications researcher at a tiny wireless communications workstation, merging anime illustration with telecom science.
Subject: one cute androgynous chibi researcher with dark blue short hair, large headphones, round glasses, an oversized white and pale blue laboratory jacket, working happily on a compact blue laptop. A small directional antenna and a tiny floating satellite, elegant cyan radio-wave arcs and a simple little constellation diagram around the desk convey wireless communications. Natural clear hands, clean silhouette, few objects, no clutter.
Style/medium: premium Japanese anime editorial spot illustration, crisp navy line art, soft cel shading, rounded friendly forms, sophisticated calm playful feeling, not childish toy 3D.
Composition: landscape composition approximately 3:2, all objects fully visible, generous transparent margins, workstation and character centered, antenna one side, tiny satellite above other side. No rectangular background or floor panel.
Color palette: mostly white, powder blue, cobalt #2b5ed7 and navy outlines, restrained aqua #48b9bd accents; tiny soft warm blush detail.
Constraints: genuinely transparent background with alpha; no text, numbers, labels, logos, watermarks, or fake interface; no school insignia; original character; no photographic effects; do not add large opaque glow.

## 卫星助手

文件：telecom-satellite-v1.png

Use case: illustration-story
Asset type: small transparent spot illustration for a light blue and white university wireless communications research dashboard.
Primary request: a charming original anime-style tiny satellite robot companion carrying a few white research papers with simple blue wave markings, happy expressive face on a rounded white capsule body, navy eyes, pale blue solar-panel wings, little cobalt radio antenna on its head, two aqua radio arcs nearby. Premium 2D anime clean line art and soft cel shading, delicate blue outlines, blue/white/cyan palette matching a calm research website. One clear simple silhouette, floating at a gentle diagonal, round friendly forms. This is a scientific mascot, not a toy product photograph. Square framing, entire robot visible, generous negative space, genuinely transparent background with alpha. No text, logos, watermark, opaque glow or landscape. Must read clearly at 150px size.

## 字体

Geng UI Rounded 是 Resource Han Rounded CN 0.990 的界面字符子集（Regular/Medium/Bold）。原始字体：https://github.com/CyanoHao/Resource-Han-Rounded/releases/tag/v0.990 。完整 SIL OFL 1.1 许可在 fonts/OFL-License.txt，部署副本位于 /assets/rounded-font-license.txt。

字体已改名以标明派生。包含固定界面文案所需字符，论文标题等动态文本缺字回退系统中文字体；字体本地加载，font-display: swap。更新文案时可重新用 fontTools.subset 生成 WOFF2，保留版权字段并使用派生名称。
