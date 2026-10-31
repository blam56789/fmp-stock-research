import os
import hmac
from datetime import date
from typing import Optional
import httpx
from fastapi import FastAPI, Header, HTTPException, Query

app = FastAPI(title='FMP Stock Research API', docs_url=None, redoc_url=None)
BASE = 'https://financialmodelingprep.com/stable'

def authorize(header: Optional[str]):
    token = os.getenv('SERVICE_ACCESS_TOKEN', '')
    if not token:
        raise HTTPException(503, 'Service access token not configured')
    if not header or not header.startswith('Bearer '):
        raise HTTPException(401, 'Bearer token required')
    if not hmac.compare_digest(header[7:], token):
        raise HTTPException(403, 'Invalid token')

async def request_fmp(endpoint: str, params: dict):
    key = os.getenv('FMP_API_KEY')
    if not key:
        raise HTTPException(503, 'FMP key not configured')
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f'{BASE}/{endpoint}', params={**params, 'apikey': key})
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Upstream FMP request failed')
    if isinstance(result, dict) and ('Error Message' in result or 'error' in result):
        raise HTTPException(502, 'FMP returned an error; check endpoint or plan access')
    return result

def validate_symbol(symbol: str):
    if len(symbol) > 20 or not symbol.replace('^', '').replace('-', '').replace('.', '').isalnum():
        raise HTTPException(400, 'Invalid symbol')
    return symbol.upper()

@app.get('/health')
def health():
    return {'status': 'ok', 'fmp_key_configured': bool(os.getenv('FMP_API_KEY')), 'service_token_configured': bool(os.getenv('SERVICE_ACCESS_TOKEN'))}

@app.get('/quote/{symbol}')
async def quote(symbol: str, authorization: Optional[str] = Header(None)):
    authorize(authorization)
    return await request_fmp('quote', {'symbol': validate_symbol(symbol)})

@app.get('/history/{symbol}')
async def history(symbol: str, start: date = Query(...), end: date = Query(...), authorization: Optional[str] = Header(None)):
    authorize(authorization)
    if end < start or (end - start).days > 3650:
        raise HTTPException(400, 'Invalid date range')
    return await request_fmp('historical-price-eod/full', {'symbol': validate_symbol(symbol), 'from': start.isoformat(), 'to': end.isoformat()})
