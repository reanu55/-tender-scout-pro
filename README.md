# Tender Scout Pro – iPhone MVP

Mobile Streamlit-Web-App zum täglichen Scannen und Vorbewerten öffentlicher Lieferaufträge.

## Funktionen
- Ein-Tap-Tages-Scan über die offizielle TED Search API
- Fit-Score 0–100 passend zu Startkapital, Reserve und Zielmarge
- Schätzung von Einkauf, Rohertrag und Finanzierungslücke
- Risikohinweise (Referenzen, Bürgschaft, Sicherheitsanforderungen, kurze Fristen)
- Lieferanten-Recherchelinks für normale B2B-Waren
- CSV/JSON-Import für Exporte anderer Vergabeportale
- Mobile Oberfläche für iPhone
- Regulierte/waffenbezogene Beschaffung wird ausgeschlossen

## Auf GitHub vom iPhone hochladen
1. ZIP in der Dateien-App entpacken.
2. In GitHub ein neues Repository `tender-scout-pro` erstellen.
3. Alle Dateien aus diesem Ordner hochladen, insbesondere `app.py` und `requirements.txt`.
4. Repository öffentlich lassen, wenn du Streamlit Community Cloud kostenlos nutzen möchtest.

## Auf Streamlit Community Cloud veröffentlichen
1. https://share.streamlit.io öffnen und mit GitHub anmelden.
2. `Create app` / `New app` wählen.
3. Repository `tender-scout-pro` auswählen.
4. Branch `main` und Main file `app.py` auswählen.
5. Deploy starten.
6. Die erzeugte `*.streamlit.app`-Adresse in Safari öffnen.
7. Safari → Teilen → `Zum Home-Bildschirm`.

Danach verhält sich Tender Scout auf dem iPhone fast wie eine App.

## Lokal
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Hinweis zu Daten
Streamlit Community Cloud kann den lokalen Dateispeicher bei Neustarts/Neu-Deployments zurücksetzen. Der Live-Scan funktioniert trotzdem. Für dauerhaft gespeicherte Favoriten, Notizen und Scan-Historie wäre als nächster Schritt eine Cloud-Datenbank (z. B. Supabase/Postgres) sinnvoll.

## Datenquelle
TED Search API v3: https://api.ted.europa.eu/v3/notices/search
Dokumentation: https://docs.ted.europa.eu/api/latest/search.html
