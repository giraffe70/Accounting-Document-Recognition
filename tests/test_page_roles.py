import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image, ImageDraw
from app import validation, config
from app.core import page_layout, pipeline, barcode, supplement


class PageRoleTests(unittest.TestCase):
    def test_confirmed_nonfinancial_roles_skip_extraction_unknown_and_conflicts_do_not(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"app/data/test-tmp-plan") as temp:
            image = Path(temp)/"page.png"
            Image.new("RGB",(600,800),"white").save(image)
            cases = [("blank","other","",True), ("supporting","bill_detail","附頁",True),
                     ("non_voucher","other","要保書 保障額度 1,000,000",True),
                     ("unknown","other","文字",False),
                     ("non_voucher","other","要保書 統一發票 實收金額500",False)]
            for kind,type_key,text,skip in cases:
                raw = {"type":type_key, "rotate":0,"reason":"synthetic source role", "content_kind":kind,
                       "layout_kind":"single","regions":[]}
                requests = []
                def reply(_chat, request):
                    requests.append(request)
                    return json.dumps(raw)
                with patch("app.core.model_factory.get_chat_model",return_value=object()), \
                     patch("app.core.pipeline._invoke_text",side_effect=reply), \
                     patch("app.core.barcode.decode_diagnostics",return_value=barcode.summarize([],[],[600,800])):
                    layout = page_layout.detect(str(image),mode="ocr_text",ocr_text=text,model_setting="fake/model")
                self.assertFalse(any(part.get("type") == "image_url" for request in requests for message in request for part in (message.get("content") if isinstance(message.get("content"),list) else [])))
                def runner(payload,_timeout):
                    self.assertEqual(payload["phase"],"layout")
                    return {"ok":True,"data":{"layout":layout}}
                payload = {"run_id":"run","id":kind,"page_path":str(image),"page_no":1,"combo":{"mode":"ocr_llm"}}
                with patch.object(validation,"RUNS",Path(temp)/"runs"):
                    data = validation._run_page_child(payload,20,runner)["data"]
                self.assertEqual(data["page_status"],"skipped" if skip else "partial")
                self.assertEqual(data["documents"],[])
                self.assertEqual(data["voucher"],{})

    def test_formal_non_voucher_page_creates_only_immutable_source(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/"app/data/test-tmp-plan") as temp:
            image = Path(temp)/"application.png"
            Image.new("RGB",(600,800),"white").save(image)
            original = image.read_bytes()
            layout = {"type":"other", "content_kind":"non_voucher","auto_split":True,"regions":[],"issues":[]}
            with patch.object(config,"UPLOAD_DIR",Path(temp)/"uploads"), patch.object(page_layout,"detect",return_value=layout):
                vouchers = pipeline.prepare_pages(str(image))
            self.assertEqual(len(vouchers),1)
            self.assertEqual(vouchers[0].origin["record_role"],"source_page")
            self.assertIsNone(vouchers[0].total_amount)
            self.assertEqual(image.read_bytes(),original)

    def test_single_region_cannot_swallow_three_independent_frames(self):
        region = {"type":"cheque","rotate":0,"reason":"single","confidence":"high","box":[0,0,1000,1000]}
        raw = {"type":"cheque","rotate":0,"reason":"single","content_kind":"document","layout_kind":"single","regions":[region]}
        evidence = {"frames":[[0,0,300,900],[350,0,650,900],[700,0,1000,900]],"anchors":[]}
        layout = page_layout.validate_layout(raw,evidence=evidence)
        self.assertFalse(layout["auto_split"])
        self.assertIsNone(layout["fallback_reason"])

    def test_ocr_spatial_row_labels_and_multiple_amounts_do_not_cross_semantics(self):
        lines = [{"text":text,"box":[[x,y],[x+80,y],[x+80,y+20],[x,y+20]]} for x,y,text in
                 [(0,0,"總計（含稅）"),(100,0,"500元(現金)"),(0,60,"明細"),(100,60,"100")]]
        text = supplement.spatial_text(lines)
        self.assertTrue(supplement.independent_money("total_amount",500,text,{}))
        self.assertFalse(supplement.independent_money("total_amount",100,text,{}))
        totals = "未稅銷售額15,500 稅額775 總計16,275"
        for key,value in (("subtotal",15500),("tax_amount",775),("total_amount",16275)):
            self.assertTrue(supplement.independent_money(key,value,totals,{}))

    def test_text_groups_use_unique_complete_border_without_model_images(self):
        frames = [[0,0,300,900],[350,0,650,900],[700,0,1000,900]]
        lines = [{"text":"synthetic cheque", "box":[[x+30,30],[x+250,30],[x+250,800],[x+30,800]]} for x in (0,350,700)]
        regions = [{"type":"cheque", "rotate":0,"reason":"group","confidence":"high","box":None,"line_ids":[i]} for i in range(3)]
        raw = {"type":"cheque","rotate":0,"reason":"three cheques","content_kind":"document","layout_kind":"multiple","regions":regions}
        layout = page_layout.validate_layout(raw,"ocr_text",lines,(1000,1000),{"frames":frames,"anchors":[]})
        self.assertTrue(layout["auto_split"])
        self.assertEqual([r["box"] for r in layout["regions"]],frames)
        self.assertTrue(all(r["ocr_region_box"] != r["box"] for r in layout["regions"]))


if __name__ == "__main__":
    unittest.main()
