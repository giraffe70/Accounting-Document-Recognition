"""Inspect or apply the dedicated source-page retirement while the service is stopped."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, event
from app.db.source_page_retirement import retire_source_pages

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parents[1] / "app/data/app.db")
parser.add_argument("--uploads", type=Path, default=Path(__file__).resolve().parents[1] / "app/data/uploads")
parser.add_argument("--apply", action="store_true", help="Back up and apply; default is a read-only inventory")
args = parser.parse_args()
if not args.database.is_file():
    parser.error("Database does not exist")
if args.apply:
    engine = create_engine(f"sqlite:///{args.database.resolve()}")
else:
    engine = create_engine(f"sqlite:///file:{args.database.resolve().as_posix()}?mode=ro&uri=true")
@event.listens_for(engine, "connect")
def foreign_keys(connection, _record):
    connection.execute("PRAGMA foreign_keys=ON")

try:
    print(json.dumps(retire_source_pages(engine, args.uploads, apply=args.apply), ensure_ascii=False, indent=2))
finally:
    engine.dispose()
