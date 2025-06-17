#!/usr/bin/env python3
"""
reverseComposition.py – rebuild openEHR compositions from the flattened
representation and (optionally) verify them bit-for-bit against the source.

Examples
--------
# rebuild everything (no verification)
python reverseComposition.py

# rebuild first 500 docs and verify each one
python reverseComposition.py --limit 500 --verify

# rebuild a single flattened ObjectId and verify
python reverseComposition.py 684b480db3b45ec84f4d04ff --verify
"""
from __future__ import annotations
import argparse, json, logging, sys, time, hashlib
from typing import Any, Dict
from datetime import datetime

import certifi
from bson import ObjectId, json_util
from pymongo import MongoClient

# ── local helpers -------------------------------------------------------
from reverse_unflatten import rebuild_composition, load_codebook, load_shortcuts

# ── canonical JSON dump (stable for hashing) ----------------------------
try:                                        # PyMongo ≥4.4
    CANONICAL = json_util.CANONICAL_JSON_OPTIONS          # type: ignore
except AttributeError:                      # older PyMongo
    from bson.json_util import JsonOptions, DatetimeRepresentation
    CANONICAL = JsonOptions(
        datetime_representation=DatetimeRepresentation.ISO8601,
        strict_number_long=True, strict_uuid=False, tz_aware=False
    )

def _dump(doc: dict) -> bytes:
    """Stable bytes for hashing, preserves ISO datetimes."""
    try:
        txt = json_util.dumps(
            doc, json_options=CANONICAL,
            sort_keys=True, separators=(",", ":")
        )
    except Exception:
        def norm(x):
            if isinstance(x, datetime):
                return x.isoformat(timespec="milliseconds") + "Z" \
                       if x.tzinfo else x.isoformat(timespec="milliseconds")
            if isinstance(x, list):
                return [norm(i) for i in x]
            if isinstance(x, dict):
                return {k: norm(v) for k, v in x.items()}
            return x
        txt = json.dumps(norm(doc), sort_keys=True, separators=(",", ":"))
    return txt.encode()

def _identical(a: dict, b: dict) -> bool:
    return hashlib.sha256(_dump(a)).digest() == hashlib.sha256(_dump(b)).digest()

# ── config loader -------------------------------------------------------
def cfg(path="config.json") -> Dict[str, Any]:
    return json.load(open(path, encoding="utf-8"))

# ── main ----------------------------------------------------------------
def main() -> None:
    # CLI args -----------------------------------------------------------
    ap = argparse.ArgumentParser()
    ap.add_argument("oid",   nargs="?", help="single flattened _id")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--query", type=str, default=None)
    ap.add_argument("--verify", action="store_true",
                    help="compare rebuilt doc byte-for-byte with original")
    args = ap.parse_args()

    # logging ------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Mongo connections --------------------------------------------------
    C   = cfg()
    cli = MongoClient(C["target"]["connection_string"], tlsCAFile=certifi.where())
    db  = cli[C["target"]["database_name"]]

    col_flat  = db[C["target"]["compositions_collection"]]
    col_codes = db[C["target"]["codes_collection"]]
    col_short = db[C["target"]["shortcuts_collection"]]
    col_out   = db[C["target"]["rebuilt_collection"]]

    codes  = load_codebook(col_codes.find_one({"_id": "ar_code"}) or {})
    shorts = load_shortcuts(col_short.find_one({"_id": "shortcuts"}) or {})

    # build query --------------------------------------------------------
    if args.oid:
        q = {"_id": ObjectId(args.oid)}
        args.limit = 1
    elif args.query:
        q = json.loads(args.query)
    else:
        q = {}

    cursor = col_flat.find(q, no_cursor_timeout=True)
    if args.limit:
        cursor = cursor.limit(args.limit)

    # counters & timer ---------------------------------------------------
    total = ok = fail = diff = 0
    t0 = time.time()

    # -------------------------------------------------------------------
    for flat_doc in cursor:
        total += 1
        try:
            rebuilt = rebuild_composition(flat_doc, codes, shorts)

            if args.verify:
                orig = db["samples"].find_one(
                    {"_id": flat_doc["comp_id"]}, {"canonicalJSON": 1}
                )
                if not orig:
                    logging.warning("⚠ original %s not found", flat_doc["comp_id"])
                    diff += 1
                elif not _identical(orig["canonicalJSON"], rebuilt):
                    logging.error("❌ mismatch for _id=%s", flat_doc["_id"])
                    diff += 1

            col_out.replace_one(
                {"_id": flat_doc["_id"]},
                {"_id": flat_doc["_id"],
                 "ehr_id": flat_doc["ehr_id"],
                 "composition": rebuilt},
                upsert=True
            )
            ok += 1
        except Exception:
            logging.exception("failed at _id=%s", flat_doc["_id"])
            fail += 1

        # ▶▶ progress ticker every 100 documents
        if total % 100 == 0:
            logging.info("… processed %d docs (ok=%d  fail=%d  Δ=%d)",
                         total, ok, fail, diff)

    # -------------------------------------------------------------------
    dt = time.time() - t0 or 0.0001
    logging.info("✔ rebuilt=%d  ✖ errors=%d  Δ diff=%d  (%.1f docs/s)",
                 ok, fail, diff, total / dt)

# -----------------------------------------------------------------------
if __name__ == "__main__":
    main()