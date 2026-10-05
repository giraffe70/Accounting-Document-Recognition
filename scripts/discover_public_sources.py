import json
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin
from pip._vendor import truststore
truststore.inject_into_ssl()
import requests

class Links(HTMLParser):
    def __init__(self): super().__init__(); self.rows=[]; self.href=None; self.label=''
    def handle_starttag(self,tag,attrs):
        if tag=='a': self.href=dict(attrs).get('href'); self.label=''
    def handle_data(self,data):
        if self.href: self.label+=data
    def handle_endtag(self,tag):
        if tag=='a' and self.href:
            self.rows.append((self.label.strip(),self.href)); self.href=None

urls=[
 'https://account.stust.edu.tw/tc/node/download',
 'https://stua04.nuu.edu.tw/p/405-1036-46338,c5808.php',
 'https://osaea.pu.edu.tw/p/16-1064-10771.php?Lang=zh-tw',
 'https://www.laps.tyc.edu.tw/modules/tadnews/index.php?nsn=7263',
]
out=[]
for url in urls:
    try:
        r=requests.get(url,timeout=25); r.raise_for_status(); r.encoding='utf-8'
        p=Links(); p.feed(r.text)
        for label,link in p.rows:
            if '.pdf' in link.lower() or any(k in label for k in ['發票','收據','錯誤態樣','核銷單據','預算及核銷']):
                out.append(dict(page_url=url,label=label,url=urljoin(url,link)))
    except Exception as exc: print(str(exc))
path=Path('ref/public_zh_tw_validation_100/discovered_links.json')
path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(out,ensure_ascii=False,indent=2))
