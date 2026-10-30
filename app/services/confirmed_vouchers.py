"""Read-only, paginated access to confirmed vouchers for all signed-in users."""
import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlmodel import select

from app.core.normalization import norm_date
from app.db.models import ReviewStatus, VoucherRecord
from app.db.session import get_session
from app.services import auth, recognition_details


def require_user(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    if not auth.get_user_by_token(request.cookies.get("session")):
        raise HTTPException(401, "未登入", headers={"Cache-Control": "no-store"})


router = APIRouter(prefix="/api/confirmed-vouchers", tags=["confirmed-vouchers"],
                   dependencies=[Depends(require_user)])


def _data(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _document_date(raw):
    """Use the existing date parser, without repairing or saving legacy records."""
    value = _data(raw).get("date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(norm_date(value)).isoformat()
    except ValueError:
        return None


def _summary(record):
    current = _data(record.current_json)
    try:
        attachments = json.loads(record.attachments or "[]")
    except (TypeError, ValueError):
        attachments = []
    return {
        "id": record.id, "company": record.company, "company_id": record.company_id,
        "voucher_id": current.get("voucher_id", ""),
        "doc_type": current.get("doc_type", ""), "doc_type_key": current.get("doc_type_key", ""),
        "date": current.get("date", ""), "total_amount": current.get("total_amount"),
        "currency": current.get("currency", ""),
        "confirmed_at": record.confirmed_at.isoformat() if record.confirmed_at else "",
        "confirmed_by_name": record.confirmed_by_name,
        "updated_at": record.updated_at.isoformat(),
        "recognition_report_ready": (record.recognition_confirmed_version == recognition_details.VERSION
                                     and current.get("details_version") == recognition_details.VERSION),
        "has_image": bool(record.image_path),
        "attachments": len(attachments) if isinstance(attachments, list) else 0,
    }


@router.get("")
def list_confirmed(company_id: int | None = Query(None, ge=1),
                   date_from: date | None = None, date_to: date | None = None,
                   page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "起日不能晚於迄日")
    with get_session() as session:
        # A connection-local read function lets SQLite filter/order/page legacy
        # dates too. Only the requested page is materialized as voucher records.
        connection = session.connection().connection.driver_connection
        connection.create_function("confirmed_voucher_date", 1, _document_date, deterministic=True)
        document_date = func.confirmed_voucher_date(VoucherRecord.current_json)
        conditions = [VoucherRecord.status == ReviewStatus.confirmed,
                      VoucherRecord.record_role == "voucher"]
        if company_id is not None:
            conditions.append(VoucherRecord.company_id == company_id)
        if date_from:
            conditions.append(document_date >= date_from.isoformat())
        if date_to:
            conditions.append(document_date <= date_to.isoformat())
        total = session.exec(select(func.count()).select_from(VoucherRecord).where(*conditions)).one()
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        records = session.exec(select(VoucherRecord).where(*conditions).order_by(
            document_date.desc(), VoucherRecord.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
        return {"items": [_summary(record) for record in records], "total": total,
                "page": page, "page_size": page_size, "pages": pages}
