import json
import re
import requests
import pdfplumber
from datetime import datetime, timedelta


HAUS_FILE = "data/haeuser.geojson"
OUTPUT_FILE = "data/haeuser_final.geojson"

BASE_URL = "https://www.waldferiendorf-eversum.de/upload/"


def normalize(text):
    if text is None:
        return ""
    return str(text).strip().lower()


# ----------------------------------------
# neueste PDF finden
# ----------------------------------------

def find_latest_pdf():

    today = datetime.today()

    for i in range(14):

        d = today - timedelta(days=i)

        filename = f"eversum-liste-{d.strftime('%d-%m-%Y')}.pdf"
        url = BASE_URL + filename

        r = requests.head(url)

        if r.status_code == 200:
            print("PDF gefunden:", url)
            return url

    raise Exception("Keine aktuelle Verkaufs-PDF gefunden")


# ----------------------------------------
# PDF herunterladen
# ----------------------------------------

def download_pdf(url):

    r = requests.get(url)

    with open("liste.pdf", "wb") as f:
        f.write(r.content)


# ----------------------------------------
# Verkaufsdaten aus PDF lesen
# ----------------------------------------

def parse_sales():

    text = ""

    with pdfplumber.open("liste.pdf") as pdf:

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


# ----------------------------------------
# MAIN
# ----------------------------------------

url = find_latest_pdf()

download_pdf(url)

sales = parse_sales()

merge_sales(sales)