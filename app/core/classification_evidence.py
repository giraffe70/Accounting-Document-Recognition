"""Conservative document semantics, independent of a model's chosen contract."""
from __future__ import annotations
import re


def supported_type(text):
    compact = re.sub(r"\s+", "", text or "")
    if re.search(r"(?:轉帳|現金|收入|支出|會計)傳票", compact) and "借方" in compact and "貸方" in compact:
        return "journal_voucher", "傳票標題及借方／貸方欄"
    # References in applications/instructions do not establish an invoice title.
    lines = [re.sub(r"\s+", "", line) for line in (text or "").splitlines()]
    invoice_title = any(re.fullmatch(r"(?:[\w\u4e00-\u9fff]{0,12})?(?:電子發票證明聯|統一發票|電子發票)(?:證明聯|收執聯|存根聯)?", line)
                        and not re.search(r"要保|說明|申請|附件|提供|檢附", line) for line in lines)
    if invoice_title:
        return "invoice", "文件本身發票標題"
    if re.search(r"(?:勞工保險|全民健康保險|勞保|健保).*(?:繳費|繳款|保險費)", compact):
        return "social_insurance", "勞健保繳款用途與標題"
    if (re.search(r"郵資|郵政.*收據|郵局.*收據", compact) or
            re.search(r"(?:高鐵|臺鐵|台鐵|鐵路).*(?:車票|票價|乘車|出發|到達)", compact)):
        return "receipt", "郵資收據或交通票券用途"
    return None, None


def resolve(type_key, text, *, protected=False):
    candidate, reason = supported_type(text)
    evidence = {"model_type": type_key, "supported_type": candidate, "reason": reason,
                "conflict": bool(candidate and candidate != type_key), "adopted": bool(candidate and candidate != type_key and not protected),
                "protected": protected}
    return (candidate if evidence["adopted"] else type_key), evidence
