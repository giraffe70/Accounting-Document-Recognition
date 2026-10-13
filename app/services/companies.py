"""Stable company identities; JSON is the initial registration source."""
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


def public(company):
    return {"id": company.id, "name": company.name, "short": company.short,
            "tax_id": company.tax_id, "account_chart_key": company.account_chart_key}


def seed(session, registrations):
    # Seed once: a later name edit must never recreate the company's identity.
    if session.exec(select(Company)).first() is not None:
        return
    for registration in registrations:
        key = str(registration.get("short") or registration.get("name") or "").strip()
        if key and resolve(session, key) is None:
            session.add(Company(name=registration.get("name", ""), short=registration.get("short", ""),
                                tax_id=registration.get("tax_id", ""), account_chart_key=key))
            session.flush()
