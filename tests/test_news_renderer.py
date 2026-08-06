import sys
import unittest
from datetime import datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_renderer import (  # noqa: E402
    NEWS_IMAGE_TEMPLATE,
    build_render_data,
    extract_news_items,
    source_name_from_url,
)


SAMPLE_HTML = """
<h2>概览</h2>
<h3>要闻</h3><ul><li>概览内容 <a href="https://example.com">↗</a> #1</li></ul>
<hr>
<h2>要闻</h2>
<h3><a href="https://platform.deepseek.com/usage">DeepSeek API 即将调价</a> <code>#1</code></h3>
<blockquote>DeepSeek 提示近期将调整 API 定价，具体方案以正式通知为准。</blockquote>
<hr>
<h3><a href="https://research.meta.ai/blog/muse">Meta 发布 Muse {1.2|一点二}</a> <code>#2</code></h3>
<blockquote>新模型提升代码生成、复杂调试与大型代码库理解能力。</blockquote>
"""


class NewsRendererTests(unittest.TestCase):
    def test_template_uses_system_font_stack(self):
        self.assertIn('"Noto Sans CJK SC"', NEWS_IMAGE_TEMPLATE)

    def test_template_supports_embedded_lxgw_wenkai(self):
        self.assertIn('font-family: "LXGW WenKai Lite Embedded"', NEWS_IMAGE_TEMPLATE)
        self.assertIn("data:font/ttf;base64,{{ font_data }}", NEWS_IMAGE_TEMPLATE)

    def test_extracts_detailed_items_not_overview(self):
        items = extract_news_items(SAMPLE_HTML, max_items=4)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "DeepSeek API 即将调价")
        self.assertEqual(items[0]["category"], "要闻")
        self.assertEqual(items[0]["source"], "DeepSeek")
        self.assertEqual(items[1]["title"], "Meta 发布 Muse 1.2")
        self.assertTrue(items[0]["show_category"])
        self.assertFalse(items[1]["show_category"])

    def test_limits_items(self):
        items = extract_news_items(SAMPLE_HTML, max_items=1)
        self.assertEqual(len(items), 1)

    def test_known_and_generic_source_names(self):
        self.assertEqual(source_name_from_url("https://www.ithome.com/0/1"), "IT之家")
        self.assertEqual(source_name_from_url("https://docs.example.org/a"), "Example")

    def test_extracts_every_detailed_update(self):
        entries = "".join(
            f'<h3><a href="https://example.com/{i}">新闻 {i}</a> '
            f'<code>#{i}</code></h3><blockquote>这是第 {i} 条完整新闻导语。</blockquote>'
            for i in range(1, 20)
        )
        items = extract_news_items(f"<h2>要闻</h2>{entries}")
        self.assertEqual(len(items), 19)

    def test_repairs_obvious_source_link_mismatch(self):
        html = (
            '<h2>模型发布</h2><h3><a href="https://usa.visa.com/news">'
            '小米开源 Xiaomi-Robotics-1</a> <code>#1</code></h3>'
            '<blockquote>小米发布机器人模型代码与检查点。</blockquote>'
        )
        self.assertEqual(extract_news_items(html)[0]["source"], "小米")

    def test_render_data_has_strict_date_fields(self):
        now = datetime(2026, 8, 6, 8, 0)
        data = build_render_data(
            "2026-08-06",
            extract_news_items(SAMPLE_HTML),
            "https://daily.juya.uk/issues/2026-08-06/",
            now=now,
        )
        self.assertEqual(data["date_cn"], "2026年8月6日")
        self.assertEqual(data["weekday"], "星期四")
        self.assertEqual(data["generated_at"], "2026/08/06 08:00")
        self.assertEqual(data["item_count"], 2)


if __name__ == "__main__":
    unittest.main()
