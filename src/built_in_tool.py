"""内置工具：联网功能。

提供的联网能力（均为同步阻塞函数，交由 Tools.async_execute 的 to_thread 分支调度）：
    - web_search(query, max_results) : 网页搜索（Bing RSS 端点，无需 API key；DuckDuckGo 不可达时替代）
    - web_fetch(url)                  : 抓取网页转纯文本

设计约定：
    - 返回类型为 str（与 super.py 中 write_latex / check_tikz 一致）。
    - 全部 try/except 兜底出错信息，避免因网络异常把整个 Agent 任务炸掉。
    - 依赖仅标准库（urllib / html.parser / xml.etree），不引入 requests/aiohttp 等额外依赖。
"""

import urllib.parse
import urllib.request
import json
import html
import re
import xml.etree.ElementTree as ET
from typing import Annotated
from html.parser import HTMLParser


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


class _TextExtractor(HTMLParser):
    """把 HTML 转成纯文本，跳过 script/style 内容，按块拼接。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._pieces: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in ("p", "br", "div", "li", "h1", "h2", "h3", "tr"):
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("p", "div", "li", "h1", "h2", "h3"):
            self._pieces.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._pieces.append(data)

    def get_text(self, limit: int = 20000) -> str:
        text = "".join(self._pieces)
        # 压缩空白/换行
        text = re.sub(r"[ \t\r]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text)
        text = re.sub(r"\n +", "\n", text)
        return text.strip()[:limit]


def _open(url: str, data: bytes | None = None, timeout: float = 20.0):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _strip_html(text: str | None) -> str:
    """把可能含 HTML 标签的文本清洗为纯文本。"""
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def web_search(
    query: Annotated[str, "搜索关键词"],
    max_results: Annotated[int, "最多返回的结果条数"] = 5,
) -> str:
    """联网搜索（使用 Bing RSS 端点，无需 API key），返回摘要列表。"""
    try:
        if not query or not query.strip():
            return "搜索关键词不能为空"
        query = query.strip()
        if max_results < 1:
            max_results = 1
        if max_results > 20:
            max_results = 20
        url = "https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "format": "rss", "count": str(max_results)}
        )
        body = _open(url, timeout=25).read().decode("utf-8", "ignore")
        root = ET.fromstring(body)
        items = root.findall(".//item")

        out = [f"搜索「{query}」结果："]
        count = 0
        for item in items:
            if count >= max_results:
                break
            title = _strip_html(item.findtext("title"))
            link = _strip_html(item.findtext("link"))
            snippet = _strip_html(item.findtext("description"))
            if not title and not link:
                continue
            out.append(f"{count + 1}. {title or 'N/A'}\n   链接: {link or 'N/A'}\n   摘要: {snippet}")
            count += 1
        if count == 0:
            return "未获取到搜索结果（可能是网络问题或搜索引擎拒绝请求）。"
        return "\n\n".join(out)
    except Exception as e:
        return f"联网搜索失败: {type(e).__name__}: {e}"


def web_fetch(
    url: Annotated[str, "要抓取的网页 URL"],
    max_chars: Annotated[int, "返回的最大字符数"] = 12000,
) -> str:
    """抓取网页并把 HTML 转成纯文本，返回其正文（截断前 max_chars 字符）。"""
    try:
        resp = _open(url, timeout=25)
        enc = resp.headers.get_content_charset() or "utf-8"
        body = resp.read().decode(enc, "ignore")
        parser = _TextExtractor()
        parser.feed(body)
        text = parser.get_text(max_chars)
        if not text:
            return "该网页未抓到可读文本（可能是重定向、动态渲染或反爬）。"
        return text
    except Exception as e:
        return f"网页抓取失败: {type(e).__name__}: {e}"


def get_weather(
    city: Annotated[str, "城市名称（支持中文/拼音/英文，如 北京 / Beijing）"],
    unit: Annotated[str, "温度单位，celsius=摄氏(默认) / fahrenheit=华氏"] = "celsius",
) -> str:
    """查询某个城市的实时天气（数据来自 wttr.in，无需 API key）。返回温度、体感、天气、湿度、风速。"""
    try:
        if not city or not city.strip():
            raise ValueError("城市不能留空")
        unit = unit.lower() if unit else "celsius"
        if unit not in ("celsius", "celsius", "c", "fahrenheit", "f"):
            unit = "celsius"
        url = "https://wttr.in/" + urllib.parse.quote(city.strip()) + "?format=j1"
        body = _open(url, timeout=25).read().decode("utf-8", "ignore")
        data = json.loads(body)
        cur = (data.get("current_condition") or [{}])[0]
        temp_key = "temp_" + ("C" if unit.startswith("c") else "F")
        feels_key = "FeelsLike" + ("C" if unit.startswith("c") else "F")
        temp = cur.get(temp_key, "?")
        feels = cur.get(feels_key, "?")
        desc = ""
        wd = cur.get("weatherDesc") or [{}]
        if wd:
            desc = wd[0].get("value", "")
        humidity = cur.get("humidity", "?")
        wind = cur.get("windspeedKmph", "?")
        area = data.get("nearest_area") or [{}]
        place = ""
        if area:
            place = ", ".join(x.get("value", "") for x in area[0].get("areaName", []) if x.get("value"))
        unit_symbol = "°C" if unit.startswith("c") else "°F"
        return (
            f"{city}({place}) 实时天气: {desc}, {temp}{unit_symbol}, "
            f"体感 {feels}{unit_symbol}, 湿度 {humidity}%, 风速 {wind} km/h"
        )
    except json.JSONDecodeError:
        return f"天气服务返回数据解析失败（{city}），请稍后再试或更换城市写法。"
    except Exception as e:
        return f"天气查询失败: {type(e).__name__}: {e}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(web_search(sys.argv[1]))
    else:
        print("用法: python built_in_tool.py <搜索关键词>")
