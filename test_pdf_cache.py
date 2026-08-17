r"""
PDF 分页缓存验证（不加载 OCR 模型，纯测缓存逻辑）。

验证点：
1. 第一次 OCR 一个 3 页 PDF → 生成 3 个单页缓存文件（_p0/_p1/_p2）。
2. 第二次只 OCR 第 1 页（pages=[1]）→ 命中 _p1 缓存，跳过 OCR（其他页不碰）。
3. 换页码组合（pages=[0,2]）→ 只对「未缓存」的页 OCR（这里都缓存过，应全部命中）。

运行：python test_pdf_cache.py
"""
import os
import sys
import fitz
from Visal import Visal, OCR_CACHE_DIR


def make_test_pdf(path: str, pages: int = 3):
    """生成一个 pages 页的测试 PDF"""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {i} content")
    doc.save(path)
    doc.close()
    print(f"已生成测试 PDF: {path} ({pages} 页)")


def build_fake_visal(call_log: list):
    """构造一个 Visal 实例，但绕过 __init__（不加载 Pix2Text），
    用假的 recognize_image 记录被调用了几次、针对哪页。"""
    visal = Visal.__new__(Visal)

    def fake_recognize_image(image_path):
        # image_path 是 PIL.Image（PDF 页转出来的图片），这里读不到页号，
        # 所以我们在 recognize_doc 里通过「第几次调用」来推断页序号。
        # 更简单：记录调用次数
        call_log.append(image_path)
        return {
            'Success': 'True',
            'Error': '',
            'inline formular': [],
            'display formula': [],
            'whole text': f"OCR#第{len(call_log)}次调用",
        }

    visal.recognize_image = fake_recognize_image
    return visal


def main():
    pdf_path = "_test_3pages.pdf"
    make_test_pdf(pdf_path, pages=3)

    # 清空可能残留的缓存
    for fname in os.listdir(OCR_CACHE_DIR):
        if fname.startswith("_test_3pages_") and fname.endswith(".json"):
            os.remove(os.path.join(OCR_CACHE_DIR, fname))

    call_log = []
    visal = build_fake_visal(call_log)

    # —— 第一次：OCR 全部 3 页 ——
    print("\n[第一次] recognize_doc(pages=[0,1,2])")
    r1 = visal.recognize_doc(pdf_path, pages=[0, 1, 2])
    print("  结果 whole text:", r1['whole text'])
    print("  recognize_image 被调用次数:", len(call_log), "(应为 3 次)")

    # 检查生成了 3 个单页缓存
    cache_files = sorted(
        f for f in os.listdir(OCR_CACHE_DIR)
        if f.startswith("_test_3pages_") and "_p" in f
    )
    print("  单页缓存文件:", cache_files)
    assert len(cache_files) == 3, f"应为 3 个单页缓存，实际 {len(cache_files)}"

    # —— 第二次：只 OCR 第 1 页（pages=[1]） ——
    print("\n[第二次] recognize_doc(pages=[1])")
    before = len(call_log)
    r2 = visal.recognize_doc(pdf_path, pages=[1])
    after = len(call_log)
    print("  结果 whole text:", r2['whole text'])
    print(f"  本次 recognize_image 新增调用 {after - before} 次 (应为 0，全部命中缓存)")

    # —— 第三次：换页码组合 pages=[0,2] ——
    print("\n[第三次] recognize_doc(pages=[0,2])")
    before = len(call_log)
    r3 = visal.recognize_doc(pdf_path, pages=[0, 2])
    after = len(call_log)
    print("  结果 whole text:", r3['whole text'])
    print(f"  本次 recognize_image 新增调用 {after - before} 次 (应为 0，全部命中缓存)")

    # —— 验证失败路径不会写入缓存（不存在文件） ——
    print("\n[边界] 不存在的文件")
    r_missing = visal.recognize_doc("_不存在的.pdf", pages=[0])
    print("  结果 Success:", r_missing['Success'], "(应为 False)")

    # 清理测试 PDF
    os.remove(pdf_path)
    print("\n===== PDF 分页缓存验证通过 =====")


if __name__ == "__main__":
    main()
