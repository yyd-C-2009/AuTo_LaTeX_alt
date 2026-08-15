import asyncio,re,os
from pix2text import Pix2Text
from Tools import Tools
from typing import Optional, Union, List, Dict, Any,Annotated,get_origin
import fitz
from PIL import Image

async def ocr_document(
    self,
    doc_path: str,
    pages: Optional[List[int]] = None,
    return_text: bool = True
) -> Dict[str, Any]:
    """
    提取 PDF/PNG/JPG 中的文字与公式（PDF 自动转图片渲染）
    """
    # ---------- 1. 前置校验（确定性门控） ----------
    if not os.path.exists(doc_path):
        return {'Success': False, 'Error': f'文件不存在: {doc_path}', 
                'inline formular': [], 'display formula': [], 'whole text': ''}

    # 处理 PDF 页码（默认前5页，强制限制最多5页）
    if pages is None:
        pages_to_process = list(range(5))  # 默认 0-4
    else:
        # 确保是列表，且不超过5页
        if not isinstance(pages, list):
            return {'Success': False, 'Error': 'pages 参数必须为整数列表', 
                    'inline formular': [], 'display formula': [], 'whole text': ''}
        pages_to_process = pages[:5]  # 硬截断，防止 OOM

    # ---------- 2. 加载 Pix2Text（懒加载，放入线程池） ----------
    try:
        p2t = await asyncio.to_thread(self._get_p2t)
    except Exception as e:
        return {'Success': False, 'Error': f'P2T 模型加载失败: {str(e)}', 
                'inline formular': [], 'display formula': [], 'whole text': ''}

    # ---------- 3. 核心识别分支 ----------
    try:
        if self._is_pdf(doc_path):
            # ---------- 3-A: PDF 处理（使用 pymupdf + with 资源管理） ----------
            # 注意：所有文件操作均使用 with...as... 保证句柄释放
            all_text_parts = []
            all_inline = []
            all_display = []

            # 【关键资源管理点 1】：使用 with 打开 PDF 文档
            # 即使内部抛出异常，fitz 也会确保文件句柄被操作系统回收
            with fitz.open(doc_path) as pdf_doc:
                # 校验页码范围是否超出文档总页数
                max_pages = len(pdf_doc)
                for idx in pages_to_process:
                    if idx >= max_pages:
                        continue  # 跳过无效页码
                    
                    # 【关键资源管理点 2】：获取页面像素图（返回内存对象，无文件句柄）
                    # 使用缩放矩阵控制 DPI（150 兼顾速度与精度）
                    mat = fitz.Matrix(150/72, 150/72)
                    page = pdf_doc[idx]
                    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
                    
                    # 转为字节流（内存中操作，无需临时文件）
                    img_data = pix.tobytes("png")  # pix 对象无需显式 close，随方法结束释放
                    
                    # 在内存中打开图像（PIL 的 Image.open 支持字节流）
                    # 注意：这里仍然建议使用 with 吗？PIL 对于字节流没有上下文管理器，
                    # 但图像对象在识别完成后会被 GC 回收。我们将其作为参数传入即可。
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(img_data))
                    
                    # 执行识别（放入线程池）
                    # 注意：Pix2Text.recognize 接受 PIL Image 对象，全程无临时文件
                    result = await asyncio.to_thread(
                        p2t.recognize, 
                        img,  # 直接传内存图像
                        return_text=True,
                        page_number=idx  # 部分场景用于调试，非必须
                    )
                    
                    # 后处理：提取文本和公式
                    if result and isinstance(result, str):
                        all_text_parts.append(f"--- Page {idx+1} ---\n{result}")
                        # 提取行内公式 ( $...$ )
                        inline_matches = re.findall(r'\$(.*?)\$', result, re.DOTALL)
                        # 提取行间公式 ( $$...$$ )
                        display_matches = re.findall(r'\$\$(.*?)\$\$', result, re.DOTALL)
                        all_inline.extend(inline_matches)
                        all_display.extend(display_matches)

            # 合并文本
            full_text = "\n\n".join(all_text_parts)
            return {
                'Success': True,
                'Error': None,
                'inline formular': list(dict.fromkeys(all_inline)),  # 去重
                'display formula': list(dict.fromkeys(all_display)),
                'whole text': full_text
            }

        else:
            # ---------- 3-B: 图片处理（单张，直接识别） ----------
            # 图片直接传入，无需 with 管理文件，因为只读且无句柄持久化
            # 但仍用 try-finally 确保可能的临时状态清理（P2T 内部处理）
            result = await asyncio.to_thread(
                p2t.recognize, 
                doc_path, 
                return_text=return_text
            )
            # 提取公式...
            if result and isinstance(result, str):
                inline_matches = re.findall(r'\$(.*?)\$', result, re.DOTALL)
                display_matches = re.findall(r'\$\$(.*?)\$\$', result, re.DOTALL)
                return {
                    'Success': True,
                    'Error': None,
                    'inline formular': inline_matches,
                    'display formula': display_matches,
                    'whole text': result
                }
            else:
                return {'Success': False, 'Error': '图片识别返回空结果', 
                        'inline formular': [], 'display formula': [], 'whole text': ''}

    except Exception as e:
        # 捕获所有未知异常（包括内存问题）
        return {'Success': False, 'Error': f'P2T OCR Error:{str(e)}', 
                'inline formular': [], 'display formula': [], 'whole text': ''}