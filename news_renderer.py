"""RSS 结构化解析与日报图片模板。

仅使用 Python 标准库解析橘鸦日报，图片渲染交给 AstrBot 官方
``html_render`` 能力，插件本身不引入浏览器或图像处理依赖。
"""

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urlparse


WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


NEWS_IMAGE_TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: #f3f5ff; }
    body {
      color: #202126;
      font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei",
        "PingFang SC", "Hiragino Sans GB", Arial, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .sheet {
      width: 1120px;
      min-height: 1520px;
      padding: 62px 66px 48px;
      overflow: hidden;
      position: relative;
      background:
        radial-gradient(circle at 96% 2%, rgba(105, 83, 255, .12), transparent 24%),
        linear-gradient(180deg, #f8f9ff 0%, #f1f3ff 100%);
    }
    .sheet::before, .sheet::after {
      content: "";
      position: absolute;
      width: 22px;
      height: 22px;
      border-radius: 7px;
      background: #6b57ff;
      opacity: .13;
      top: 74px;
    }
    .sheet::before { left: 66px; }
    .sheet::after { right: 66px; }
    .rule { height: 1px; background: #cfd4e5; }
    .header {
      position: relative;
      min-height: 205px;
      padding-top: 48px;
      border-bottom: 1px solid #cfd4e5;
    }
    .brand {
      margin: 0;
      font-size: 58px;
      line-height: 1.08;
      font-weight: 900;
      letter-spacing: -2px;
    }
    .brand .accent { color: #6754ff; }
    .date-line {
      display: flex;
      align-items: center;
      gap: 17px;
      margin-top: 13px;
      color: #697184;
      font-size: 25px;
      font-weight: 700;
    }
    .date-line .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #6754ff;
    }
    .weekday {
      position: absolute;
      right: 0;
      top: 35px;
      z-index: 2;
      font-size: 88px;
      line-height: 1;
      font-weight: 950;
      letter-spacing: -8px;
      white-space: nowrap;
    }
    .slash {
      position: absolute;
      right: 18px;
      top: 69px;
      width: 430px;
      height: 2px;
      z-index: 1;
      background: #6b57ff;
      transform: rotate(17deg);
      transform-origin: right center;
      box-shadow: 0 0 0 1px rgba(107, 87, 255, .15);
    }
    .section-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin: 31px 0 2px;
    }
    .section-title h2 {
      margin: 0;
      color: #6754ff;
      font-size: 39px;
      line-height: 1.2;
      font-weight: 900;
      letter-spacing: .5px;
    }
    .section-title h2::before {
      content: "";
      display: inline-block;
      width: 18px;
      height: 18px;
      margin-right: 12px;
      border-radius: 5px;
      background: #6754ff;
      vertical-align: 4px;
    }
    .count {
      padding: 6px 13px;
      border: 1px solid #d8dbeb;
      border-radius: 999px;
      color: #747b8d;
      background: rgba(255, 255, 255, .58);
      font-size: 17px;
      font-weight: 700;
    }
    .news { padding: 0 0 2px; }
    .item {
      position: relative;
      margin-left: 25px;
      padding: 27px 0 27px;
      border-bottom: 1px solid #d7dbea;
    }
    .item::before {
      content: "";
      position: absolute;
      left: -25px;
      top: 38px;
      width: 11px;
      height: 11px;
      border-radius: 50%;
      background: #6754ff;
    }
    .item-head {
      display: flex;
      align-items: flex-start;
      gap: 12px;
    }
    .number {
      flex: 0 0 auto;
      min-width: 43px;
      padding-top: 5px;
      color: #6754ff;
      font-size: 18px;
      font-weight: 900;
      letter-spacing: .5px;
    }
    .title {
      margin: 0;
      color: #202126;
      font-size: 30px;
      line-height: 1.35;
      font-weight: 900;
      letter-spacing: .1px;
    }
    .summary {
      margin: 8px 0 0 55px;
      color: #687184;
      font-size: 21px;
      line-height: 1.52;
      font-weight: 620;
      letter-spacing: .1px;
    }
    .meta {
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 10px 0 0 55px;
      color: #2d2f36;
      font-size: 18px;
      font-weight: 800;
    }
    .category-break {
      display: flex;
      align-items: center;
      gap: 15px;
      margin: 34px 0 0;
      color: #6754ff;
      font-size: 24px;
      font-weight: 900;
    }
    .category-break::after {
      content: "";
      flex: 1;
      height: 1px;
      background: #d5d8e9;
    }
    .footer { margin-top: 54px; }
    .quote {
      margin: 23px 10px 27px;
      padding: 20px 24px;
      border-radius: 16px;
      color: #6754ff;
      background: rgba(195, 207, 239, .72);
      text-align: center;
      font-size: 22px;
      line-height: 1.45;
      font-weight: 800;
      box-shadow: inset 0 0 0 1px rgba(106, 84, 255, .05);
    }
    .footer-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: #697184;
      font-size: 17px;
      font-weight: 700;
    }
    .url {
      max-width: 610px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  </style>
</head>
<body>
  <main class="sheet">
    <div class="rule"></div>
    <header class="header">
      <h1 class="brand">AI 资讯快报 · <span class="accent">热闻</span></h1>
      <div class="date-line">
        <span>{{ date_cn | e }}</span><span class="dot"></span><span>每天读懂 AI 新变化</span>
      </div>
      <div class="slash"></div>
      <div class="weekday">{{ weekday | e }}</div>
    </header>

    <section class="section-title">
      <h2>{{ issue_date | e }}</h2>
      <span class="count">本期完整收录 {{ total_count }} 条</span>
    </section>

    <section class="news">
      {% for item in items %}
      {% if item.show_category %}
      <div class="category-break">{{ item.category | e }}</div>
      {% endif %}
      <article class="item">
        <div class="item-head">
          <span class="number">#{{ loop.index }}</span>
          <h3 class="title">{{ item.title | e }}</h3>
        </div>
        <p class="summary">{{ item.summary | e }}</p>
        <div class="meta">
          <span>来源：{{ item.source | e }}</span>
        </div>
      </article>
      {% endfor %}
    </section>

    <footer class="footer">
      <div class="rule"></div>
      <div class="quote">「追踪变化，理解趋势，让技术真正服务于人。」</div>
      <div class="footer-row">
        <span>生成时间：{{ generated_at | e }}</span>
        <span>数据来源：{{ source_name | e }}</span>
      </div>
      <div class="footer-row" style="margin-top:8px">
        <span class="url">{{ source_url | e }}</span>
        <span>Powered by AstrBot</span>
      </div>
    </footer>
  </main>
</body>
</html>
"""


def _normalize_text(value: str) -> str:
    """清理空白和日报中用于视频口播的 ``{文字|读音}`` 标记。"""
    value = re.sub(r"\{([^{}|]+)\|[^{}]+\}", r"\1", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _trim(value: str, limit: int) -> str:
    value = _normalize_text(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip("，。；、 ") + "…"


def source_name_from_url(url: str) -> str:
    """从新闻链接提取简洁、稳定的来源名。"""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    known_sources = (
        ("deepseek.com", "DeepSeek"),
        ("meta.ai", "Meta AI"),
        ("fb.com", "Meta"),
        ("facebook.com", "Meta"),
        ("google", "Google"),
        ("discoveryloop.com", "Discovery Loop"),
        ("cloudflare", "Cloudflare"),
        ("primeintellect.ai", "Prime Intellect"),
        ("commandcode.ai", "Command Code"),
        ("modelscope.cn", "魔搭社区"),
        ("meituan", "美团"),
        ("qwen", "通义千问"),
        ("alibabacloud", "阿里云"),
        ("bytedance", "字节跳动 Seed"),
        ("sand.ai", "Sand.ai"),
        ("jd.com", "京东"),
        ("xiaomi", "小米"),
        ("visa.com", "Visa"),
        ("ithome.com", "IT之家"),
        ("theinformation.com", "The Information"),
        ("x.com", "X"),
        ("mp.weixin.qq.com", "微信公众号"),
    )
    for needle, name in known_sources:
        if needle in host:
            return name
    if not host:
        return "橘鸦AI日报"
    parts = host.split(".")
    base = parts[-2] if len(parts) > 1 else parts[0]
    return base.replace("-", " ").title()


def _source_name_for_news(title: str, url: str) -> str:
    """修正聚合链接或原始链接明显错配时的来源展示。"""
    source = source_name_from_url(url)
    lowered = title.lower()
    if source in ("X", "微信公众号"):
        title_sources = (
            (("longcat", "美团"), "美团"),
            (("qwen", "千问"), "通义千问"),
            (("google",), "Google"),
            (("seed", "字节"), "字节跳动 Seed"),
        )
        for needles, name in title_sources:
            if any(needle.lower() in lowered for needle in needles):
                return name
    if source == "Visa" and ("小米" in title or "xiaomi" in lowered):
        return "小米"
    return source


class _DailyHTMLParser(HTMLParser):
    """提取详细新闻标题、导语、栏目与首个原始链接。"""

    def __init__(self, max_items: Optional[int]):
        super().__init__(convert_charrefs=True)
        self.max_items = max_items
        self.items: List[Dict[str, str]] = []
        self.section = ""
        self._capture = ""
        self._buffer: List[str] = []
        self._h3_url = ""
        self._pending: Dict[str, str] = {}

    def _at_limit(self) -> bool:
        return self.max_items is not None and len(self.items) >= self.max_items

    def handle_starttag(self, tag: str, attrs):
        if self._at_limit():
            return
        attrs_dict = dict(attrs)
        if tag in ("h2", "h3"):
            self._capture = tag
            self._buffer = []
            if tag == "h3":
                self._h3_url = ""
        elif tag == "a" and self._capture == "h3" and not self._h3_url:
            self._h3_url = attrs_dict.get("href", "")
        elif tag == "blockquote" and self._pending:
            self._capture = "blockquote"
            self._buffer = []

    def handle_data(self, data: str):
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str):
        if self._at_limit():
            return
        if tag == "h2" and self._capture == "h2":
            self.section = _normalize_text("".join(self._buffer))
            self._capture = ""
            self._buffer = []
        elif tag == "h3" and self._capture == "h3":
            text = _normalize_text("".join(self._buffer))
            number_match = re.search(r"#\s*(\d+)", text)
            if number_match and self._h3_url:
                title = re.sub(r"\s*#\s*\d+\s*$", "", text).strip()
                self._pending = {
                    "title": title,
                    "url": self._h3_url,
                    "category": "" if self.section == "概览" else self.section,
                }
            self._capture = ""
            self._buffer = []
        elif tag == "blockquote" and self._capture == "blockquote":
            summary = _normalize_text("".join(self._buffer))
            if self._pending and summary:
                item = dict(self._pending)
                item["summary"] = summary
                item["source"] = _source_name_for_news(item["title"], item["url"])
                self.items.append(item)
            self._pending = {}
            self._capture = ""
            self._buffer = []


def extract_news_items(
    raw_html: str, max_items: Optional[int] = None
) -> List[Dict[str, str]]:
    """从 RSS ``content:encoded`` 中提取全部新闻，失败时返回空列表。"""
    limit = max(1, max_items) if max_items is not None else None
    parser = _DailyHTMLParser(limit)
    try:
        parser.feed(raw_html or "")
        parser.close()
    except Exception:
        return []
    result = []
    previous_category = None
    for item in parser.items[:max_items]:
        category = _trim(item.get("category", ""), 12)
        result.append(
            {
                "title": _trim(item["title"], 72),
                "summary": _trim(item["summary"], 220),
                "source": _trim(item["source"], 32),
                "category": category,
                "show_category": bool(category and category != previous_category),
                "url": item.get("url", ""),
            }
        )
        previous_category = category
    return result


def build_render_data(
    article_date: str,
    items: List[Dict[str, str]],
    source_url: str,
    now: datetime = None,
    total_count: int = None,
) -> Dict:
    """构造模板数据。无效日期会安全回退到当前日期。"""
    now = now or datetime.now()
    try:
        issue_dt = datetime.strptime(article_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        issue_dt = now
        article_date = issue_dt.strftime("%Y-%m-%d")
    return {
        "issue_date": article_date,
        "date_cn": f"{issue_dt.year}年{issue_dt.month}月{issue_dt.day}日",
        "weekday": WEEKDAYS[issue_dt.weekday()],
        "generated_at": now.strftime("%Y/%m/%d %H:%M"),
        "source_name": "橘鸦AI日报",
        "source_url": source_url or "https://daily.juya.uk/",
        "item_count": len(items),
        "total_count": total_count if total_count is not None else len(items),
        "items": items,
    }
