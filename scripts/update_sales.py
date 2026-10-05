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

# Plausibilitätsprüfung: Warnung, wenn die neueste PDF älter ist als ...
MAX_PDF_AGE_DAYS = 30

# Gesammelte Warnungen. Sie stoppen den Lauf nicht (Daten werden trotzdem
# committet), lassen aber den Workflow am Ende rot werden.
WARNINGS = []


def warn(msg):
    print(f"::warning::{msg}")
    WARNINGS.append(msg)


class PlausiError(Exception):
    """Schwerer Fehler: Es werden keine Daten geschrieben."""


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

        # Für einen Tag können mehrere Versionen existieren (z.B. "-2" als
        # korrigierte Neufassung). SUFFIXES ist aufsteigend sortiert, daher
        # überschreiben wir und behalten am Ende die höchste vorhandene
        # Version = die neueste Korrektur.
        latest = None

        for suffix in SUFFIXES:
            filename = f"{base_name}{suffix}.pdf"
            url = BASE_URL + filename

            try:
                r = requests.head(url, timeout=10)
                if r.status_code == 200:
                    latest = url
            except requests.RequestException as e:
                print(f"Fehler beim Prüfen von {url}: {e}")

        if latest:
            print("PDF gefunden (neueste Version des Tages):", latest)
            return latest, d

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

    # Jede Angebotszeile enthält "€ <Preis> VB € <Pacht>". Gibt es mehr solcher
    # Zeilen als erkannte Objekte, passt die Adresse einer Zeile nicht zum
    # Muster (z.B. Hausnummer "5a" oder neue Schreibweise).
    kandidaten = re.findall(r'€\s*[\d\.]+\s*VB?\s*€\s*[\d\.]+', text)
    if len(kandidaten) > len(sales):
        warn(f"PDF enthält {len(kandidaten)} Angebotszeilen, erkannt wurden nur {len(sales)}. "
             "Vermutlich passt eine Adresse nicht zum Erkennungsmuster.")

    if not sales:
        raise PlausiError("Keine Verkaufsobjekte in der PDF erkannt – Format geändert? Es werden keine Daten geschrieben.")

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

    geo_keys = {
        (normalize(f["properties"].get("addr:street")), normalize(f["properties"].get("addr:housenumber")))
        for f in geo["features"]
    }
    for key, s in sales_index.items():
        if key not in geo_keys:
            warn(f'"{s["addr:street"]} {s["addr:housenumber"]}" steht in der PDF, '
                 "wurde aber keinem Gebäude in haeuser.geojson zugeordnet.")

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
    vorher_aktiv = sum(1 for e in history.values() if e.get("aktiv", True))
    vom_markt = 0
    for haus_id, entry in history.items():
        if haus_id not in aktuelle_ids and entry.get("aktiv", True):
            entry["aktiv"] = False
            entry["verkauft_am"] = today
            vom_markt += 1

    # Verschwindet mehr als die Hälfte der Angebote auf einmal, ist eher die
    # Erkennung kaputt als der Markt leergekauft. Dann nichts speichern.
    if vom_markt >= 3 and vom_markt > vorher_aktiv / 2:
        raise PlausiError(f"{vom_markt} von {vorher_aktiv} Angeboten wären auf einmal verschwunden – "
                          "das ist unplausibel. Es werden keine Daten geschrieben.")

    print("Historie aktualisiert, neue Punkte:", changed, "| vom Markt:", vom_markt)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    print("Historie geschrieben:", HISTORY_FILE)


# ----------------------------------------
# MAIN
# ----------------------------------------

def report_warnings():
    # Anzahl der Warnungen an den Workflow übergeben, Details in die
    # Zusammenfassung des Laufs schreiben (beides nur in GitHub Actions).
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"warnungen={len(WARNINGS)}\n")
    if WARNINGS and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## Plausibilitätsprüfung\n\n")
            f.writelines(f"- {w}\n" for w in WARNINGS)
    print("Warnungen:", len(WARNINGS))


url, pdf_datum = find_latest_pdf()

alter = (datetime.today() - pdf_datum).days
if alter > MAX_PDF_AGE_DAYS:
    warn(f"Die neueste gefundene PDF ist {alter} Tage alt ({url}). "
         "Wurde das Namensschema der Datei geändert?")

download_pdf(url)

sales = parse_sales()

geo = merge_sales(sales)

update_history(geo)

report_warnings()
