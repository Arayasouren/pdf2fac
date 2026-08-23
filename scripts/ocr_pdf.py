#!/usr/bin/env python3
"""
扫描版 PDF 中文 OCR — macOS Vision 框架（本地、免费，无需 tesseract/PdfReader）

用 PyMuPDF 把每页渲染成图，再调 macOS Vision 的 VNRecognizeTextRequest
做中英文识别（zh-Hans 优先）。259 页摄影集实测约 1~3 秒/页。

Usage:
    python3 scripts/ocr_pdf.py <pdf> --out <目录> [--dpi 200]
    python3 scripts/ocr_pdf.py <pdf> --out <目录> --text-only   # 有文字层时直接抽取, 不 OCR

输出:
    <目录>/pages/page_001.txt ...   每页一个文本
    <目录>/full.txt                 按页序合并的全文（带页码分隔）
    <目录>/ocr_stats.json           每页字符数等统计

注意: OCR 模式需 PyMuPDF + pyobjc (macOS); --text-only 仅需 PyMuPDF
     （/usr/bin/python3 没有这两个包）。
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import fitz  # PyMuPDF
import Vision
from Foundation import NSURL

LANGS = ['zh-Hans', 'en-US']


def ocr_png(png_path):
    """对单张 PNG 做 Vision OCR，返回识别文本（按块拼接）。"""
    url = NSURL.fileURLWithPath_(str(png_path))
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(LANGS)
    req.setUsesLanguageCorrection_(True)

    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f'Vision OCR failed: {err}')

    lines = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if cands and len(cands) > 0:
            lines.append(str(cands[0].string()))
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='扫描版 PDF 中文 OCR (macOS Vision)')
    ap.add_argument('pdf', help='PDF 文件路径')
    ap.add_argument('--out', required=True, help='输出目录')
    ap.add_argument('--dpi', type=int, default=200, help='渲染 DPI (默认 200)')
    ap.add_argument('--start', type=int, default=1, help='起始页 (1-based, 默认 1)')
    ap.add_argument('--end', type=int, default=0, help='截止页 (1-based, 默认 0=最后一页)')
    ap.add_argument('--text-only', action='store_true',
                    help='直接抽取内嵌文字层, 不做 OCR (有文字层的 PDF 秒级完成)')
    args = ap.parse_args()

    pdf_path = Path(args.pdf).expanduser()
    out_dir = Path(args.out)
    pages_dir = out_dir / 'pages'
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    n_pages = len(doc)
    p_start = max(1, args.start)
    p_end = n_pages if args.end <= 0 else min(args.end, n_pages)

    print(f'=== OCR: {pdf_path.name} ===')
    print(f'页数: {n_pages}, 处理范围: {p_start}~{p_end}, DPI: {args.dpi}')
    print(f'输出: {out_dir}\n')

    stats = {}
    t0 = time.time()

    if args.text_only:
        for pno in range(p_start, p_end + 1):
            page_txt = pages_dir / f'page_{pno:03d}.txt'
            if page_txt.exists() and page_txt.stat().st_size > 0:
                stats[pno] = page_txt.stat().st_size
                continue
            text = doc[pno - 1].get_text().strip()
            page_txt.write_text(text, encoding='utf-8')
            stats[pno] = len(text)
            if pno % 50 == 0:
                print(f'  page {pno}/{p_end} ...', flush=True)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for pno in range(p_start, p_end + 1):
                page_txt = pages_dir / f'page_{pno:03d}.txt'
                if page_txt.exists() and page_txt.stat().st_size > 0:
                    stats[pno] = page_txt.stat().st_size
                    continue  # 断点续跑

                pix = doc[pno - 1].get_pixmap(dpi=args.dpi)
                png = tmp / f'p{pno}.png'
                pix.save(str(png))
                try:
                    text = ocr_png(png)
                except Exception as e:
                    text = ''
                    print(f'  page {pno}: OCR ERROR {e}', flush=True)
                png.unlink()

                page_txt.write_text(text, encoding='utf-8')
                stats[pno] = len(text)
                done = pno - p_start + 1
                total = p_end - p_start + 1
                eta = (time.time() - t0) / done * (total - done)
                print(f'  page {pno}/{p_end}: {len(text)} chars '
                  f'({done}/{total}, ETA {eta/60:.1f}min)', flush=True)

    # 合并全文
    full_path = out_dir / 'full.txt'
    with open(full_path, 'w', encoding='utf-8') as f:
        for pno in range(p_start, p_end + 1):
            page_txt = pages_dir / f'page_{pno:03d}.txt'
            if page_txt.exists():
                f.write(f'\n===== 第 {pno} 页 =====\n')
                f.write(page_txt.read_text(encoding='utf-8'))
                f.write('\n')

    (out_dir / 'ocr_stats.json').write_text(
        json.dumps({str(k): v for k, v in stats.items()}, indent=1), encoding='utf-8')

    total_chars = sum(stats.values())
    print(f'\n=== DONE in {time.time()-t0:.0f}s ===')
    print(f'总字符数: {total_chars:,} -> {full_path}')
    empty = [p for p, c in stats.items() if c == 0]
    if empty:
        print(f'无文字页(纯照片页): {len(empty)} 页')


if __name__ == '__main__':
    main()
