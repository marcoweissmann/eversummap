import json
import os
import re
import requests
import pdfplumber
from datetime import datetime, timedelta


HAUS_FILE = "data/haeuser.geojson"
OUTPUT_FILE = "data/haeuser_final.geojson"
HISTORY_FILE = "data/preis_historie.json"

BASE_URL = "https://www.waldferiendorf-eversum.de/upload/"

PDF_FILE = "/tmp/liste.pdf"

# Wie viele Tage rückwirkend gesucht wird
SEARCH_DAYS = 90

# Mögliche Suffixe (leer = kein Suffix, dann -2, -3, -4 ...)
SUFFIXES = ["", "-2", "-3", "-4", "-5"]


def normalize(text):
    if text is None:
        return ""
    return str(text).strip().lower()


# ----------------------------------------
# neueste PDF finden
# ----------------------------------------

def find_latest_pdf():
    today = datetime.today()

    for i in range(SEARCH_DAYS):
        d = today - timedelta(days=i)
        base_name = f"eversum-liste-{d.strftime('%d-%m-%Y')}"

        for suffix in SUFFIXES:
            filename = f"{base_name}{suffix}.pdf"
            url = BASE_URL + filename

            try:
                r = requests.head(url, timeout=10)
                if r.status_code == 200:
                    print("PDF gefunden:", url)
                    return url
            except requests.RequestException as e:
                print(f"Fehler beim Prüfen von {url}: {e}")

    raise Exception(f"Keine Verkaufs-PDF in den letzten {SEARCH_DAYS} Tagen gefunden")


# ----------------------------------------
# PDF herunterladen
# ----------------------------------------

def download_pdf(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()

    with open(PDF_FILE, "wb") as f:
        f.write(r.content)

    print("PDF gespeichert:", PDF_FILE)


# ----------------------------------------
# Verkaufsdaten aus PDF lesen
# ----------------------------------------

def parse_sales():
    text = ""

    with pdfplumber.open(PDF_FILE) as pdf:
        for page in pdf.pages:
            t = page.extract_text()

            if t:
                text += t + "\n"

    pattern = re.compile(
        r'([A-Za-zÄÖÜäöüß\s]+?)\s+(\d+)\s+(\d+)\s+€\s*([\d\.]+)\s*VB?\s*€\s*([\d\.]+)'
    )

    sales = []

    for match in pattern.finditer(text):
        street = match.group(1).strip()
        house = match.group(2)

        flaeche = int(match.group(3))
        preis = int(match.group(4).replace(".", ""))
        pacht = int(match.group(5).replace(".", ""))

        sales.append({
            "addr:street": street,
            "addr:housenumber": house,
            "flaeche": flaeche,
            "preis": preis,
            "pacht": pacht
        })

    print("Verkaufsobjekte erkannt:", len(sales))

    return sales


# ----------------------------------------
# Merge mit Gebäude-GeoJSON
# ----------------------------------------

def merge_sales(sales):
    with open(HAUS_FILE, encoding="utf-8") as f:
        geo = json.load(f)

    sales_index = {}

    for s in sales:
        key = (
            normalize(s["addr:street"]),
            normalize(s["addr:housenumber"])
        )
        sales_index[key] = s

    matched = 0

    for feature in geo["features"]:
        p = feature["properties"]

        street = normalize(p.get("addr:street"))
        house = normalize(p.get("addr:housenumber"))

        key = (street, house)

        if key in sales_index:
            s = sales_index[key]

            p["status"] = "zu_verkaufen"
            p["preis"] = s["preis"]
            p["pacht"] = s["pacht"]
            p["flaeche"] = s["flaeche"]

            matched += 1
        else:
            p.setdefault("status", "nicht_verkauf")

    print("Gematchte Häuser:", matched)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(geo, f, indent=2, ensure_ascii=False)

    print("GeoJSON geschrieben:", OUTPUT_FILE)

    return geo


# ----------------------------------------
# Preis-Historie fortschreiben
# ----------------------------------------
# Pro Haus eine Zeitreihe. Es wird nur dann ein neuer Punkt angehängt,
# wenn sich Preis oder Pacht gegenüber dem letzten Eintrag geändert hat.
# Schlüssel ist die stabile OSM-ID (z.B. "way/366828292").

def update_history(geo):
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
    else:
        history = {}

    today = datetime.today().strftime("%Y-%m-%d")
    changed = 0
    aktuelle_ids = set()

    for feature in geo["features"]:
        p = feature["properties"]

        if p.get("status") != "zu_verkaufen":
            continue

        haus_id = p.get("id") or p.get("@id")
        if not haus_id:
            continue

        aktuelle_ids.add(haus_id)

        preis = p.get("preis")
        pacht = p.get("pacht")
        flaeche = p.get("flaeche")

        entry = history.setdefault(haus_id, {"addr": "", "punkte": []})
        entry["addr"] = f'{p.get("addr:street", "")} {p.get("addr:housenumber", "")}'.strip()
        entry["flaeche"] = flaeche
        entry["aktiv"] = True
        # Falls das Haus zuvor als verkauft galt und nun wieder gelistet ist:
        entry.pop("verkauft_am", None)

        punkte = entry["punkte"]
        last = punkte[-1] if punkte else None

        if last is None or last.get("preis") != preis or last.get("pacht") != pacht:
            punkte.append({"datum": today, "preis": preis, "pacht": pacht})
            changed += 1

    # Häuser, die in der Historie stehen, aber nicht mehr in der aktuellen
    # Liste auftauchen -> vermutlich verkauft / vom Markt genommen.
    vom_markt = 0
    for haus_id, entry in history.items():
        if haus_id not in aktuelle_ids and entry.get("aktiv", True):
            entry["aktiv"] = False
            entry["verkauft_am"] = today
            vom_markt += 1

    print("Historie aktualisiert, neue Punkte:", changed, "| vom Markt:", vom_markt)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    print("Historie geschrieben:", HISTORY_FILE)


# ----------------------------------------
# MAIN
# ----------------------------------------

url = find_latest_pdf()

download_pdf(url)

sales = parse_sales()

geo = merge_sales(sales)

update_history(geo)
