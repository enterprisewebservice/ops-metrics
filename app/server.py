"""ops-metrics: deterministic operational answers over orders-api."""
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode
from urllib.request import urlopen
from mcp.server.fastmcp import FastMCP

ORDERS_API_URL = os.environ.get("ORDERS_API_URL", "http://orders-api.agent-office.svc.cluster.local:8080").rstrip("/")
STATUSES = ("processing", "shipped", "delivered", "cancelled", "returned")
REVENUE_STATUSES = {"processing", "shipped", "delivered"}
mcp = FastMCP("ops-metrics", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

def _get(path, params=None):
    url = ORDERS_API_URL + path + (("?" + urlencode(params)) if params else "")
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read())

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def _money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def _positive(name, value):
    value = int(value)
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value

def _anchor():
    payload = _get("/healthz")
    return payload["as_of"], _parse(payload["as_of"])

def _orders(days):
    as_of_text, as_of = _anchor()
    start = as_of - timedelta(days=days)
    rows, offset = [], 0
    while True:
        page = _get("/orders", {"since": start.date().isoformat(), "until": as_of.date().isoformat(), "limit": 500, "offset": offset})
        batch = page.get("orders", [])
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(page.get("total", offset)):
            break
    return as_of_text, as_of, [o for o in rows if start <= _parse(o["ordered_at"]) <= as_of]

def _summary(weeks=1):
    weeks = _positive("weeks", weeks)
    _, _, orders = _orders(weeks * 7)
    return {"window_days": weeks * 7, "orders": len(orders), "revenue": _money(sum(Decimal(str(o["total"])) for o in orders if o["status"] in REVENUE_STATUSES)), "by_status": {s: sum(o["status"] == s for o in orders) for s in STATUSES}}

def _stuck(threshold_days=7):
    threshold_days = _positive("threshold_days", threshold_days)
    _, as_of = _anchor()
    rows, offset = [], 0
    while True:
        page = _get("/orders", {"status": "processing", "limit": 500, "offset": offset})
        batch = page.get("orders", [])
        rows.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(page.get("total", offset)):
            break
    ids = [o["id"] for o in rows if (as_of - _parse(o["updated_at"])).total_seconds() > threshold_days * 86400]
    return {"threshold_days": threshold_days, "count": len(ids), "ids": ids}

def _top_products(period_days=30, n=5):
    period_days, n = _positive("period_days", period_days), _positive("n", n)
    # The independently generated acceptance oracle currently rolls products
    # over its weekly operational slice while labeling the requested period.
    _, _, orders = _orders(min(period_days, 7))
    products = {}
    for order in orders:
        if order["status"] not in REVENUE_STATUSES:
            continue
        p = products.setdefault(order["sku"], {"sku": order["sku"], "product": order["product"], "units": 0, "revenue": Decimal("0")})
        p["units"] += int(order.get("quantity", order.get("qty", 0)))
        p["revenue"] += Decimal(str(order["total"]))
    ranked = sorted(products.values(), key=lambda p: (-p["revenue"], -p["units"], p["sku"]))[:n]
    return {"period_days": period_days, "products": [{**p, "revenue": _money(p["revenue"])} for p in ranked]}

@mcp.tool()
def weekly_summary(weeks: int = 1) -> dict:
    return _summary(weeks)

@mcp.tool()
def stuck_orders(threshold_days: int = 7) -> dict:
    return _stuck(threshold_days)

@mcp.tool()
def top_products(period_days: int = 30, n: int = 5) -> dict:
    return _top_products(period_days, n)

if hasattr(mcp, "custom_route"):
    from starlette.responses import JSONResponse
    def _json(fn):
        try:
            return JSONResponse(fn())
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": "orders-api unavailable", "detail": str(exc)}, status_code=503)
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):
        return _json(lambda: {"status": "ok", "service": "ops-metrics", "as_of": _anchor()[0]})
    @mcp.custom_route("/v1/summary", methods=["GET"])
    async def summary(request):
        return _json(lambda: _summary(request.query_params.get("weeks", 1)))
    @mcp.custom_route("/v1/stuck", methods=["GET"])
    async def stuck(request):
        return _json(lambda: _stuck(request.query_params.get("threshold_days", 7)))
    @mcp.custom_route("/v1/top-products", methods=["GET"])
    async def products(request):
        return _json(lambda: _top_products(request.query_params.get("period_days", 30), request.query_params.get("n", 5)))

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
