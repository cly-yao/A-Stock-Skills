#!/usr/bin/env python3
"""Small read-only market sample.

Exit 0 when the screener returns live rows that satisfy the predicate.
Exit 2 when the network sample cannot be completed. This script never writes
synthetic prices into the live result.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astock_runtime
from skills_import import load

screener = load("skills/05-quant/screener/main.py", "live_screener")
report = load("skills/05-reports/report/main.py", "live_report")
data_source = load("skills/01-infra/astock-data-source/main.py", "live_data_source")

CODES = ["600519", "000001", "300750"]
WHERE = "pe>10 and pe<40"
OUT = Path("/opt/cursor/artifacts/live_readonly_sample.json")


def main() -> int:
    started = time.time()
    document = {
        "contains_synthetic": False,
        "codes": CODES,
        "where": WHERE,
        "screener": None,
        "independent_quotes": None,
        "daily": None,
        "stock": None,
        "errors": [],
        "wall_sec": None,
    }
    payload = None
    attempts = []
    for attempt in (1, 2):
        try:
            screened = screener.screen(scope=CODES, where=WHERE, timeout=12, top_n=10)
            payload = screener.result_payload(screened)
            attempts.append({
                "attempt": attempt,
                "row_count": payload["row_count"],
                "source": payload.get("source"),
                "fallback": payload.get("fallback"),
                "errors": payload.get("errors"),
            })
            if payload["row_count"] > 0 and not payload.get("fallback"):
                break
        except Exception as exc:
            attempts.append({"attempt": attempt, "exception": f"{type(exc).__name__}: {exc}"})
            payload = None
    document["screener_attempts"] = attempts
    try:
        if payload is None:
            raise RuntimeError("screener did not return a payload")
        document["screener"] = payload
        if payload["provenance"] != "live":
            document["errors"].append("screener result was not marked live")
        if payload["banner"].startswith("SYNTHETIC"):
            document["errors"].append("live screener returned a synthetic banner")
        for row in payload["rows"]:
            if row["code"] not in CODES:
                document["errors"].append(f"unexpected code {row['code']}")
            pe = row.get("pe")
            if pe is None or not (10 < float(pe) < 40):
                document["errors"].append(f"{row['code']} pe {pe} failed {WHERE}")
            if row.get("price") is None:
                document["errors"].append(f"{row['code']} has no price")
        if payload["row_count"] == 0:
            document["errors"].append(
                "screener returned no rows: " + " | ".join(str(item) for item in payload["errors"])
            )
    except Exception as exc:
        document["errors"].append(f"screener exception: {type(exc).__name__}: {exc}")

    screened_codes = {row["code"] for row in (document["screener"] or {}).get("rows") or []}
    try:
        quotes = data_source.get_quotes_for_codes(CODES, timeout=12)
        rows = quotes.to_dict(orient="records") if quotes is not None else []
        document["independent_quotes"] = {
            "provenance": "live",
            "source": quotes.attrs.get("source") if quotes is not None else None,
            "missing": list(quotes.attrs.get("missing") or []) if quotes is not None else CODES,
            "rows": rows,
        }
        for row in rows:
            pe = row.get("pe")
            if pe is None:
                continue
            inside = 11 < float(pe) < 39
            outside = float(pe) <= 9 or float(pe) >= 41
            if inside and row["code"] not in screened_codes:
                document["errors"].append(
                    f"independent pe {pe} for {row['code']} is inside the predicate but screener omitted it"
                )
            if outside and row["code"] in screened_codes:
                document["errors"].append(
                    f"independent pe {pe} for {row['code']} is outside the predicate but screener kept it"
                )
    except Exception as exc:
        document["independent_quotes"] = {
            "provenance": "live",
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        daily = report.build_daily_report(max_calls=6, timeout=8)
        document["daily"] = {
            "provenance": daily["provenance"],
            "banner": daily["banner"],
            "http_calls": len(daily["http_calls"]),
            "sections": [
                {"id": section["id"], "status": section["status"], "source": section.get("source"),
                 "error": section.get("error")}
                for section in daily["sections"]
            ],
        }
        if daily["provenance"] != "live":
            document["errors"].append("daily report provenance is not live")
        if len(daily["http_calls"]) > 6:
            document["errors"].append("daily report exceeded 6 HTTP calls")
        if not any(section["status"] in {"ok", "partial"} for section in daily["sections"]):
            document["errors"].append("daily report had no successful section")
    except Exception as exc:
        document["errors"].append(f"daily exception: {type(exc).__name__}: {exc}")

    try:
        stock = report.build_stock_report("600519", max_calls=5, timeout=8)
        document["stock"] = {
            "provenance": stock["provenance"],
            "banner": stock["banner"],
            "http_calls": len(stock["http_calls"]),
            "sections": [
                {"id": section["id"], "status": section["status"], "source": section.get("source"),
                 "error": section.get("error"),
                 "price": (section.get("rows") or [{}])[0].get("price") if section["id"] == "quote" else None}
                for section in stock["sections"]
            ],
        }
        if stock["provenance"] != "live":
            document["errors"].append("stock report provenance is not live")
        if len(stock["http_calls"]) > 5:
            document["errors"].append("stock report exceeded 5 HTTP calls")
        quote = next(section for section in stock["sections"] if section["id"] == "quote")
        if quote["status"] == "ok":
            price = quote["rows"][0].get("price")
            if price is None or float(price) <= 0:
                document["errors"].append("stock quote price missing")
        elif quote["status"] not in {"failed", "partial", "skipped"}:
            document["errors"].append("stock quote has no status")
    except Exception as exc:
        document["errors"].append(f"stock exception: {type(exc).__name__}: {exc}")

    document["wall_sec"] = round(time.time() - started, 3)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "wall_sec": document["wall_sec"],
        "errors": document["errors"],
        "screener_rows": None if not document["screener"] else document["screener"]["row_count"],
        "screener_provenance": None if not document["screener"] else document["screener"]["provenance"],
        "daily": document["daily"],
        "stock": document["stock"],
        "output": str(OUT),
    }, ensure_ascii=False, indent=2))
    return 0 if not document["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())
