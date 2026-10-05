import json, math
from pathlib import Path
import pypdfium2 as pdfium
from PIL import Image, ImageDraw
ROOT=Path('ref/public_zh_tw_validation_100')
SELECT={
 'S01':[6,7,9,10,11,13,16,17,18,19], 'S02':[11,16,17],
 'S03':[88,89,90,91,92,93,94,97,98,99], 'S04':[44,45,46],
 'S06':list(range(10,16))+[18,20,25,26,27,28,29], 'S07':[17,19,20],
 'S08':[41,42,43], 'S09':[6,8], 'S11':[10], 'S12':[3,4,5,6,7],
 **{f'S{i}':[1] for i in range(13,21)},
 'S21':[2,3,4,5,6,8,9,10,11,12,13,14,15,16,17],
 'S22':[2,3,5,6,7], 'S23':list(range(3,13)),
 'S24':list(range(1,46)), 'S25':list(range(1,18)), 'S26':list(range(1,7)),
 'S27':list(range(10,25)), 'S28':list(range(25,33))+[51,53,65,66,67,68,69]
}
if __name__=='__main__':
    import sys
    if len(sys.argv)>1:
        requested=set(sys.argv[1:]);SELECT={k:v for k,v in SELECT.items() if k in requested}
    out=ROOT/'qa';out.mkdir(exist_ok=True)
    for sid,pages in SELECT.items():
        doc=pdfium.PdfDocument(ROOT/'originals'/f'{sid}.pdf')
        for batch in range(math.ceil(len(pages)/12)):
            group=pages[batch*12:batch*12+12]
            sheet=Image.new('RGB',(1600,math.ceil(len(group)/4)*340),'#dddddd')
            draw=ImageDraw.Draw(sheet)
            for j,n in enumerate(group):
                page=doc[n-1]; im=page.render(scale=1).to_pil().convert('RGB')
                im.thumbnail((388,310))
                x=(j%4)*400; y=(j//4)*340
                sheet.paste(im,(x+(400-im.width)//2,y+25))
                draw.text((x+8,y+5),f'{sid} p{n}',fill='black')
                page.close()
            sheet.save(out/f'{sid}_{batch+1}.jpg',quality=90)
        doc.close()
    print('Preview sheets ready')
