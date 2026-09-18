from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from geng_agent.docx_styles import _add_title, _setup_document

from geng_agent.docx_writer import (
    write_markdown_report_docx,
    write_result_review_markdown_docx,
)


TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class DocxWriterTests(unittest.TestCase):

    def test_appendix_starts_a_page_without_an_empty_page_break_paragraph(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.docx"
            write_markdown_report_docx(
                path,
                markdown_text="# 报告\n\n正文结尾。\n\n## 附录：完整证据\n\n保留证据。\n",
                title="报告", subtitle="",
            )
            document = Document(path)
            appendix = next(p for p in document.paragraphs if p.text == "附录：完整证据")
            self.assertTrue(appendix.paragraph_format.page_break_before)
            self.assertEqual(len(document._element.xpath(".//w:br[@w:type='page']")), 0)
            self.assertEqual([p.text for p in document.paragraphs],
                             ["报告", "正文结尾。", "附录：完整证据", "保留证据。"])

    def test_title_disables_template_and_inherited_paragraph_borders(self) -> None:
        document = Document()
        for style_name in ("Normal", "Title"):
            properties = document.styles[style_name]._element.get_or_add_pPr()
            borders = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:color"), "0070C0")
            borders.append(bottom)
            properties.append(borders)
        _setup_document(document)
        _add_title(document, "简约报告", "")
        for element in (document.styles["Title"]._element, document.paragraphs[0]._p):
            borders = element.find(qn("w:pPr")).findall(qn("w:pBdr"))
            self.assertEqual(len(borders), 1)
            self.assertEqual(len(borders[0]), 6)
            self.assertTrue(all(edge.get(qn("w:val")) == "nil" for edge in borders[0]))
        # Overriding Title must not mutate the base style used by other content.
        normal_border = document.styles["Normal"]._element.find(qn("w:pPr")).find(qn("w:pBdr"))
        self.assertEqual(normal_border[0].get(qn("w:val")), "single")

    def test_short_data_rows_stay_together_but_long_evidence_rows_can_paginate(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.docx"
            long_text = "完整证据及参数说明。" * 500
            write_markdown_report_docx(
                path,
                markdown_text=("# 报告\n\n| 项目 | 结果 |\n|---|---|\n"
                               "| T1 | 该短行应整体换页，不能在表头后只留下半句话。 |\n"
                               f"| 详细证据 | {long_text} |\n"),
                title="报告", subtitle="",
            )
            document = Document(path)
            rows = document.tables[0].rows
            self.assertEqual(rows[1]._tr.trPr.find(qn("w:cantSplit")).get(qn("w:val")), "true")
            self.assertEqual(rows[2]._tr.trPr.find(qn("w:cantSplit")).get(qn("w:val")), "false")
            self.assertEqual(rows[2].cells[1].text, long_text)

    def test_editor_title_is_used_once_without_added_report_body(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "review.docx"
            write_markdown_report_docx(
                path,
                markdown_text="# Editor 的报告标题\n\n## 任务概览\n\n作者正文。\n",
                title="旧版固定标题",
                subtitle="旧版自动副标题",
            )
            document = Document(path)
            self.assertEqual([p.text for p in document.paragraphs],
                             ["Editor 的报告标题", "任务概览", "作者正文。"])
            self.assertEqual(document.paragraphs[0].style.name, "Title")
            self.assertEqual(document.paragraphs[1].style.name, "Heading 1")
            self.assertTrue(document.styles["Heading 1"].paragraph_format.keep_with_next)
            self.assertEqual(len(document.sections[0].footer._element.xpath(".//w:fldSimple")), 1)

    def test_long_authored_text_is_preserved_in_paragraphs_and_table_cells(self) -> None:
        long_text = "关键参数与完整证据。" * 250 + "正文结尾"
        cell_text = "完整假设说明。" * 250 + "单元格结尾"
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "review.docx"
            write_markdown_report_docx(
                path,
                markdown_text=f"# 报告\n\n{long_text}\n\n| 项目 | 内容 |\n|---|---|\n| 假设 | {cell_text} |\n",
                title="回退标题", subtitle="",
            )
            document = Document(path)
            self.assertIn(long_text, [paragraph.text for paragraph in document.paragraphs])
            self.assertEqual(document.tables[0].cell(1, 1).text, cell_text)
            self.assertNotIn("已截断", document._element.xml)

    def test_links_and_code_blocks_remain_usable_without_markdown_interpretation(self) -> None:
        code = "python run.py --label '**原样保留**'\n    # 不是标题\n    | a | b |"
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.docx"
            write_markdown_report_docx(
                path,
                markdown_text=("# 报告\n\n打开[原始数据](results/data.csv)，运行 `python run.py`。\n\n"
                               f"```powershell\n{code}\n```\n"),
                title="报告", subtitle="",
            )
            document = Document(path)
            links = document._element.xpath(".//w:hyperlink")
            self.assertEqual(len(links), 1)
            relationship = document.part.rels[links[0].get(qn("r:id"))]
            self.assertEqual(relationship.target_ref, "results/data.csv")
            self.assertEqual(links[0].xpath(".//w:t")[0].text, "原始数据")
            blocks = [p for p in document.paragraphs if p.style.name == "Report Code"]
            self.assertEqual([p.text for p in blocks], [code])
            self.assertEqual(len(document.tables), 0)
            inline_code = next(run for p in document.paragraphs for run in p.runs if run.text == "python run.py")
            self.assertEqual(inline_code.font.name, "Consolas")

    def test_table_widths_alignment_and_borders_preserve_readable_structure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.docx"
            write_markdown_report_docx(
                path,
                markdown_text=("# 报告\n\n| 任务 | 关键差距 | 数值 | 状态 |\n|---|---|---|:---:|\n"
                               "| T1 | 区间内的方法排序与论文描述存在明显差异 | 0.00231 | 未复现 |\n"
                               "| T2 | 多种方法存在重合曲线，不能视为独立交叉点 | -1.2e-3 | 未复现 |\n"),
                title="报告", subtitle="",
            )
            document = Document(path)
            table = document.tables[0]
            self.assertGreater(table.columns[1].width, table.columns[0].width)
            section = document.sections[0]
            self.assertLessEqual(sum(column.width for column in table.columns),
                                 section.page_width - section.left_margin - section.right_margin)
            self.assertEqual(table.cell(1, 2).paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.RIGHT)
            self.assertEqual(table.cell(1, 3).paragraphs[0].alignment, WD_ALIGN_PARAGRAPH.CENTER)
            borders = table._tbl.tblPr.find(qn("w:tblBorders"))
            self.assertIsNotNone(borders)
            self.assertTrue(all(border.get(qn("w:color")) == "D9D9D9" for border in borders))


    def test_write_result_review_markdown_docx_creates_openable_report(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "result_review.docx"
            image_path = root / "local.png"
            paper_image_path = root / "paper.png"
            image_path.write_bytes(TINY_PNG)
            paper_image_path.write_bytes(TINY_PNG)

            write_result_review_markdown_docx(
                path,
                markdown_text=(
                    "## 1. reproduce_fig_1\n\n"
                    "### 图像对比\n\n"
                    "| 本地复现图 | 论文原图 |\n"
                    "|---|---|\n"
                    f"| ![本地复现图]({image_path}) | ![论文原图：Fig. 1]({paper_image_path}) |\n\n"
                    "### 简短审查结论\n\n"
                    "- 本地曲线趋势一致。\n"
                ),
                status={
                    "passed": True,
                    "mode": "codex_markdown_by_experiment",
                    "result_review_markdown_path": str(root / "result_review.md"),
                },
            )

            document = Document(path)
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            table_text = "\n".join(
                paragraph.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
                for paragraph in cell.paragraphs
            )
            self.assertIn("复现结果二次审查报告", text)
            self.assertIn("本地曲线趋势一致", text)
            self.assertIn("本地复现图", table_text)
            self.assertIn("论文原图：Fig. 1", table_text)
            self.assertNotIn("附录", text)
            self.assertEqual(len(document.tables), 1)
            self.assertEqual(len(document.inline_shapes), 2)

    def test_image_tables_are_detected_from_cells_not_specialized_headers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "result_review.docx"
            images = [root / f"figure_{index}.png" for index in range(6)]
            for image_path in images:
                image_path.write_bytes(TINY_PNG)

            write_result_review_markdown_docx(
                path,
                markdown_text=(
                    "## 任务 1：图 1\n\n"
                    "| 本地最终结果 | 论文图 |\n"
                    "|---|---|\n"
                    f"| ![本地图1]({images[0]}) | ![论文图1]({images[1]}) |\n\n"
                    "| 验收判据 | 证据结论 |\n"
                    "|---|---|\n"
                    "| 趋势一致 | **支持** |\n\n"
                    "## 任务 2：图 2(a)\n\n"
                    "| 本地最终结果 | 论文页面中的图 2(a) |\n"
                    "|---|---|\n"
                    f"| ![本地图2a]({images[2]}) | ![论文图2a]({images[3]}) |\n\n"
                    "## 补充三栏图片表\n\n"
                    "| 本地最终结果 | 论文图 | 补充视图 |\n"
                    "|---|---|---|\n"
                    f"| ![本地图3]({images[4]}) | ![论文图3]({images[5]}) | 仅文字说明 |\n"
                ),
                status={"passed": True},
            )

            document = Document(path)
            table_text = "\n".join(
                paragraph.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
                for paragraph in cell.paragraphs
            )

            self.assertEqual(len(document.tables), 4)
            self.assertEqual(len(document.tables[-1].columns), 3)
            self.assertEqual(len(document.inline_shapes), 6)
            self.assertIn("论文页面中的图 2(a)", table_text)
            self.assertIn("仅文字说明", table_text)
            self.assertIn("支持", table_text)
            self.assertNotIn("![", table_text)

    def test_generic_report_docx_resolves_relative_report_assets(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            asset = root / "report_assets" / "task_1" / "paper_target.png"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(TINY_PNG)
            path = root / "reproduction_report.docx"

            write_markdown_report_docx(
                path,
                markdown_text="## task_1\n\n![论文原图](report_assets/task_1/paper_target.png)\n",
                title="本地复现报告",
                subtitle="参数与假设",
                base_dir=root,
            )

            document = Document(path)
            self.assertEqual(len(document.inline_shapes), 1)
            self.assertIn("本地复现报告", "\n".join(paragraph.text for paragraph in document.paragraphs))

    def test_generic_markdown_tables_and_inline_bold_render_as_word_structure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "review.docx"

            write_markdown_report_docx(
                path,
                markdown_text=(
                    "## 任务结果\n\n"
                    "整体风险为**高**，但__关键结论__仍可核验。\n\n"
                    "- **主要差异：**仅缺少精确参数。\n\n"
                    "| 方法 | 终局结果 | 备注 |\n"
                    "|---|:---:|---|\n"
                    "| Alpha \\| Beta | **带假设复现** | `a|b` |\n"
                ),
                title="复现审查报告",
                subtitle="Markdown 结构回归",
                base_dir=root,
            )

            document = Document(path)
            paragraphs = list(document.paragraphs) + [
                paragraph
                for table in document.tables
                for row in table.rows
                for cell in row.cells
                for paragraph in cell.paragraphs
            ]
            rendered_text = "\n".join(paragraph.text for paragraph in paragraphs)
            bold_text = {
                run.text
                for paragraph in paragraphs
                for run in paragraph.runs
                if run.bold
            }

            self.assertEqual(len(document.tables), 1)
            self.assertEqual(len(document.tables[0].columns), 3)
            for cell in document.tables[0].rows[0].cells:
                self.assertTrue(cell.paragraphs[0].paragraph_format.keep_with_next)
            self.assertIn("Alpha | Beta", rendered_text)
            self.assertIn("a|b", rendered_text)
            self.assertNotIn("|---|", rendered_text)
            self.assertNotIn("**", rendered_text)
            self.assertNotIn("__", rendered_text)
            self.assertIn("高", bold_text)
            self.assertIn("关键结论", bold_text)
            self.assertIn("主要差异：", bold_text)
            self.assertIn("带假设复现", bold_text)

    def test_write_result_review_markdown_docx_records_missing_images(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "result_review.docx"
            missing = root / "missing.png"

            write_result_review_markdown_docx(
                path,
                markdown_text=f"## 1. reproduce_fig_1\n\n![missing figure]({missing})\n\nReviewer body.\n",
                status={"passed": True},
            )

            document = Document(path)
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            self.assertIn("图片缺失", text)
            self.assertIn(str(missing), text)
            self.assertEqual(len(document.inline_shapes), 0)

    def test_write_result_review_markdown_docx_records_missing_image_in_comparison_table(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "result_review.docx"
            local_image = root / "local.png"
            missing = root / "missing.png"
            local_image.write_bytes(TINY_PNG)

            write_result_review_markdown_docx(
                path,
                markdown_text=(
                    "## 1. reproduce_fig_1\n\n"
                    "| 本地复现图 | 论文原图 |\n"
                    "|---|---|\n"
                    f"| ![本地复现图]({local_image}) | ![论文原图]({missing}) |\n"
                ),
                status={"passed": True},
            )

            document = Document(path)
            text = "\n".join(
                paragraph.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
                for paragraph in cell.paragraphs
            )
            self.assertIn("图片缺失", text)
            self.assertIn(str(missing), text)
            self.assertEqual(len(document.tables), 1)
            self.assertEqual(len(document.inline_shapes), 1)

    def test_write_result_review_markdown_docx_renders_multiple_images_in_one_table_cell(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "result_review.docx"
            local_a = root / "local_a.png"
            local_b = root / "local_b.png"
            paper = root / "paper.png"
            for image_path in (local_a, local_b, paper):
                image_path.write_bytes(TINY_PNG)

            write_result_review_markdown_docx(
                path,
                markdown_text=(
                    "## 1. reproduce_fig_1\n\n"
                    "| 本地复现图 | 论文原图 |\n"
                    "|---|---|\n"
                    f"| ![本地复现图 A]({local_a}) <br /> ![本地复现图 B]({local_b}) "
                    f"| ![论文原图]({paper}) |\n"
                ),
                status={"passed": True},
            )

            document = Document(path)
            table_text = "\n".join(
                paragraph.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
                for paragraph in cell.paragraphs
            )
            self.assertEqual(len(document.tables), 1)
            self.assertEqual(len(document.inline_shapes), 3)
            self.assertIn("本地复现图 A", table_text)
            self.assertIn("本地复现图 B", table_text)
            self.assertNotIn("![", table_text)


if __name__ == "__main__":
    unittest.main()
