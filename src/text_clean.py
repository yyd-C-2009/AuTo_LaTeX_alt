# -*- coding: utf-8 -*-
"""LaTeX 输出文本订正：把中文标点替换为等宽的英文字符，避免 LaTeX 编译时报 Warning。

LLM 直接输出的 LaTeX 里常混入中文标点（。，：；！？（）等），
这些字符在 LaTeX 源码里会导致编译警告甚至乱码，统一替换为等宽英文字符。

用法：
    from text_clean import text_clean
    cleaned = text_clean(raw_string)
"""


def text_clean(text: str) -> str:
    '''订正 LaTeX 输出文本中的中文标点，替换为等宽的英文/LaTeX 符号。'''
    if not isinstance(text, str):
        return text

    # 先替换「成对出现」的双字符（避免拆成两个重复符号）
    _PAIR_MAP = {
        '……': '\\ldots',   # 中文省略号
        '——': ' --- ',      # 中文破折号 -> em dash
    }
    for ch, repl in _PAIR_MAP.items():
        text = text.replace(ch, repl)

    # 再替换单个中文标点
    _SINGLE_MAP = {
        '。': '. ',
        '，': ', ',
        '、': ', ',
        '：': ': ',
        '；': '; ',
        '！': '! ',
        '？': '? ',
        '（': '(',
        '）': ')',
        '“': '``',
        '”': "''",
        '‘': '`',
        '’': "'",
        '《': '<',
        '》': '>',
        '【': '[',
        '】': ']',
        '　': ' ',        # 全角空格 -> 普通空格
        '—': '---',       # 单破折号（不成对时）-> em dash
        '…': '\\ldots',   # 单省略号
    }
    for ch, repl in _SINGLE_MAP.items():
        text = text.replace(ch, repl)

    return text


if __name__ == '__main__':
    demo = '他说：“你好，欢迎使用LaTeX。”这是一个——测试——结束……综上：1、2、3。'
    print('before:', demo)
    print('after :', text_clean(demo))
