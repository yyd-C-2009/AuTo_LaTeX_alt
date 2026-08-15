import os
os.environ['ORT_PROVIDERS'] = 'DmlExecutionProvider,CPUExecutionProvider'
import asyncio,re
from pix2text import Pix2Text
import io
from Tools import Tools
from typing import Optional, Union, List, Dict, Any,Annotated,get_origin
import fitz
from PIL import Image
import onnxruntime

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

    
    async def recognize_image(self, image_path) -> dict[str,Any]:
        try:
            result = await asyncio.to_thread(
                self.p2t.recognize,
                image_path,
                return_text = True
            )
        except Exception as e:
            return {
                'success': 'False',
                'Error':str(e),
                'inline formular': [],
                'display formula': [],
                'whole text':  ''
            }
        result = LaTeX_math_get(result)
        # result['success'] = 'True'
        # loop = asyncio.get_event_loop()
        # result = await loop.run_in_executor(None, self.p2t.recognize, image_path)
        return result

    async def recognize_doc(
        self,
        doc_path: Annotated[str,'PDF/PNG/JPG存放路径'] = None,
        pages: Annotated[Optional[Union[str,list[int]]],'PDF页码编号, 总数不超过5'] = None,
        return_text:Annotated[bool,'是否返回文本类型'] = True
    )->dict[str,Any]:
        '''
        提取pdf/png/jpg中的文字, 返回一个json格式的字符串给API端口，由于模型大小限制，根据上下文请仔细甄别模型输出是否正确
        其中的格式说明：
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
            # pages = self._safe_page_parser(doc_path,pages=pages)

        if self.is_pdf(doc_path=doc_path):              #需要启动转换工具将PDF转换为字节流识别
            pages = self._safe_page_parser(pages=pages,pdf_path=doc_path)
            DPI = 150       #清晰度，为了避免Agent卡死线程，目前是不可调参数
            mar = fitz.Matrix(DPI/72,DPI/72)
            all_test_parts = []
            all_inline = []
            all_display = []

            with fitz.open(doc_path) as pdf_doc:
                max_pages = len(pdf_doc)
                todo_list = []
                for idx in pages:
                    if idx >= max_pages:
                        break
                    page = pdf_doc[idx]
                    pix = page.get_pixmap(matrix=mar,colorspace=fitz.csRGB)

                    image_bytes = pix.tobytes("png")
                    image = Image.open(io.BytesIO(image_bytes))

                    todo_list.append(asyncio.create_task(self.recognize_image(image_path=image)))

            result_list = await asyncio.gather(*todo_list)

            for result in result_list:
                if result['Success'] == 'False':
                    return result

                all_test_parts.append(result['whole text'])
                all_inline.extend(result['inline formular'])
                all_display.extend(result['display formula'])

            result = {
                'Success': 'True',
                'Error': '',
                'inline formular': all_inline,
                'display formula': all_display,
                'whole text': '\n'.join(all_test_parts)
            }

            return result

        else:

            return self.recognize_image(doc_path)

if __name__ == '__main__':
    p2t = Pix2Text(
        device='cpu',              # 必须为 'cpu'，因为 Pix2Text 的 'cuda' 会尝试 torch.cuda
        use_fast=True,
        providers=['DmlExecutionProvider']
    )
    print(f"当前 ONNX Runtime 可用 providers: {onnxruntime.get_available_providers()}")
    print(p2t.recognize('test.png', return_text=True))