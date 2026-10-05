"""Render curated public Traditional Chinese voucher pages; never synthesize content."""
import csv
import hashlib
import html
import json
import zipfile
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

ROOT = Path(__file__).resolve().parents[1] / 'ref/public_zh_tw_validation_100'
# Page numbers are 1-based PDF pages, not the page numbers printed in slides.
SELECT = {
 'S01':[7,9,10,11,13,16,18,19], 'S02':[16,17],
 'S03':[92,93,94,99], 'S04':[44,45,46],
 'S06':[10,11,12,13,14,15,18,20,27], 'S07':[19,20],
 'S08':[41,42,43], 'S09':[6,8],
 'S13':[1], 'S16':[1], 'S18':[1], 'S19':[1], 'S20':[1],
 'S21':[2,5,6,9,10,11,13,15,17], 'S22':[3,6,7],
 'S24':[1,3,5,6,10,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44],
 'S25':[2,8,10,12,14,16], 'S26':[1,2,3,4,5,6],
 'S27':[10,11,12,13,14,15,16,18,20,22,23,24],
 'S28':[26,27,28,29,53],
}
TYPES = {
 'S01':['收據','電子發票／收據','手寫發票','收銀機發票','手寫收據','郵政憑證','保險收據','旅行業收據'],
 'S02':['手寫發票','手寫收據'], 'S03':['發票與收據','電子發票','旅行業收據','繳費與捐款收據'],
 'S04':['手寫發票','收銀機發票','手寫收據'],
 'S06':['手寫發票','手寫發票','收銀機發票','收銀機發票','電子發票','手寫收據','交通票證','旅行業收據','手寫收據'],
 'S07':['手寫收據','手寫收據'], 'S08':['發票與明細','發票與收據','電子／收銀機發票'],
 'S09':['手寫收據','收據與發票'], 'S13':['手寫收據'], 'S16':['電子發票'],
 'S18':['電子發票'], 'S19':['收銀機發票'], 'S20':['收銀機發票'],
 'S21':['手寫發票','手寫發票','手寫發票','收銀機發票','收銀機發票','電子發票','電子發票','手寫收據','手寫收據'],
 'S22':['多張發票黏貼頁','多張發票黏貼頁','手寫收據'],
 'S24':['手寫發票','手寫收據','發票與核銷附件','發票與核銷附件','手寫發票','保險收據','郵政憑證','郵政憑證','手寫發票','手寫發票','保險收據','手寫發票','手寫發票','手寫收據','手寫收據','手寫收據','手寫發票','手寫發票','手寫發票','手寫發票','手寫收據'],
 'S25':['手寫收據','手寫收據','手寫收據','手寫收據','手寫收據','手寫發票'],
 'S26':['手寫發票','收銀機發票','手寫收據','手寫發票','手寫發票','手寫收據'],
 'S27':['收銀機發票','收銀機發票','手寫發票','手寫發票','電子發票','手寫收據','手寫收據','手寫收據','手寫收據','手寫收據','手寫收據','手寫收據'],
 'S28':['收銀機發票','電子發票','手寫收據','發票與明細','旅行業收據'],
}

