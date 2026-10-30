"""Offline interaction checks for the read-only confirmed voucher page."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "static" / "js" / "confirmed.js"
HTML = ROOT / "app" / "static" / "confirmed.html"
NODE = Path(r"C:\Program Files\nodejs\node.exe")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const requests = [], alerts = [], all = {};
class El {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.handlers={}; this.attrs={}; this.className=''; this.textContent=''; this.value=''; this.disabled=false; this.open=false;
    this.classList={toggle:(c,on)=>{this.attrs['class:'+c]=on}, add:(c)=>{this.attrs['class:'+c]=true}, remove:(c)=>{this.attrs['class:'+c]=false}}; }
  addEventListener(k,fn){this.handlers[k]=fn} append(...xs){this.children.push(...xs)} replaceChildren(...xs){this.children=[...xs]}
  setAttribute(k,v){this.attrs[k]=v} showModal(){this.open=true} close(){this.open=false;this.handlers.close?.()}
  click(){return this.handlers.click?.({target:this})}
}
const document={createElement:(tag)=>new El(tag), getElementById:(id)=>all[id]||(all[id]=new El()),
  open(){}, write(html){this.written=html}, close(){}};
const window={open(){ if(input.popupBlocked)return null; return {document:{open(){},write(html){window.report=html},close(){}},close(){window.popupClosed=true}}},addEventListener(k,fn){this.handlers=this.handlers||{};this.handlers[k]=fn}};
const location={hash:'',href:''};
function resp(body,status=200){return {ok:status>=200&&status<300,status,json:async()=>body,text:async()=>body.html||''}}
let listResolve, reportResolve;
async function fetch(url,opts={}) {
  requests.push({url,method:opts.method||'GET'});
  if(url==='/api/me')return input.meStatus?resp({detail:input.meDetail||'unauthorized'},input.meStatus):resp({name:'測試者'});
  if(url==='/api/companies')return resp({companies:[{id:7,name:'甲公司'}]});
  if(url==='/api/doc-types')return input.failTypes?resp({detail:[{loc:['query','company_id'],msg:'Input should be a valid integer'}]},422):resp({types:{invoice:{label:'類型標籤',fields:[{key:'vendor',label:'供應商'}],item_fields:[{key:'name',label:'品名'},{key:'account_code',label:'舊科目代號'}],entry_fields:[]}}});
  if(url.startsWith('/api/confirmed-vouchers')) {
    if(input.deferFirstList && requests.filter(x=>x.url.startsWith('/api/confirmed-vouchers')).length===1) return await new Promise(resolve=>{listResolve=resolve});
    if(input.raceLists) { const n=requests.filter(x=>x.url.startsWith('/api/confirmed-vouchers')).length; return resp({items:[{id:n,voucher_id:`row-${n}`}],total:1,page:1,page_size:50,pages:1}); }
    return resp(input.list || {items:[],total:0,page:1,page_size:50,pages:0});
  }
  if(url==='/api/vouchers/9')return input.detailStatus===404?resp({detail:'gone'},404):resp(input.detail||{id:9,status:input.detailStatus||'confirmed',record_role:'voucher',company:'甲公司',company_id:7,confirmed_at:'2026-01-02T03:04:05Z',confirmed_by_name:'使用者',updated_at:'version / 1',recognition_report_ready:input.ready??true,has_image:!input.noImage,attachments:1,current:{doc_type_key:'invoice',doc_type:'發票',voucher_id:'A-1',date:'2026-01-01',currency:'TWD',fields:{vendor:'<img src=x onerror=alert(1)>'},items:[{name:'紙',account_code:'5100',account_name:'紙品'}],entries:[{description:'租金',amount:0,debit_account:'5100',debit_account_name:'租金費用',credit_account:'2100',credit_account_name:'應付'}],subtotal:0,tax_amount:0,total_amount:0,note:'摘要'}});
  if(url.includes('/recognition-report')) {
    if(input.deferReport)return await new Promise(resolve=>{reportResolve=resolve});
    return resp(input.report409?{detail:'stale'}:{html:'<html>報告</html>'},input.report409?409:200);
  }
  if(url.includes('/image'))return resp({});
  throw new Error('unexpected '+url);
}
const context={console,document,window,location,fetch,URLSearchParams,setTimeout,clearTimeout};
vm.createContext(context); vm.runInContext(fs.readFileSync(input.js,'utf8'),context);
const tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
  await tick(); await tick();
  const api=window.ConfirmedPage;
  if(input.action==='filter'){
    all.companyFilter.value='7';all.dateFrom.value='2026-01-01';all.dateTo.value='2026-01-31';all.filters.handlers.submit({preventDefault(){}});await tick();
    const filtered=requests.filter(x=>x.url.startsWith('/api/confirmed-vouchers')).at(-1).url;
    all.clearFilters.click();await tick();
    all.nextPage.click(); await tick();
    process.stdout.write(JSON.stringify({filtered,cleared:requests.filter(x=>x.url.startsWith('/api/confirmed-vouchers')).at(-1).url,methods:requests.map(x=>x.method)}));
  } else if(input.action==='detail'){
    await api.openDetail(9);
    if(input.triggerImageError) { const img=all.detailBody.children[0].children.find(x=>x.tagName==='img'); img.handlers.error(); }
    process.stdout.write(JSON.stringify({requests,dialogOpen:all.detailDialog.open,listMessage:all.listMessage.textContent,htmlInjected:all.detailBody.children.some(x=>String(x.textContent).includes('<img')),
      texts:JSON.stringify(all.detailBody.children),image:all.detailBody.children[0]?.children.find(x=>x.tagName==='img')?.src,printDisabled:JSON.stringify(all.detailBody.children).includes('disabled')}));
  } else if(input.action==='print'){
    await api.openDetail(9); await api.printReport(api.state.current);
    const findPrint=(el)=>el.tagName==='button'&&el.textContent==='列印辨識報告'?el:el.children.map(findPrint).find(Boolean);
    process.stdout.write(JSON.stringify({requests,popupClosed:window.popupClosed,report:window.report,detailMessage:all.detailMessage.textContent,printDisabled:findPrint(all.detailBody).disabled,dialogOpen:all.detailDialog.open}));
  } else if(input.action==='start'){
    await tick(); await tick();
    process.stdout.write(JSON.stringify({href:location.href,listMessage:all.listMessage.textContent,listChildren:all.listMessage.children.map(x=>x.textContent),requests}));
  } else if(input.action==='stalePrint'){
    await api.openDetail(9); const pending=api.printReport(api.state.current); await tick();
    all.detailDialog.close(); all.detailMessage.textContent='other record';
    reportResolve(resp({html:'<html>late</html>'})); await pending;
    process.stdout.write(JSON.stringify({popupClosed:window.popupClosed,report:window.report||null,detailMessage:all.detailMessage.textContent}));
  } else if(input.action==='race'){
    const first=api.loadList(); await tick(); const second=api.loadList(); await tick();
    if(listResolve)listResolve(resp({items:[{id:1,voucher_id:'old'}],total:1,pages:1})); await Promise.all([first,second]);
    process.stdout.write(JSON.stringify({body:all.confirmedRows.children.map(x=>x.children[0].children[0].textContent),requests}));
  }
})().catch(e=>{console.error(e.stack||e);process.exit(1)});
"""


class ConfirmedFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = JS.read_text(encoding="utf-8")

    def run_node(self, **data):
        payload = {"js": str(JS), **data}
        result = subprocess.run([str(NODE), "-e", HARNESS], input=json.dumps(payload), text=True,
                                capture_output=True, check=True, encoding="utf-8")
        return json.loads(result.stdout)

    def test_filter_clear_and_pagination_use_get_only(self):
        out = self.run_node(action="filter", list={"items": [], "total": 0, "page": 1, "page_size": 50, "pages": 2})
        self.assertIn("company_id=7", out["filtered"])
        self.assertIn("date_from=2026-01-01", out["filtered"])
        self.assertIn("date_to=2026-01-31", out["filtered"])
        self.assertIn("page_size=50", out["filtered"])
        self.assertNotIn("company_id", out["cleared"])
        self.assertTrue(all(method == "GET" for method in out["methods"]))
        self.assertNotRegex(self.source, r"fetch\([^\n]*method\s*:\s*['\"](?:POST|PUT|DELETE)")

    def test_detail_encodes_version_and_uses_text_nodes(self):
        out = self.run_node(action="detail")
        self.assertIn("expected_updated_at=version%20%2F%201", out["image"])
        self.assertFalse(out["htmlInjected"])
        self.assertIn("<img src=x onerror=alert(1)>", out["texts"])
        self.assertIn("類型標籤", out["texts"])
        self.assertIn("科目名稱", out["texts"])
        self.assertIn("原稿傳票明細", out["texts"])
        self.assertIn("借方科目名稱", out["texts"])
        self.assertEqual(out["texts"].count("<img src=x onerror=alert(1)>"), 1)

    def test_attachments_work_without_main_image_and_image_errors_can_retry(self):
        out = self.run_node(action="detail", noImage=True)
        self.assertIn("&att=0", out["image"])
        errored = self.run_node(action="detail", triggerImageError=True)
        self.assertIn('"class:hidden":false', errored["texts"])

    def test_expired_status_closes_and_refreshes(self):
        out = self.run_node(action="detail", detailStatus="pending")
        self.assertFalse(out["dialogOpen"])
        self.assertIn("回到待確認", out["listMessage"])
        self.assertGreaterEqual(sum("confirmed-vouchers" in r["url"] for r in out["requests"]), 2)
        missing = self.run_node(action="detail", detailStatus=404)
        self.assertFalse(missing["dialogOpen"])
        self.assertIn("已不存在", missing["listMessage"])

    def test_report_readiness_and_conflict(self):
        not_ready = self.run_node(action="print", ready=False)
        self.assertFalse(any("recognition-report" in r["url"] for r in not_ready["requests"]))
        conflict = self.run_node(action="print", report409=True)
        self.assertTrue(any("recognition-report" in r["url"] and "version%20%2F%201" in r["url"] for r in conflict["requests"]))
        self.assertTrue(conflict["popupClosed"])
        self.assertIn("版本已變更", conflict["detailMessage"])
        self.assertTrue(conflict["printDisabled"])
        stale = self.run_node(action="stalePrint", deferReport=True)
        self.assertTrue(stale["popupClosed"])
        self.assertIsNone(stale["report"])
        self.assertEqual(stale["detailMessage"], "other record")

    def test_non_401_startup_error_shows_retry_and_422_text(self):
        out = self.run_node(action="start", failTypes=True)
        self.assertEqual(out["href"], "")
        self.assertIn("Input should be a valid integer", out["listMessage"])
        self.assertIn("重試", out["listChildren"])
        auth = self.run_node(action="start", meStatus=401)
        self.assertEqual(auth["href"], "/")

    def test_late_list_response_does_not_replace_newer_results(self):
        out = self.run_node(action="race", deferFirstList=True, raceLists=True)
        self.assertEqual(out["body"], ["row-3"])

    def test_page_is_read_only_and_uses_safe_rendering(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('<dialog id="detailDialog"', html)
        self.assertNotIn("innerHTML", self.source)
        self.assertNotIn("window.print", self.source)


if __name__ == "__main__":
    unittest.main()
