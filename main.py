import hmac
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send
from mcp.server.transport_security import TransportSecuritySettings

BASE = "https://financialmodelingprep.com/stable"
PUBLIC_URL = "https://fmp-api-production.up.railway.app"
AUTH0_DOMAIN = "dev-8gz0ff2v0pjmf6tv.us.auth0.com"
ISSUER = f"https://{AUTH0_DOMAIN}/"
AUDIENCE = f"{PUBLIC_URL}/mcp"
JWKS = jwt.PyJWKClient(f"{ISSUER}.well-known/jwks.json", cache_keys=True)


def authorize_service(header: Optional[str]):
    token = os.getenv("SERVICE_ACCESS_TOKEN", "")
    if not token:
        raise HTTPException(503, "Service access token not configured")
    if not header or not header.startswith("Bearer "):
        raise HTTPException(401, "Bearer token required")
    if not hmac.compare_digest(header[7:], token):
        raise HTTPException(403, "Invalid token")


def validate_symbol(symbol: str) -> str:
    if len(symbol) > 20 or not symbol.replace("^", "").replace("-", "").replace(".", "").isalnum():
        raise ValueError("Invalid symbol")
    return symbol.upper()


async def request_fmp(endpoint: str, params: dict):
    key = os.getenv("FMP_API_KEY")
    if not key:
        raise RuntimeError("FMP key not configured")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{BASE}/{endpoint}", params={**params, "apikey": key})
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Upstream FMP request failed") from exc
    if isinstance(result, dict) and ("Error Message" in result or "error" in result):
        raise RuntimeError("FMP returned an error; check endpoint or plan access")
    return result


mcp = FastMCP(
    "FMP Stock Research",
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "fmp-api-production.up.railway.app",
            "fmp-api-production.up.railway.app:*",
        ],
    ),
)


@mcp.tool()
async def stock_quote(symbol: str) -> dict:
    """Get the latest available stock quote for a ticker such as MSFT or NVDA."""
    return {"symbol": validate_symbol(symbol), "data": await request_fmp("quote", {"symbol": validate_symbol(symbol)})}


@mcp.tool()
async def historical_prices(symbol: str, start: str, end: str) -> dict:
    """Get daily historical closing prices. Dates must be YYYY-MM-DD, maximum 3650 days."""
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if b < a or (b - a).days > 3650:
        raise ValueError("Invalid date range")
    ticker = validate_symbol(symbol)
    return {"symbol": ticker, "data": await request_fmp("historical-price-eod/full", {"symbol": ticker, "from": start, "to": end})}


@mcp.tool()
async def income_statements(symbol: str, period: str = "annual", limit: int = 5) -> dict:
    """Retrieve company income statements. Period is annual or quarter; limit 1-10."""
    if period not in ("annual", "quarter") or not 1 <= limit <= 10:
        raise ValueError("Invalid period or limit")
    ticker = validate_symbol(symbol)
    return {"symbol": ticker, "data": await request_fmp("income-statement", {"symbol": ticker, "period": period, "limit": limit})}


@mcp.tool()
async def key_metrics(symbol: str, period: str = "annual", limit: int = 5) -> dict:
    """Retrieve company financial and valuation key metrics, subject to FMP plan availability."""
    if period not in ("annual", "quarter") or not 1 <= limit <= 10:
        raise ValueError("Invalid period or limit")
    ticker = validate_symbol(symbol)
    return {"symbol": ticker, "data": await request_fmp("key-metrics", {"symbol": ticker, "period": period, "limit": limit})}


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


api = FastAPI(title="FMP Stock Research API", docs_url=None, redoc_url=None, lifespan=lifespan)


@api.get("/health")
def health():
    return {"status": "ok", "fmp_key_configured": bool(os.getenv("FMP_API_KEY")), "service_token_configured": bool(os.getenv("SERVICE_ACCESS_TOKEN")), "auth0_subject_configured": bool(os.getenv("AUTH0_ALLOWED_SUB"))}


@api.get("/quote/{symbol}")
async def quote(symbol: str, authorization: Optional[str] = Header(None)):
    authorize_service(authorization)
    try:
        return await request_fmp("quote", {"symbol": validate_symbol(symbol)})
    except ValueError:
        raise HTTPException(400, "Invalid symbol")
    except RuntimeError:
        raise HTTPException(502, "Upstream FMP request failed")


@api.get("/history/{symbol}")
async def history(symbol: str, start: date = Query(...), end: date = Query(...), authorization: Optional[str] = Header(None)):
    authorize_service(authorization)
    if end < start or (end - start).days > 3650:
        raise HTTPException(400, "Invalid date range")
    try:
        return await request_fmp("historical-price-eod/full", {"symbol": validate_symbol(symbol), "from": start.isoformat(), "to": end.isoformat()})
    except ValueError:
        raise HTTPException(400, "Invalid symbol")
    except RuntimeError:
        raise HTTPException(502, "Upstream FMP request failed")


@api.get("/.well-known/oauth-protected-resource")
@api.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata():
    return {"resource": AUDIENCE, "authorization_servers": [ISSUER], "bearer_methods_supported": ["header"], "scopes_supported": ["openid", "profile", "email", "read:stocks"]}


# Route /mcp directly without a 307 redirect
# Serve the MCP application directly at /mcp
# without redirecting to /mcp/
mcp_asgi = mcp.streamable_http_app()

class MCPPathAdapter:
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await api(scope, receive, send)

api.mount("/mcp", mcp_asgi)

class MCPAuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or not (scope.get("path", "").startswith("/mcp/" ) or scope.get("path") == "/mcp"):
            await self.app(scope, receive, send)
            return
        allowed_sub = os.getenv("AUTH0_ALLOWED_SUB", "").strip()
        if not allowed_sub:
            await JSONResponse({"detail": "AUTH0_ALLOWED_SUB is not configured"}, status_code=503)(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        if not auth.startswith("Bearer "):
            await JSONResponse({"detail": "Bearer token required"}, status_code=401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"'})(scope, receive, send)
            return
        token = auth[7:]
        try:
            key = JWKS.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER, options={"require": ["exp", "iat", "iss", "sub", "aud"]})
            if claims.get("sub") != allowed_sub:
                raise jwt.InvalidTokenError("Account not authorized")
        except (jwt.PyJWTError, jwt.PyJWKClientError, ValueError):
            await JSONResponse({"detail": "Invalid or unauthorized access token"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


app = MCPAuthMiddleware(MCPPathAdapter())