def main():
    assert sum(map(len, SELECT.values())) == 100
    sources = {s['id']: s for s in json.loads((ROOT/'source_manifest.json').read_text(encoding='utf-8'))}
    (ROOT/'samples').mkdir(exist_ok=True)
    (ROOT/'thumbnails').mkdir(exist_ok=True)
    records = []
    for sid, pages in SELECT.items():
        assert len(pages) == len(TYPES[sid])
        src = sources[sid]
        doc = pdfium.PdfDocument(ROOT / src['file'])
        for p, kind in zip(pages, TYPES[sid]):
            sample_id = f'TW{len(records)+1:03d}'
            filename = f'{sample_id}_{sid}_p{p:03d}.jpg'
            page = doc[p-1]
            scale = min(200/72, 2800/max(page.get_size()))
            im = page.render(scale=scale).to_pil().convert('RGB')
            dest = ROOT/'samples'/filename
            im.save(dest, quality=94, subsampling=0)
            assert dest.stat().st_size < 5*1024*1024
            thumb = im.copy()
            thumb.thumbnail((360,360))
            thumb.save(ROOT/'thumbnails'/filename, quality=85)
            flags = ['公開教材範例', '可能含教學文字或標註']
            if sid in ('S24','S25') or (sid=='S27' and p>=18) or (sid=='S06' and p==27):
                flags.append('原教材錯誤／缺漏範例，需人工判讀')
            if sid=='S26': flags.append('低畫質黑白掃描')
            if sid=='S06' and p==20: flags.append('含英文附件')
            records.append(dict(sample_id=sample_id, file='samples/'+filename,
                language='zh-Hant-TW', type_hint=kind, flags='；'.join(flags),
                source_id=sid, publisher=src['publisher'], source_title=src['title'],
                source_url=src['url'], original_pdf=src['file'], pdf_page=p,
                width=im.width, height=im.height, bytes=dest.stat().st_size,
                sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),
                ground_truth_status='未標註', transformation='原始 PDF 單頁渲染 JPG；未裁切、未生成或修改單據內容'))
            page.close()
        doc.close()
    assert len({r['sha256'] for r in records}) == 100
    assert len(list((ROOT/'samples').glob('*.jpg'))) == 100
    for r in records:
        with Image.open(ROOT/r['file']) as im: im.verify()
    (ROOT/'manifest.json').write_text(json.dumps(records, ensure_ascii=False, indent=2),encoding='utf-8')
    with (ROOT/'manifest.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
    annotations=[dict(sample_id=r['sample_id'],file=r['file'],reviewed=False,
        documents=[], expected_ocr_text=None, expected_journal_entries=None,
        actual_result=None, passed=None, notes='') for r in records]
    (ROOT/'annotation_template.json').write_text(json.dumps(annotations,ensure_ascii=False,indent=2),encoding='utf-8')
    counts=dict(Counter(r['type_hint'] for r in records))
    report=dict(sample_count=100,selected_source_pdf_count=len(SELECT),
        publisher_count=len({r['publisher'] for r in records}),
        unique_file_hashes=100,all_images_decode=True,all_files_under_5MiB=True,
        largest_sample_bytes=max(r['bytes'] for r in records),type_hint_counts=counts,
        visual_review='人工查看來源候選頁聯絡表，排除空白、純文字及明顯重複標註頁；未宣稱完成跨來源交易層級去重',
        model_inference_run=False,ground_truth_available=False)
    (ROOT/'validation_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    cards=[]
    for r in records:
        esc=lambda value:html.escape(str(value),quote=True)
        cards.append(f'''<article data-search="{esc(r['sample_id']+' '+r['type_hint']+' '+r['publisher'])}">
<a href="{esc(r['file'])}" target="_blank"><img loading="lazy" src="thumbnails/{Path(r['file']).name}" alt="{esc(r['sample_id'])}"></a>
<h2>{esc(r['sample_id'])} · {esc(r['type_hint'])}</h2><p>{esc(r['publisher'])} · PDF 第 {r['pdf_page']} 頁</p>
<p class="muted">{esc(r['flags'])}</p><a href="{esc(r['original_pdf'])}#page={r['pdf_page']}">原始 PDF</a> · <a href="{esc(r['source_url'])}">公開來源</a></article>''')
    (ROOT/'index.html').write_text('''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>繁體中文單據驗證資料 100 份</title>
<style>body{font-family:system-ui,sans-serif;margin:32px;background:#f2f5f8;color:#163047}h1{font-size:28px}input{padding:12px;width:min(90%,480px);margin:12px 0 24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:20px}article{background:white;border:1px solid #d8e1e9;border-radius:12px;padding:16px}img{width:100%;height:280px;object-fit:contain}h2{font-size:17px}.muted{font-size:13px;color:#64748b}a{color:#0868b7}</style>
<h1>繁體中文公開單據 · 100 個測試頁</h1><p>從公開 PDF 篩選及轉製的 JPG，可直接上傳。含教學標註與多張單據同頁；沒有人工確認的 OCR／會計分錄標準答案。</p>
<p><a href="README.md">使用說明</a> · <a href="manifest.csv">來源清單 CSV</a> · <a href="validation_report.json">檔案檢查結果</a></p>
<input id="search" placeholder="搜尋編號、類型或來源機構"><div class="grid">'''+''.join(cards)+'''</div><script>document.querySelector('#search').addEventListener('input',e=>{for(const card of document.querySelectorAll('article'))card.hidden=!card.dataset.search.toLowerCase().includes(e.target.value.toLowerCase())})</script></html>''',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
