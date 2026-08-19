"""内置工具：联网功能。

提供的联网能力（均为同步阻塞函数，交由 Tools.async_execute 的 to_thread 分支调度）：
    - web_search(query, max_results) : 网页搜索（DuckDuckGo HTML 端点，无需 API key）
    - web_fetch(url)                  : 抓取网页转纯文本

设计约定：
    - 返回类型为 str（与 super.py 中 write_latex / check_tikz 一致）。
    - 全部 try/except 兜底出错信息，避免因网络异常把整个 Agent 任务炸掉。
    - 依赖仅标准库（urllib / html.parser），不引入 requests/aiohttp 等额外依赖。
"""

import urllib.parse
import urllib.request
import html
import re
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
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    return urllib.request.urlopen(req, timeout=timeout)


def web_search(
    query: Annotated[str, "搜索关键词"],
    max_results: Annotated[int, "最多返回的结果条数"] = 5,
) -> str:
    """联网搜索页面（使用 DuckDuckGo HTML 端点，无需 API key），返回摘要列表。"""
    try:
        if max_results < 1:
            max_results = 1
        if max_results > 20:
            max_results = 20
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        body = _open(url, data=f"q={urllib.parse.quote(query)}".encode(), timeout=25).read().decode("utf-8", "ignore")

        links = re.findall(r'class="result__a"[^>]*>(.*?)</a>', body, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
        urls = re.findall(r'class="result__a" href="([^"]+)"', body)

        out = [f"搜索「{query}」结果："]
        count = 0
        for i, raw_link in enumerate(links):
            if count >= max_results:
                break
            title = html.unescape(re.sub(r"<[^>]+>", "", raw_link)).strip()
            if not title:
                continue
            snippet = html.unescape(re.sub(r"<[^>]+>", "", snippets[i])) if i < len(snippets) else ""
            real_url = ""
            if i < len(urls):
                m = re.search(r"uddg=(.*?)&", urls[i])
                if m:
                    real_url = urllib.parse.unquote(m.group(1)).strip()
            link = real_url or (urls[i] if i < len(urls) else "N/A")
            out.append(f"{count + 1}. {title}\n   链接: {link}\n   摘要: {snippet.strip()}")
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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(web_search(sys.argv[1]))
    else:
        print("用法: python built_in_tool.py <搜索关键词>")
