"""Stable company identities; JSON is the initial registration source."""
import json
from app import config
from sqlmodel import select
from app.db.models import Company


def resolve(session, value=None, *, company_id=None):
    if company_id is not None:
        return session.get(Company, company_id)
    if not value:
        return None
    matches = session.exec(select(Company).where(
        (Company.short == value) | (Company.name == value) | (Company.account_chart_key == value))).all()
    return matches[0] if len(matches) == 1 else None


def registrations():
    path = config.DATA_DIR / "companies.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def public(company, registration_rows=None):
    result = {"id": company.id, "name": company.name, "short": company.short,
              "tax_id": company.tax_id, "account_chart_key": company.account_chart_key}
    if not result["tax_id"]:
        # Only fill a missing value for the stable chart key. Never rename a
        # registered company or overwrite a tax ID maintained in the database.
        rows = registrations() if registration_rows is None else registration_rows
        matches = [row for row in rows if (row.get("short") or row.get("name")) == company.account_chart_key]
        if len(matches) == 1:
            result["tax_id"] = matches[0].get("tax_id") or ""
    return result


def seed(session, registrations):
    # Seed once: a later name edit must never recreate the company's identity.
    existing = session.exec(select(Company)).all()
    if existing:
        for company in existing:
            tax_id = public(company, registrations)["tax_id"]
            if not company.tax_id and tax_id:
                company.tax_id = tax_id
                session.add(company)
        return
    for registration in registrations:
        key = str(registration.get("short") or registration.get("name") or "").strip()
        if key and resolve(session, key) is None:
            session.add(Company(name=registration.get("name", ""), short=registration.get("short", ""),
                                tax_id=registration.get("tax_id", ""), account_chart_key=key))
            session.flush()
