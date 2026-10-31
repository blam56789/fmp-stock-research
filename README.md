# FMP Stock Research API

Private FMP-backed quote and historical daily price API. Never commit API keys.

## Railway environment variables

- `FMP_API_KEY`: your FMP API key
- `SERVICE_ACCESS_TOKEN`: a long random secret, different from your FMP key

## Endpoints

- `GET /health`: public readiness check; reports only whether variables exist
- `GET /quote/MSFT`: requires `Authorization: Bearer <SERVICE_ACCESS_TOKEN>`
- `GET /history/SPY?start=2024-01-01&end=2024-12-31`: requires bearer token

For VIX try `/history/%5EVIX?start=2024-01-01&end=2024-12-31` subject to FMP plan availability.

Railway detects Python from `requirements.txt` and starts via `Procfile`.
