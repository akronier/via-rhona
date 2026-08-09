#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_zeitplan.py  --  ViaRhona-Planer: Start- und Ankunftszeiten ergaenzen

Berechnet je Etappe:
  * Sonnenaufgang (NOAA-Algorithmus, ohne externe Libs)
  * empfohlene Startzeit (Temperaturband, begrenzt durch buergerliche Daemmerung)
  * Brutto-Fahrdauer inkl. grosszuegiger Pausen
  * geschaetzte Ankunftszeit
  * Split-Empfehlung bei zu spaeter Ankunft

und injiziert das Ergebnis als JS-Konstante ZEITPLAN in index.html.

Aufruf:
    python3 patch_zeitplan.py                # nutzt TEMP_FALLBACK
    python3 patch_zeitplan.py --live         # holt Tmax von Open-Meteo
    python3 patch_zeitplan.py --dry-run      # nur Tabelle, kein Patch

Vor dem ersten Lauf: ETAPPEN unten mit den echten GPX-Werten fuellen.
"""

import argparse
import json
import math
import re
import sys
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------
# 1) ETAPPENDATEN  --  aus dem Planer / den GPX-Auswertungen uebernehmen
#    km = korrigierte Distanz aus der Full-Density-Reprocessing-Runde
#    lat/lon = Etappenziel (verifizierte Koordinaten)
# --------------------------------------------------------------------------
ETAPPEN = [
    # (Nr, Datum,        Ziel,                    km,   lat,     lon)
    (1,  "2026-08-11", "PLATZHALTER",           0.0, 46.5197,  6.6323),
    (2,  "2026-08-12", "PLATZHALTER",           0.0, 46.2044,  6.1432),
    (3,  "2026-08-13", "PLATZHALTER",           0.0, 45.9560,  5.8330),
    (4,  "2026-08-14", "PLATZHALTER",           0.0, 45.7600,  5.6900),
    (5,  "2026-08-15", "PLATZHALTER",           0.0, 45.7640,  4.8357),
    (6,  "2026-08-16", "PLATZHALTER",           0.0, 45.5260,  4.8740),
    (7,  "2026-08-17", "PLATZHALTER",           0.0, 45.0430,  4.8340),
    (8,  "2026-08-18", "PLATZHALTER",          82.2, 44.9333,  4.8917),
    (9,  "2026-08-19", "PLATZHALTER",           0.0, 44.5580,  4.7500),
    (10, "2026-08-20", "PLATZHALTER",           0.0, 44.3360,  4.7480),
    (11, "2026-08-21", "PLATZHALTER",           0.0, 43.9493,  4.8055),
    (12, "2026-08-22", "PLATZHALTER",           0.0, 43.8060,  4.6430),
    (13, "2026-08-23", "PLATZHALTER",           0.0, 43.6108,  3.8767),
    (14, "2026-08-24", "PLATZHALTER",           0.0, 43.4050,  3.6940),
    (15, "2026-08-25", "Ecluses de Fonseranes", 0.0, 43.3442,  3.2158),
]

GESAMT_SOLL = 929.4  # km, zur Plausibilitaetspruefung

# --------------------------------------------------------------------------
# 2) MODELLPARAMETER
# --------------------------------------------------------------------------
V_SCHNITT      = 13.0   # km/h, drei Raeder ohne Motor, bepackt
PAUSE_GRUND    = 20     # min, Grundpause je Etappe
PAUSE_PRO_10KM = 12     # min, Trinken / Fotos / Schauen
MITTAG_AB_KM   = 70     # ab dieser Distanz zusaetzliche Mittagspause
MITTAG_MIN     = 45     # min
ANKUNFT_LIMIT  = "14:00"   # spaeter -> Split empfohlen
DAEMMERUNG_MIN = 30     # min vor Sonnenaufgang fahrbar (mit Licht!)

# Tmax-Faustwerte, falls kein --live (Stand 09.08.2026)
TEMP_FALLBACK = {
    "2026-08-11": 29.6, "2026-08-12": 30.4, "2026-08-13": 34.0,
    "2026-08-14": 35.0, "2026-08-15": 34.0, "2026-08-16": 32.0,
}
TEMP_DEFAULT = 34.0   # Annahme fuer Tage ohne Prognose


def startzeit_nach_temp(tmax):
    """Temperaturband -> gewuenschte Startzeit in Minuten nach Mitternacht."""
    if tmax >= 38:
        return 6 * 60
    if tmax >= 36:
        return 6 * 60 + 15
    if tmax >= 33:
        return 6 * 60 + 30
    if tmax >= 32:
        return 6 * 60 + 45
    return 7 * 60


# --------------------------------------------------------------------------
# 3) SONNENAUFGANG (NOAA, vereinfacht; Genauigkeit ca. +/- 1 min)
# --------------------------------------------------------------------------
def sonnenaufgang(d, lat, lon, tz_offset=2.0):
    """Sonnenaufgang als Minuten nach Mitternacht Ortszeit (MESZ = +2)."""
    n = d.toordinal() - date(2000, 1, 1).toordinal() + 0.5 - lon / 360.0
    J = 2451545.0 + n
    M = math.radians((357.5291 + 0.98560028 * n) % 360)
    C = (1.9148 * math.sin(M) + 0.0200 * math.sin(2 * M)
         + 0.0003 * math.sin(3 * M))
    lam = math.radians((math.degrees(M) + C + 180 + 102.9372) % 360)
    J_transit = J + 0.0053 * math.sin(M) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.44)))

    phi = math.radians(lat)
    # -0.833 deg = Refraktion + Sonnenradius
    cos_w = ((math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl))
             / (math.cos(phi) * math.cos(decl)))
    if cos_w > 1 or cos_w < -1:
        return None  # Polartag/-nacht, hier irrelevant
    w = math.degrees(math.acos(cos_w))

    J_rise = J_transit - w / 360.0
    # Julianisches Datum -> UTC-Bruchteil des Tages
    frac = (J_rise + 0.5) % 1.0
    minuten_utc = frac * 24 * 60
    return (minuten_utc + tz_offset * 60) % (24 * 60)


# --------------------------------------------------------------------------
# 4) FAHRZEIT / BRUTTOZEIT
# --------------------------------------------------------------------------
def bruttozeit(km):
    """Brutto-Etappendauer in Minuten, grosszuegig inkl. Pausen."""
    if km <= 0:
        return 0
    fahrt = km / V_SCHNITT * 60
    pausen = PAUSE_GRUND + (km / 10.0) * PAUSE_PRO_10KM
    if km >= MITTAG_AB_KM:
        pausen += MITTAG_MIN
    return fahrt + pausen


def hhmm(minuten):
    if minuten is None:
        return "--:--"
    m = int(round(minuten))
    return "{:02d}:{:02d}".format((m // 60) % 24, m % 60)


# --------------------------------------------------------------------------
# 5) OPEN-METEO (optional)
# --------------------------------------------------------------------------
def tmax_live(lat, lon, tag):
    import urllib.request
    url = ("https://api.open-meteo.com/v1/forecast"
           "?latitude={:.4f}&longitude={:.4f}"
           "&daily=temperature_2m_max&timezone=Europe%2FParis"
           "&start_date={}&end_date={}").format(lat, lon, tag, tag)
    try:
        with urllib.request.urlopen(url, timeout=12) as r:
            data = json.load(r)
        return float(data["daily"]["temperature_2m_max"][0])
    except Exception as e:
        print("  ! Open-Meteo fehlgeschlagen ({}): {}".format(tag, e),
              file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# 6) BERECHNUNG
# --------------------------------------------------------------------------
def berechne(live=False):
    limit_min = int(ANKUNFT_LIMIT[:2]) * 60 + int(ANKUNFT_LIMIT[3:])
    zeilen = []

    for nr, tag, ziel, km, lat, lon in ETAPPEN:
        d = datetime.strptime(tag, "%Y-%m-%d").date()

        tmax = None
        if live:
            tmax = tmax_live(lat, lon, tag)
        if tmax is None:
            tmax = TEMP_FALLBACK.get(tag, TEMP_DEFAULT)

        sa = sonnenaufgang(d, lat, lon)
        frueheste = sa - DAEMMERUNG_MIN if sa is not None else 0
        gewuenscht = startzeit_nach_temp(tmax)
        start = max(gewuenscht, frueheste)

        dauer = bruttozeit(km)
        ankunft = start + dauer

        split = None
        if km > 0 and ankunft > limit_min:
            # Vormittagsblock bis 13:00, Siesta bis 17:00, Rest danach
            vormittag = 13 * 60 - start
            km_vormittag = max(0.0, (vormittag - PAUSE_GRUND
                                     - (vormittag / 60) * PAUSE_PRO_10KM * 0.6)
                               / 60 * V_SCHNITT)
            rest_km = max(0.0, km - km_vormittag)
            wieder = 17 * 60
            ende = wieder + rest_km / V_SCHNITT * 60 + 15
            split = {
                "pause_von": "13:00",
                "pause_bis": "17:00",
                "km_vormittag": round(km_vormittag, 1),
                "km_nachmittag": round(rest_km, 1),
                "ankunft_split": hhmm(ende),
            }

        zeilen.append({
            "nr": nr,
            "datum": tag,
            "ziel": ziel,
            "km": round(km, 1),
            "tmax": round(tmax, 1),
            "sonnenaufgang": hhmm(sa),
            "start": hhmm(start),
            "dauer_min": int(round(dauer)),
            "dauer": hhmm(dauer) if km > 0 else "--:--",
            "ankunft": hhmm(ankunft) if km > 0 else "--:--",
            "split": split,
        })
    return zeilen


# --------------------------------------------------------------------------
# 7) AUSGABE + PATCH
# --------------------------------------------------------------------------
def tabelle(zeilen):
    kopf = ("Et  Datum       km    Tmax  SA     Start  Dauer  Ankunft  Hinweis")
    print(kopf)
    print("-" * len(kopf))
    for z in zeilen:
        hinweis = ""
        if z["split"]:
            hinweis = "SPLIT -> {} / {}".format(
                z["split"]["pause_von"], z["split"]["ankunft_split"])
        print("T{:<3}{}  {:>5}  {:>4}  {}  {}  {}  {}   {}".format(
            z["nr"], z["datum"], z["km"], z["tmax"], z["sonnenaufgang"],
            z["start"], z["dauer"], z["ankunft"], hinweis))
    summe = sum(z["km"] for z in zeilen)
    print("-" * len(kopf))
    print("Summe: {:.1f} km (Soll {:.1f})".format(summe, GESAMT_SOLL))
    if abs(summe - GESAMT_SOLL) > 1.0:
        print("WARNUNG: Distanzsumme weicht ab -- ETAPPEN pruefen!")


def patch(pfad, zeilen):
    with open(pfad, "r", encoding="utf-8") as f:
        html = f.read()

    block = "const ZEITPLAN = " + json.dumps(
        zeilen, ensure_ascii=False, indent=2) + ";"

    muster = re.compile(r"const\s+ZEITPLAN\s*=\s*\[.*?\];", re.S)
    if muster.search(html):
        html_neu = muster.sub(lambda m: block, html, count=1)
        print("ZEITPLAN ersetzt.")
    else:
        # vor dem ersten <script>-Ende einhaengen
        pos = html.find("</script>")
        if pos == -1:
            print("FEHLER: kein </script> gefunden.", file=sys.stderr)
            return False
        html_neu = html[:pos] + "\n" + block + "\n" + html[pos:]
        print("ZEITPLAN neu eingefuegt.")

    with open(pfad + ".bak", "w", encoding="utf-8") as f:
        f.write(html)
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(html_neu)
    print("Backup: {}.bak".format(pfad))
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", default="index.html")
    p.add_argument("--live", action="store_true",
                   help="Tmax von Open-Meteo holen")
    p.add_argument("--dry-run", action="store_true",
                   help="nur Tabelle, kein Patch")
    a = p.parse_args()

    zeilen = berechne(live=a.live)
    tabelle(zeilen)
    if not a.dry_run:
        print()
        patch(a.file, zeilen)


if __name__ == "__main__":
    main()
