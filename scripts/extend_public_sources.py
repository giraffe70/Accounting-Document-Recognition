import json
from pathlib import Path
ROOT=Path('ref/public_zh_tw_validation_100')
sources=json.loads((ROOT/'sources.json').read_text(encoding='utf8'))
def add(title,publisher,url,page_url=None):
    if url in {s['url'] for s in sources}:return
    sources.append(dict(id=f'S{len(sources)+1:02}',title=title,publisher=publisher,url=url,page_url=page_url or url))
for name,num in zip(['免用統一發票收據','二聯式發票','三聯式發票','電子發票感熱紙','電子發票網路列印','電子發票信用卡載具','收銀機三聯式','收銀機單聯式'],['210181052','901295671','497911290','640381748','245181823','119433684','611436927','693872474']):
    add(name,'國立成功大學',f'https://acco.ncku.edu.tw/var/file/30/1030/img/4225/{num}.pdf','https://acco.ncku.edu.tw/p/403-1030-3097.php?Lang=zh-tw')
for link in json.loads((ROOT/'discovered_links.json').read_text(encoding='utf8')):
    if ('核銷專區' in link['url'] and any(x in link['label'] for x in ['02.','03-','04-'])) or link['label'] in ['核銷單據範例1110927.pdf','預算及核銷.pdf']:
        add(link['label'],'南臺科技大學' if 'stust' in link['url'] else ('國立聯合大學' if 'nuu.' in link['url'] else '靜宜大學'),link['url'],link['page_url'])
add('中臺科技大學經費核銷說明會1130221','中臺科技大學','https://account.ctust.edu.tw/var/file/27/1027/img/781891476.pdf')
(ROOT/'sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding='utf8')
