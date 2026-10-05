"""Download explicitly selected public sources; inspect PDF pages for dataset curation.

No model inference, application database writes, or fabricated sample generation.
"""
import concurrent.futures
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from pip._vendor import truststore

# Honor the Windows certificate store (including managed-network root CAs).
truststore.inject_into_ssl()
import requests
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[1] / 'ref' / 'public_zh_tw_validation_100'
ROOT.mkdir(parents=True, exist_ok=True)

def fetch(source):
    out = ROOT / 'originals' / (source['id'] + '.pdf')
    out.parent.mkdir(exist_ok=True)
    result = dict(source)
    try:
        if not out.exists():
            r = requests.get(source['url'], timeout=(15, 60), headers={'User-Agent': 'Mozilla/5.0 (public document validation research)'})
            r.raise_for_status()
            if not r.content.startswith(b'%PDF'):
                raise ValueError('Response is not a PDF')
            out.write_bytes(r.content)
            result['final_url'] = r.url
        result.update(file=str(out.relative_to(ROOT)).replace('\\','/'), bytes=out.stat().st_size,
                      sha256=hashlib.sha256(out.read_bytes()).hexdigest(), downloaded_at=datetime.now(timezone.utc).isoformat())
        result['status'] = 'downloaded'
    except Exception as exc:
        result.update(status='failed', error=str(exc))
    return result

def inspect(sources):
    pages=[]
    for source in sources:
        if source.get('status') != 'downloaded': continue
        try:
            doc=pdfium.PdfDocument(ROOT/source['file'])
            source['pages']=len(doc)
            for i in range(len(doc)):
                page=doc[i]
                tp=page.get_textpage()
                text=tp.get_text_range()
                pages.append(dict(source_id=source['id'], page=i+1, text=text,
                                  width=page.get_width(),height=page.get_height()))
                tp.close();page.close()
            doc.close()
        except Exception as exc: source['inspect_error']=str(exc)
    (ROOT/'pages.json').write_text(json.dumps(pages,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'source_manifest.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([dict(id=s['id'],status=s['status'],pages=s.get('pages'),error=s.get('error')) for s in sources],ensure_ascii=False),flush=True)

if __name__=='__main__':
    sources=json.loads((ROOT/'sources.json').read_text(encoding='utf-8'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(fetch,sources))
    inspect(results)
