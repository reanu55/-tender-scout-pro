# Tender Scout Pro — Rebuild

Diese Version wurde neu aufgebaut, um Archivtreffer und abgelaufene Ausschreibungen konsequent auszuschließen.

## Streamlit
- Repository: dein bestehendes GitHub-Repository
- Branch: `main`
- Main file: `app.py`

## Datenlogik
Der Scanner verwendet TED Search API v3 mit `scope=ACTIVE`, `buyer-country=DEU`, einem dynamischen Veröffentlichungszeitraum und Sortierung nach Publikationsdatum. Danach erfolgt ein zweiter harter lokaler Filter: aktuelle Veröffentlichung, Warenauftrag und offene Angebotsfrist.

Die Deal-Akte kann zusätzlich eine PDF-Leistungsbeschreibung auslesen, um Mengen und Artikel-/Typangaben zu erkennen. Werte werden nicht erfunden; fehlende Angaben werden als unbekannt markiert.
