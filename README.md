# Tender Scout Pro — Production

Mobile-first Streamlit cockpit for analysing active German TED procurement notices.

## Deploy on Streamlit Community Cloud
- Repository: your existing GitHub repository
- Branch: `main`
- Main file path: `app.py`

## TED integration
The app uses `POST https://api.ted.europa.eu/v3/notices/search`, query `buyer-country=DEU`, `scope=ACTIVE`, page-number pagination and an adaptive field fallback.

## Important interpretation
- An estimated procurement value is not guaranteed revenue.
- A calculated max purchase price is a screening estimate, not a supplier quote.
- Exact quantities/SKUs are shown only when present in the notice text.
- Regulated weapons/ammunition/explosives are excluded from sourcing assistance.
