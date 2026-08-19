import os
os.environ['ORT_PROVIDERS'] = 'DmlExecutionProvider,CPUExecutionProvider'
os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')  # 无 HF 环境时默认走 hf-mirror 中文镜像下载 Pix2Text 模型
import asyncio,re
from pix2text import Pix2Text
import io
import hashlib
import json
from Tools import Tools
from typing import Optional, Union, List, Dict, Any,Annotated,get_origin
import fitz
from PIL import Image
import onnxruntime

OCR_CACHE_DIR = "OCR_result"   # OCR 结果缓存目录：按「文件名 + 内容 md5」长期保存识别结果

def LaTeX_math_get(_string:str) -> dict[str,]:
    display_formula = re.findall(r'\$\$(.*?)\$\$',_string,re.DOTALL)
    display_formula = [m.strip() for m in display_formula]
    _cleand_text = re.sub(r'\$\$(.*?)\$\$','',_string,flags=re.DOTALL)
    inline_formula  = re.findall(r'\$(.*?)\$',_cleand_text,re.DOTALL)
    inline_formula = [m.strip() for m in inline_formula]
    return {
        'Success': 'True',
        'inline formular': inline_formula,
        'display formula': display_formula,
        'whole text':  _string
    }

class Visal:
    ''' Visal 类用于处理图像识别任务，使用 Pix2Text 模型进行图像到文本的转换。持有全局唯一p2t对象，避免重复加载模型。'''
    def __init__(self):
        os.environ['ORT_PROVIDERS'] = 'DmlExecutionProvider,CPUExecutionProvider'
        self.p2t = Pix2Text(device='cpu',use_fast = True,providers=['DmlExecutionProvider', 'CPUExecutionProvider'])  # 强制CPU模式, 单线程处理加载
        print("----------p2t loaded----------")

    def is_pdf(self,doc_path:str)->bool:
        return doc_path.lower().endswith('.pdf')

    def _safe_page_parser(
        self,
        pdf_path: str = None,
        pages: Optional[Union[List[int], str]] = None
    ) -> List[int]:
        
        if pages is None:

            return list(range(0,5))

        elif not isinstance(pages,list) or pages and not isinstance(pages[0],int):
            raise ValueError('PDF 文件 pages 必须为整数列表')

        return pages

    
    def recognize_image(self, image_path) -> dict[str,Any]:
        '''同步 OCR 单张图片（image_path 可为路径字符串或 PIL.Image 对象）'''
        try:
            result = self.p2t.recognize(image_path, return_text=True)  # 同步阻塞，交由 Tools.async_execute 的 to_thread 调度
        except Exception as e:
            return {
                'Success': 'False',
                'Error': str(e),
                'inline formular': [],
                'display formula': [],
                'whole text':  ''
            }
        return LaTeX_math_get(result)

    def _cache_path(self, doc_path: str, page: int | None = None) -> str:
        '''生成缓存文件路径：文件名 + 内容md5 [+ 页码]，存为 .json
        page=None 表示整文件（单张图片），page=整数 表示 PDF 的第 page 页'''
        with open(doc_path, 'rb') as f:
            md5 = hashlib.md5(f.read()).hexdigest()[:16]
        base = os.path.splitext(os.path.basename(doc_path))[0]
        if page is None:
            return os.path.join(OCR_CACHE_DIR, f"{base}_{md5}.json")
        return os.path.join(OCR_CACHE_DIR, f"{base}_{md5}_p{page}.json")

    def _load_cache(self, cache_path: str) -> dict | None:
        '''加载缓存'''
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def _save_cache(self, cache_path: str, result: dict) -> None:
        os.makedirs(OCR_CACHE_DIR, exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)

    def recognize_doc(
        self,
        doc_path: Annotated[str,'PDF/PNG/JPG存放路径'] = None,
        pages: Annotated[Optional[Union[str,list[int]]],'PDF页码编号, 最多识别5页，超出自动截断为前5页'] = None,
        return_text:Annotated[bool,'是否返回文本类型'] = True
    )->dict[str,Any]:
        '''
        提取pdf/png/jpg中的文字（同步阻塞，由 Tools.async_execute 的 to_thread 调度）。
        带缓存：PDF 按「页」缓存（文件名+内容md5+页码），单张图片按「文件」缓存，
        存于 OCR_result/ 目录，命中则跳过 OCR。
        返回格式：
        {
            Success: 是否成功读取
            Error: (若失败) 错误原因
            inline formular: 行内公式提取, 返回list
            display formula: 行间公式提取, 返回list
            whole text: 全文提取, 返回str
        }
        '''
        if not os.path.exists(path=doc_path):
            return {
                'Success': 'False',
                'Error': f'File {doc_path} do not exist',
                'inline formular': [],
                'display formula': [],
                'whole text':  ''
            }

        if self.is_pdf(doc_path) and pages is None:
            raise ValueError("PDF文件的页码参数pages不能为空, 请指定页码范围或页码列表, 总数不超过5页")

        # 先归一化 pages（PDF 才需要）
        if self.is_pdf(doc_path):
            pages = self._safe_page_parser(pages=pages, pdf_path=doc_path)
            pages = pages[:5]            # 页数硬上限 5：超出截断（防资源耗尽，docstring 声明的约束在代码层落实）

        if self.is_pdf(doc_path=doc_path):              # PDF：逐页缓存 + 逐页 OCR
            DPI = 150
            mar = fitz.Matrix(DPI/72, DPI/72)
            all_text_parts = []
            all_inline = []
            all_display = []

            with fitz.open(doc_path) as pdf_doc:
                max_pages = len(pdf_doc)
                for idx in pages:
                    if idx >= max_pages:
                        break

                    # —— 该页缓存检查：命中直接用，跳过该页 OCR ——
                    page_cache = self._cache_path(doc_path, page=idx)
                    cached = self._load_cache(page_cache)
                    if cached is not None:
                        print(f"[OCR缓存命中] {page_cache}")
                        r = cached
                    else:
                        page = pdf_doc[idx]
                        pix = page.get_pixmap(matrix=mar, colorspace=fitz.csRGB)
                        image_bytes = pix.tobytes("png")
                        image = Image.open(io.BytesIO(image_bytes))

                        r = self.recognize_image(image)
                        if r['Success'] == 'False':
                            return r
                        # 单页结果单独缓存
                        self._save_cache(page_cache, r)

                    all_text_parts.append(r['whole text'])
                    all_inline.extend(r['inline formular'])
                    all_display.extend(r['display formula'])

            return {
                'Success': 'True',
                'Error': '',
                'inline formular': all_inline,
                'display formula': all_display,
                'whole text': '\n'.join(all_text_parts)
            }

        # —— 单张图片：整文件缓存 ——
        cache_path = self._cache_path(doc_path)
        cached = self._load_cache(cache_path)
        if cached is not None:
            print(f"[OCR缓存命中] {cache_path}")
            return cached

        result = self.recognize_image(doc_path)

        # —— 写入缓存 ——
        if result.get('Success') == 'True':
            self._save_cache(cache_path, result)
        return result

if __name__ == '__main__':
    p2t = Pix2Text(
        device='cpu',              # 必须为 'cpu'，因为 Pix2Text 的 'cuda' 会尝试 torch.cuda
        use_fast=True,
        providers=['DmlExecutionProvider']
    )
    print(f"当前 ONNX Runtime 可用 providers: {onnxruntime.get_available_providers()}")
    print(p2t.recognize('test2.png', return_text=True))