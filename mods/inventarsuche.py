# Inventarsuche fuer No Man's Sky (NMS.py / pyMHF)
# Liest alle Inventare live aus dem laufenden Spiel und zeigt Suchtreffer im pyMHF-Fenster.
# Ab v0.5 schaltet sie (optional) die Sichtbarkeit von Slot-Ebenen im Inventar-Bildschirm um, damit Treffer
# hervorgehoben werden.
# Ab v0.6: Bauplan-Suche - zeigt fuer einen Gegenstand die Zutaten und in welchem Inventar sie liegen.
# Ab v0.7: ganzer Bauplan-Baum, Einkaufsliste fehlender Grundstoffe, Raffinerie-Hinweise und
#          automatische Uebernahme des im Herstellungsfenster/Baum gezeigten Produkts.
# Spaeter: Raffinerie-Ziele, Kochen (Naehrstoffprozessor), Fundorte in der Info-Box des Spiels, Reittier-Tempo,
#          Knopf "holen" und Nachladen der Gefahrenschutz-Module.
#
# Was die Mod veraendert (alles andere wird nur gelesen):
#   - Spielstaende (save*.hg) und Spieldateien (*.pak) werden NIE geschrieben, nur gelesen.
#   - Im laufenden Spiel, jeweils nur nach Einschalten im Mod-Fenster:
#       * Sichtbarkeit von Slot-Ebenen (Hervorhebung), Textpuffer der Info-Box (eigener Puffer der Mod),
#       * Reittier-Tempo (GcCreatureGlobals, Originalwerte werden gemerkt und wiederhergestellt),
#       * "holen": Stapel verschieben ueber die Spielfunktionen cGcInventoryStore::Add/Remove,
#       * Schutz-Module nachladen: Ladewert schreiben, pruefen, erst dann Ladestoff per Remove abziehen.
#
# Aufbau (Threads):
#   - Spiel-Thread (on_main_loop, Hooks): einziger Ort, der Spielspeicher liest/schreibt. Legt einen
#     "Schnappschuss" der Inventare als reine Python-Daten an.
#   - GUI-Thread (dearpygui, Property main_panel): arbeitet nur mit dem Schnappschuss, nie mit Spielspeicher.
#   - Hintergrund-Threads: Icons/Sprachdateien/Tabellen aus den .pak-Archiven, Spielstand lesen.
#
# Entstanden als Zusammenarbeit von Kismet23 (Idee, Tests im Spiel) und Claude (Code).
# Getestet gegen: nmspy 184271.0 / pymhf 0.2.5

import ctypes
import os
import time
import threading
import traceback
from logging import getLogger

import dearpygui.dearpygui as dpg

from pymhf import Mod
from pymhf.core.memutils import get_addressof
from pymhf.gui.decorators import BOOLEAN, STRING, gui_button, gui_group
from pymhf.gui.widgets import CustomWidget, WidgetBehaviour

import nmspy.data.enums as enums
import nmspy.data.basic_types as basic
import nmspy.data.exported_types as nmse
import nmspy.data.types as nms
from nmspy.common import gameData
from nmspy.decorators import main_loop

logger = getLogger("Inventarsuche")

REFRESH_SECONDS = 1.0     # wie oft der Schnappschuss im Spiel-Thread erneuert wird
MAX_ITEMS_PER_STORE = 2000  # Schutz gegen unplausible Speicherwerte
MAX_RESULT_LINES = 300

TECH = int(enums.cGcInventoryType.Technology)
TYPE_NAMES = {0: "Substanz", 1: "Technik", 2: "Produkt"}


def _as_int(v) -> int:
    """c_enum32 & Co. sind ctypes-Typen -> int() direkt schlaegt fehl, daher .value nutzen."""
    return int(getattr(v, "value", v))


# Zuordnung aus dem Test vom 08.11.2026 (Abstaende zu PlayerState im Inventar-Bildschirm bestaetigt).
# Index in PlayerState.mInventories (0x21 Stores ab Offset 0x910, je 0x248 Bytes) -> Anzeigename.
# Nicht aufgefuehrte Indizes erscheinen als "Inventar N".
INVENTORY_LABELS = {
    0: "Exoanzug",
    1: "Exoanzug Technik",
    2: "Exoanzug Fracht",
    3: "Multiwerkzeug",
    26: "Zutatenlager (Naehrstoffprozessor)",  # von Kismet23 im Spiel bestaetigt, 09.11.2026
    32: "Korvetten-Lager (Bauteile)",  # laut Kismet23: einziges Lager mit Bauteilen, 12.11.2026
    7: "Frachter",  # im Spiel bestaetigt (Abstand 0x1908), 09.11.2026
    8: "Frachter Technik",
}
for _n in range(10):
    INVENTORY_LABELS[12 + _n] = f"Lagercontainer {_n}"


def _fix_text(s: str) -> str:
    """Das Spiel liefert UTF-8; manche Wege dekodieren als Latin-1 -> 'ZÃƒÂ¤he' statt 'Zaehe'. Reparieren."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _clean_id(raw: str) -> str:
    """'^FUEL1' -> 'FUEL1', 'UP_LASER#12345' -> 'UP_LASER'."""
    s = raw.strip("\x00").strip()
    if s.startswith("^"):
        s = s[1:]
    if "#" in s:
        s = s.split("#", 1)[0]
    return s


# Icons werden einmalig aus dem Spielarchiv als PNG in diesen Ordner (neben mods/) entpackt.
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons_cache")
ICON_PX = 28


class IconStore:
    """Holt Item-Icons aus dem Spielarchiv NMSARC.TexUI.pak (nur lesend) und legt sie als kleine PNGs im
    Projektordner ab. Das Entpacken laeuft in einem Hintergrund-Thread, nie im Spiel- oder GUI-Thread."""

    def __init__(self):
        self.paths: dict[str, str] = {}   # Item-ID -> Pfad im Archiv (klein geschrieben)
        self.ready: set[str] = set()       # IDs mit fertigem PNG
        self.failed: set[str] = set()
        self._queue: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.pak: str | None = None
        self.disabled = False
        try:
            os.makedirs(ICON_DIR, exist_ok=True)
            for fn in os.listdir(ICON_DIR):
                if fn.endswith(".png"):
                    self.ready.add(fn[:-4])
        except Exception:
            self.disabled = True

    def find_pak(self):
        try:
            import psutil

            exe_dir = os.path.dirname(psutil.Process().exe())
            pak = os.path.normpath(os.path.join(exe_dir, "..", "GAMEDATA", "PCBANKS", "NMSARC.TexUI.pak"))
            if os.path.exists(pak):
                self.pak = pak
                logger.info(f"Icons: Archiv gefunden: {pak}")
            else:
                self.disabled = True
                logger.info(f"Icons: Archiv nicht gefunden ({pak}), Icons aus.")
        except Exception:
            self.disabled = True
            logger.error("Icons: " + traceback.format_exc())

    def png(self, id_: str) -> str:
        return os.path.join(ICON_DIR, id_ + ".png")

    def request(self, id_: str):
        if self.disabled or not self.pak or id_ in self.ready or id_ in self.failed or id_ not in self.paths:
            return
        with self._lock:
            self._queue.add(id_)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="Inventarsuche-Icons", daemon=True)
                self._thread.start()

    def _work(self):
        try:
            import io

            from hgpaktool.api import HGPAKFile
            from PIL import Image
        except Exception:
            self.disabled = True
            logger.error("Icons: hgpaktool/Pillow fehlen:\n" + traceback.format_exc())
            return
        while True:
            with self._lock:
                batch = list(self._queue)[:40]
                self._queue.difference_update(batch)
            if not batch:
                return
            want = {self.paths[i]: i for i in batch}
            try:
                data = HGPAKFile(self.pak).extract_specific(list(want), as_buffer=False)
            except Exception:
                logger.error("Icons: Entpacken fehlgeschlagen:\n" + traceback.format_exc())
                self.failed.update(batch)
                continue
            for path, id_ in want.items():
                raw = data.get(path)
                try:
                    if not raw:
                        raise ValueError("nicht im Archiv")
                    im = Image.open(io.BytesIO(raw)).convert("RGBA").resize((ICON_PX, ICON_PX), Image.LANCZOS)
                    im.save(self.png(id_))
                    self.ready.add(id_)
                except Exception:
                    self.failed.add(id_)
            time.sleep(0.05)  # dem Spiel Luft lassen


# Zutaten-Slots je Raffinerie. SUIT_REFINER2 und EXO_REFINER von Kismet23 im Spiel geprueft (09.11.2026);
# SUIT_REFINER (Mk. 1) als 1 Slot angenommen.
# Reihenfolge der Exofahrzeug-Inventare (Index 2 = Koloss im Spiel bestaetigt, 09.11.2026)
# Reihenfolge von PlayerState.mVehicleInventories (Index 2 = Koloss im Spiel bestaetigt).
VEHICLE_NAMES = ["Wanderer", "Nomade", "Koloss", "Pilger", "Fahrzeug 5", "Nautilon", "Minotaurus"]
Inventarsuche_COOK_MODES = ("Units (Handel)", "Anomalie (Koch-Bewertung)", "Lebenserhaltung", "Schild / Leben",
                           "Gefahrenschutz", "Ausdauer / Jetpack", "Entdecker-Boni")
# Warteschlange fuer Klicks auf "holen" (GUI-Thread schreibt, Spiel-Thread arbeitet ab)
FETCH_QUEUE: list = []
REFINER_SLOTS = {"BUILD_REFINER1": 1, "BUILD_REFINER2": 2, "BUILD_REFINER3": 3,
                 "SUIT_REFINER": 1, "SUIT_REFINER2": 2, "EXO_REFINER": 3}
# Spielstaende (nur lesen): %APPDATA%\\HelloGames\\NMS\\<Profil>\\save*.hg. Aufbau: Bloecke mit Magic
# 0xFEEDA1E5 + Laengen, LZ4-komprimiert, Inhalt JSON mit verschleierten Schluesseln (z. B. "vLc", "6f=").
SAVE_ROOT = os.path.join(os.environ.get("APPDATA", ""), "HelloGames", "NMS")
SAVE_MAGIC = 0xFEEDA1E5


class EnglishNames:
    """Englische Texte aus den Sprachdateien des Spiels (NMSARC.MetadataEtc.pak, nur lesend), damit auch
    englische Begriffe aus Foren gefunden werden (z. B. "carbon" -> Kohlenstoff). Laedt im Hintergrund."""

    def __init__(self):
        self.texts: dict[str, str] = {}
        self.version = 0
        self._thread: threading.Thread | None = None

    def start(self, pcbanks_dir: str):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._load, args=(pcbanks_dir,), name="Inventarsuche-EN", daemon=True)
        self._thread.start()

    # Sprachdatei-MBIN: ab 0x20 ein dynamisches Array (Offset, Anzahl); je Eintrag 0x130 Bytes =
    # TkID0x20 (Schluessel) + 17 VariableSizeString (Offset, Laenge) fuer die Sprachen. Erster
    # nicht-leerer Text wird genommen.
    def _load(self, pcbanks_dir: str):
        try:
            import struct

            from hgpaktool.api import HGPAKFile

            pak = os.path.join(pcbanks_dir, "NMSARC.MetadataEtc.pak")
            with HGPAKFile(pak) as f:
                files = [n for n in f.filenames if n.startswith("language/") and n.endswith("_english.mbin")]
            blobs = HGPAKFile(pak).extract_specific(files, as_buffer=False)
            texts: dict[str, str] = {}
            for data in blobs.values():
                arr_off, cnt = struct.unpack_from("<QI", data, 0x20)
                base = 0x20 + arr_off
                stride = 0x130  # 0x20 ID + 17 Sprachfelder a 0x10 (geprueft an nms_loc1_english.mbin)
                first = data[base:base + 4]
                if not (first.isascii() and first[:1].isalpha()):
                    continue  # unbekanntes Format -> Datei ueberspringen
                for k in range(cnt):
                    p = base + k * stride
                    if p + stride > len(data):
                        break
                    key = data[p:p + 0x20].split(b"\0")[0].decode("ascii", "ignore")
                    for fld in range((stride - 0x20) // 0x10):
                        q = p + 0x20 + fld * 0x10
                        off, size = struct.unpack_from("<QI", data, q)
                        if size:
                            texts[key] = data[q + off:q + off + size].split(b"\0")[0].decode("utf-8", "replace")
                            break
            self.texts = texts
            self.version += 1
            logger.info(f"Englische Namen geladen: {len(texts)} Texte aus {len(files)} Dateien")
        except Exception:
            logger.error("Englische Namen laden fehlgeschlagen:\n" + traceback.format_exc())


class FarmPlants:
    """Welche Zutaten anbaubar sind: Ernte der Pflanzen aus dem Baumenue (Basisbau). Liest im Hintergrund
    (nur lesend) aus NMSARC.Precache.pak die Tabelle der baubaren Teile (nms_basepartproducts.mbin) und die
    Belohnungstabelle (rewardtable.mbin, Ernte-Eintraege wie PLANT_SNOW -> Frostkristall). Eine baubare
    Pflanze (z. B. SNOWPLANT, CARBONPLANTER) gehoert zum Ernte-Eintrag mit demselben Kern (SNOW, CARBON)."""

    PRODUCT_SIZE = 0x300   # cGcProductData
    PRODUCT_ID = 0x150
    ENTRY_SIZE = 0x38      # cGcGenericRewardTableEntry: List (0x28), Id @0x28
    ITEM_SIZE = 0x28       # cGcRewardTableItem: LabelID, Reward (NMSTemplate @0x10)
    T_SUBSTANCE = bytes.fromhex("75b55145")  # Kennung cGcRewardSpecificSubstance (ID @0x0)
    T_PRODUCT = bytes.fromhex("770bb921")    # Kennung cGcRewardSpecificProduct (ID @0x20)

    def __init__(self):
        self.harvest: dict[str, str] = {}  # Zutat-ID -> baubare Pflanze (Basisteil-ID)
        self.version = 0
        self._thread: threading.Thread | None = None

    def start(self, pcbanks_dir: str):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._load, args=(pcbanks_dir,), name="Inventarsuche-Anbau",
                                        daemon=True)
        self._thread.start()

    @staticmethod
    def _core(i: str) -> str:
        return i.replace("PLANTER", "").replace("PLANT", "").replace("_", "")

    # MBIN-Tabellen werden direkt geparst (kein MBINCompiler noetig): Kopf 0x20 Bytes, danach die Wurzel-
    # Struktur; dynamische Arrays sind (relativer Offset, Anzahl). NMSTemplate-Belohnungen haben eine
    # 4-Byte-Typkennung, an der Substanz bzw. Produkt unterschieden werden.
    def _load(self, pcbanks_dir: str):
        try:
            import struct

            from hgpaktool.api import HGPAKFile

            pak = os.path.join(pcbanks_dir, "NMSARC.Precache.pak")
            bp_name = "metadata/reality/tables/nms_basepartproducts.mbin"
            rw_name = "metadata/reality/tables/rewardtable.mbin"
            blobs = HGPAKFile(pak).extract_specific([bp_name, rw_name], as_buffer=False)
            bp, rw = blobs[bp_name], blobs[rw_name]

            def tkid(d, p):
                return d[p:p + 0x10].split(b"\0")[0].decode("ascii", "replace")

            def arr(d, p):
                off, cnt = struct.unpack_from("<QI", d, p)
                return p + off, cnt

            base, cnt = arr(bp, 0x20)
            parts = set()
            for k in range(cnt):
                i = tkid(bp, base + k * self.PRODUCT_SIZE + self.PRODUCT_ID)
                if "PLANT" in i and self._core(i):
                    parts.add(i)
            by_core = {self._core(i): i for i in parts}
            harvest: dict[str, str] = {}
            for fo in range(0, 0x270, 0x10):  # alle Untertabellen von cGcRewardTable durchsehen
                tb, tc = arr(rw, 0x20 + fo)
                if not (0 < tc < 5000 and tb + tc * self.ENTRY_SIZE <= len(rw)):
                    continue
                for k in range(tc):
                    e = tb + k * self.ENTRY_SIZE
                    eid = tkid(rw, e + 0x28)
                    if not eid.startswith("PLANT"):
                        continue
                    part = by_core.get(self._core(eid))
                    if not part:
                        continue  # kein baubares Teil dazu (z. B. Wildpflanzen)
                    ib, ic = arr(rw, e + 0x10)
                    for j in range(min(ic, 8)):
                        it = ib + j * self.ITEM_SIZE
                        toff, = struct.unpack_from("<Q", rw, it + 0x10)
                        tp, tag = it + 0x10 + toff, rw[it + 0x18:it + 0x1C]
                        rid = tkid(rw, tp) if tag == self.T_SUBSTANCE else (
                            tkid(rw, tp + 0x20) if tag == self.T_PRODUCT else "")
                        if rid and rid.replace("_", "").isalnum():
                            harvest[rid] = part
            self.harvest = harvest
            self.version += 1
            logger.info(f"Anbaubare Zutaten (Pflanzen im Baumenue): {len(harvest)} -> "
                        + ", ".join(f"{k}<-{v}" for k, v in sorted(harvest.items())))
        except Exception:
            logger.error("Anbau-Daten laden fehlgeschlagen:\n" + traceback.format_exc())


class KnownRecipes:
    """Liest aus dem neuesten Spielstand (NUR LESEND) die freigeschalteten Produkte und Technologien.
    Schluessel im Spielstand (verschleiert): vLc -> 6f= -> eZ< (bekannte Produkte), 4kj (bekannte Technik)."""

    def __init__(self):
        self.known: set[str] | None = None
        self.placed: dict[str, dict[str, int]] = {}
        self.version = 0
        self.source = ""
        self._mtime = 0.0
        self._thread: threading.Thread | None = None

    @staticmethod
    def newest_save() -> str | None:
        best, best_t = None, 0.0
        try:
            for d in os.listdir(SAVE_ROOT):
                sd = os.path.join(SAVE_ROOT, d)
                if not os.path.isdir(sd):
                    continue
                for fn in os.listdir(sd):
                    if fn.startswith("save") and fn.endswith(".hg"):
                        p = os.path.join(sd, fn)
                        t = os.path.getmtime(p)
                        if t > best_t:
                            best, best_t = p, t
        except Exception:
            return None
        return best

    def refresh_async(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._load, name="Inventarsuche-Spielstand", daemon=True)
        self._thread.start()

    # Neuesten Spielstand lesen (nur wenn sich die Datei geaendert hat). Freigeschaltete Bauplaene stehen
    # unter vLc -> 6f= -> eZ< (Produkte) und 4kj (Technik); Basen unter F?0 (NKm = Name, @ZJ = Objekte,
    # r<7 = Objekt-ID) - daraus werden aufgestellte Raffinerien gezaehlt.
    def _load(self):
        try:
            import json
            import struct

            import lz4.block

            path = self.newest_save()
            if not path:
                return
            mtime = os.path.getmtime(path)
            if mtime == self._mtime:
                return
            with open(path, "rb") as f:  # nur lesen
                raw = f.read()
            out = bytearray()
            pos = 0
            if struct.unpack_from("<I", raw, 0)[0] == SAVE_MAGIC:
                while pos + 16 <= len(raw):
                    magic, csize, dsize, _ = struct.unpack_from("<IIII", raw, pos)
                    if magic != SAVE_MAGIC:
                        break
                    pos += 16
                    out += lz4.block.decompress(raw[pos:pos + csize], uncompressed_size=dsize)
                    pos += csize
            else:
                out = bytearray(raw)
            data = json.loads(out.rstrip(b"\x00").decode("utf-8", "replace"))
            ps = data["vLc"]["6f="]
            known = {_clean_id(x) for x in ps.get("eZ<", []) + ps.get("4kj", []) if isinstance(x, str)}
            # Gebaute Objekte je Basis (F?0 = Basen, NKm = Name, @ZJ = Objekte, r<7 = Objekt-ID)
            placed: dict[str, dict[str, int]] = {}
            for base in ps.get("F?0", []) or []:
                try:
                    counts: dict[str, int] = {}
                    for obj in base.get("@ZJ", []) or []:
                        oid = _clean_id(str(obj.get("r<7", "")))
                        if oid:
                            counts[oid] = counts.get(oid, 0) + 1
                    placed[str(base.get("NKm", "")) or "?"] = counts
                except Exception:
                    continue
            self.placed = placed
            if not known:
                logger.info("Spielstand: keine Freischalt-Listen gefunden, Filter aus.")
                return
            self.known = known
            self._mtime = mtime
            self.source = os.path.basename(path)
            self.version += 1
            logger.info(f"Spielstand gelesen ({self.source}): {len(known)} freigeschaltete Bauplaene")
        except Exception:
            logger.error("Spielstand lesen fehlgeschlagen:\n" + traceback.format_exc())
            self._mtime = os.path.getmtime(path) if "path" in locals() and path else self._mtime


# =====================================================================================================
# GUI-Bausteine. Jedes Widget hat draw() (einmal aufbauen) und redraw(**werte) (jedes Bild): redraw
# uebernimmt neue Werte aus der Mod und gibt Benutzereingaben als Dict zurueck (pyMHF-Muster).
# =====================================================================================================
class SuggestInput(CustomWidget):
    """Eingabefeld mit Vorschlagsliste darunter (Autovervollstaendigung). Klick auf einen Vorschlag uebernimmt ihn.
    Die Property liefert {"value": str, "suggestions": [str]}, der Setter bekommt {"value": str}."""

    widget_behaviour = WidgetBehaviour.SEPARATE

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self._pending = None
        self._value = ""
        self._shown = None

    def draw(self):
        row = dpg.add_group(horizontal=True)
        self.ids["ROW"] = row
        self.ids["LABEL"] = dpg.add_text(self.label, parent=row)
        self.ids["INPUT"] = dpg.add_input_text(parent=row, width=-1, callback=self._on_input)
        self.ids["LIST"] = dpg.add_listbox(items=[], num_items=1, width=-1, show=False, callback=self._on_pick)

    def _on_input(self, sender, app_data, *args):
        self._pending = app_data or ""

    def _on_pick(self, sender, app_data, *args):
        if app_data:
            self._pending = app_data
            dpg.set_value(self.ids["INPUT"], app_data)

    def redraw(self, value: str = "", suggestions=()):
        result = None
        if self._pending is not None:
            self._value = self._pending
            self._pending = None
            result = {"value": self._value}
        elif value != self._value:
            # Wert wurde von aussen geaendert (z. B. Bauplan aus dem Herstellungsfenster uebernommen).
            self._value = value
            dpg.set_value(self.ids["INPUT"], value)
        sug = tuple(suggestions or ())
        if sug and len(sug) == 1 and sug[0].lower() == self._value.strip().lower():
            sug = ()
        if sug != self._shown:
            self._shown = sug
            dpg.configure_item(self.ids["LIST"], items=list(sug), show=bool(sug), num_items=max(1, min(6, len(sug))))
        return result


# Altes Widget (vor den Tabellen), wird nicht mehr angezeigt, bleibt fuer aeltere Aufrufe erhalten.
class TextBlock(CustomWidget):
    """Mehrzeiliger Text ueber die volle Fensterbreite mit Zeilenumbruch (statt abgeschnittener Tabellenzelle),
    optional mit Icon pro Zeile. Die Property liefert {"rows": [...], "icons": IconStore|None};
    eine Zeile ist entweder Text oder (Item-ID, Text)."""

    widget_behaviour = WidgetBehaviour.SEPARATE
    _textures: dict[str, int | str] = {}

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self._last = None

    def draw(self):
        self.ids["LABEL"] = dpg.add_text(self.label + ":", color=(255, 192, 72))
        self.ids["BODY"] = dpg.add_group(indent=12)
        self.ids["SEP"] = dpg.add_spacer(height=6)

    def _texture(self, icons, id_: str):
        if id_ in TextBlock._textures:
            return TextBlock._textures[id_]
        if icons is None or id_ not in icons.ready:
            if icons is not None:
                icons.request(id_)
            return None
        try:
            from PIL import Image

            im = Image.open(icons.png(id_)).convert("RGBA")
            data = [c / 255.0 for px in im.getdata() for c in px]
            if "REG" not in TextBlock._textures:
                TextBlock._textures["REG"] = dpg.add_texture_registry()
            tag = dpg.add_static_texture(im.width, im.height, data, parent=TextBlock._textures["REG"])
            TextBlock._textures[id_] = tag
            return tag
        except Exception:
            icons.failed.add(id_)
            return None

    def redraw(self, rows=None, icons=None) -> None:
        if isinstance(rows, str):
            rows = rows.split("\n")
        rows = rows or []
        ids_in_rows = [r[0] for r in rows if isinstance(r, tuple) and r[0]]
        ready = tuple(i for i in ids_in_rows if icons is not None and i in icons.ready)
        key = (tuple(rows), ready)
        if key == self._last:
            return
        self._last = key
        body = self.ids["BODY"]
        dpg.delete_item(body, children_only=True)
        for row in rows:
            id_, text = row if isinstance(row, tuple) else (None, row)
            tex = self._texture(icons, id_) if id_ else None
            if tex is not None:
                grp = dpg.add_group(horizontal=True, parent=body)
                dpg.add_image(tex, width=ICON_PX, height=ICON_PX, parent=grp)
                dpg.add_text(text, wrap=0, parent=grp)
            else:
                dpg.add_text(text, wrap=0, parent=body)


# Farben fuer Tabellenzellen/Kopfzeilen (RGB).
GREEN = (120, 220, 120)
YELLOW = (235, 200, 90)
RED = (235, 120, 110)
GRAY = (150, 150, 150)
HEAD = (255, 192, 72)


class DataView(CustomWidget):
    """Ergebnisanzeige als Tabellen (statt Fliesstext). Erhaelt ein "spec"-Dict:
    {"msg": str, "head": [str | (str, farbe)], "sections": [{"title", "collapse", "open", "note", "cols", "rows"}],
     "foot": [str]}. Eine Zelle ist ein Text oder {"t": Text, "i": Item-ID (Icon), "c": Farbe, "ind": Einrueckung}.
    Neu gezeichnet wird nur, wenn sich der Inhalt aendert; aufgeklappte Abschnitte bleiben aufgeklappt."""

    widget_behaviour = WidgetBehaviour.SEPARATE
    _textures: dict = {}

    def __init__(self):
        super().__init__()
        self._last = None
        self._headers: dict[str, int] = {}
        self._open: dict[str, bool] = {}

    def draw(self):
        self.ids["BODY"] = dpg.add_group()

    def _texture(self, icons, id_: str):
        if id_ in DataView._textures:
            return DataView._textures[id_]
        if icons is None or id_ not in icons.ready:
            if icons is not None:
                icons.request(id_)
            return None
        try:
            from PIL import Image

            im = Image.open(icons.png(id_)).convert("RGBA")
            data = [c / 255.0 for px in im.getdata() for c in px]
            if "REG" not in DataView._textures:
                DataView._textures["REG"] = dpg.add_texture_registry()
            tag = dpg.add_static_texture(im.width, im.height, data, parent=DataView._textures["REG"])
            DataView._textures[id_] = tag
            return tag
        except Exception:
            icons.failed.add(id_)
            return None

    def _cell(self, parent, cell, icons):
        if isinstance(cell, dict) and cell.get("btn"):
            dpg.add_button(label=cell["btn"], parent=parent, user_data=cell.get("act"),
                           callback=lambda s, a, u: FETCH_QUEUE.append(u))
            return
        if not isinstance(cell, dict):
            dpg.add_text(str(cell), parent=parent)
            return
        grp = dpg.add_group(horizontal=True, parent=parent)
        if cell.get("ind"):
            dpg.add_spacer(width=14 * cell["ind"], parent=grp)
        if cell.get("i"):
            tex = self._texture(icons, cell["i"])
            if tex is not None:
                dpg.add_image(tex, width=ICON_PX - 6, height=ICON_PX - 6, parent=grp)
        kw = {"color": cell["c"]} if cell.get("c") else {}
        dpg.add_text(cell.get("t", ""), parent=grp, **kw)

    @staticmethod
    def _icon_ids(spec) -> list:
        out = []
        for sec in spec.get("sections", []):
            for row in sec.get("rows", []):
                for cell in row:
                    if isinstance(cell, dict) and cell.get("i"):
                        out.append(cell["i"])
        return out

    def redraw(self, spec=None, icons=None) -> None:
        if spec is None:
            spec = {}
        if isinstance(spec, str):
            spec = {"msg": spec}
        if isinstance(spec, list):  # alte Zeilenform (Fehlertexte o. ae.)
            spec = {"msg": "\n".join(r if isinstance(r, str) else r[1] for r in spec)}
        ready = tuple(i for i in self._icon_ids(spec) if icons is not None and i in icons.ready)
        key = (repr(spec), ready)
        if key == self._last:
            return
        self._last = key
        for title, hid in self._headers.items():
            try:
                self._open[title] = bool(dpg.get_value(hid))
            except Exception:
                pass
        self._headers = {}
        body = self.ids["BODY"]
        dpg.delete_item(body, children_only=True)
        if spec.get("msg"):
            dpg.add_text(spec["msg"], wrap=0, parent=body)
        for line in spec.get("head", []):
            text, color = (line, None) if isinstance(line, str) else line
            dpg.add_text(text, wrap=0, parent=body, **({"color": color} if color else {}))
        for sec in spec.get("sections", []):
            parent = body
            title = sec.get("title")
            if title:
                if sec.get("collapse"):
                    base = title.split(" (")[0]
                    is_open = self._open.get(base, sec.get("open", True))
                    parent = dpg.add_collapsing_header(label=title, default_open=is_open, parent=body)
                    self._headers[base] = parent
                else:
                    dpg.add_text(title, color=HEAD, parent=body)
            if sec.get("note"):
                dpg.add_text(sec["note"], wrap=0, color=GRAY, parent=parent)
            rows = sec.get("rows") or []
            if rows:
                tbl = dpg.add_table(parent=parent, header_row=True, resizable=True, row_background=True,
                                    policy=dpg.mvTable_SizingStretchProp, borders_outerH=True,
                                    borders_innerV=True, borders_outerV=True)
                widths = sec.get("widths") or ([2.5] + [1.0] * (len(sec.get("cols", [])) - 1))
                for n, col in enumerate(sec.get("cols", [])):
                    w = widths[n] if n < len(widths) else 1.0
                    dpg.add_table_column(label=col, parent=tbl, init_width_or_weight=w)
                for row in rows:
                    r = dpg.add_table_row(parent=tbl)
                    for cell in row:
                        self._cell(r, cell, icons)
        for line in spec.get("foot", []):
            dpg.add_text(line, wrap=0, color=GRAY, parent=body)


class CheckBox(CustomWidget):
    """Einfaches Haekchen innerhalb des Haupt-Panels (damit "Reload Mod" funktioniert)."""

    widget_behaviour = WidgetBehaviour.SEPARATE

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self._pending = None
        self._value = None

    def draw(self):
        self.ids["BOX"] = dpg.add_checkbox(label=self.label, callback=self._on_change)

    def _on_change(self, sender, app_data, *args):
        self._pending = bool(app_data)

    def redraw(self, value: bool = False):
        if self._pending is not None:
            self._value, self._pending = self._pending, None
            return {"value": self._value}
        if value != self._value:
            self._value = value
            dpg.set_value(self.ids["BOX"], bool(value))
        return None


class Combo(CustomWidget):
    """Auswahlliste innerhalb des Haupt-Panels."""

    widget_behaviour = WidgetBehaviour.SEPARATE

    def __init__(self, label: str, items):
        super().__init__()
        self.label = label
        self.items = list(items)
        self._pending = None
        self._value = None

    def draw(self):
        row = dpg.add_group(horizontal=True)
        self.ids["ROW"] = row
        dpg.add_text(self.label, parent=row)
        self.ids["BOX"] = dpg.add_combo(items=self.items, parent=row, width=260, callback=self._on_change)

    def _on_change(self, sender, app_data, *args):
        self._pending = app_data

    def redraw(self, value: str = ""):
        if self._pending is not None:
            self._value, self._pending = self._pending, None
            return {"value": self._value}
        if value != self._value:
            self._value = value
            dpg.set_value(self.ids["BOX"], value)
        return None


class Slider(CustomWidget):
    """Schieberegler (Kommazahl) innerhalb des Haupt-Panels."""

    widget_behaviour = WidgetBehaviour.SEPARATE

    def __init__(self, label: str, lo: float, hi: float, default: float = 1.0, fmt: str = "%.2f"):
        super().__init__()
        self.label, self.lo, self.hi, self.default, self.fmt = label, lo, hi, default, fmt
        self._pending = None
        self._value = None

    def draw(self):
        row = dpg.add_group(horizontal=True)
        self.ids["ROW"] = row
        dpg.add_text(self.label, parent=row)
        self.ids["S"] = dpg.add_slider_float(min_value=self.lo, max_value=self.hi, default_value=self.default,
                                             format=self.fmt, width=260, parent=row, callback=self._on_change)

    def _on_change(self, sender, app_data, *args):
        self._pending = float(app_data)

    def redraw(self, value: float = 1.0):
        if self._pending is not None:
            self._value, self._pending = self._pending, None
            return {"value": self._value}
        if value != self._value:
            self._value = value
            dpg.set_value(self.ids["S"], float(value))
        return None


class MainPanel(CustomWidget):
    """Alle Eingaben und Ergebnisse in EINEM Widget mit eigenen Reitern. Grund: pyMHF zeichnet eigene Widgets
    beim "Reload Mod" komplett neu und stolpert, wenn mehrere davon aufeinander folgen ("Item not found").
    Als einziges, letztes Widget funktioniert "Reload Mod"."""

    widget_behaviour = WidgetBehaviour.SEPARATE

    TABS = (
        ("Inventar", ("search", "results")),
        ("Bauplan", ("recipe", "recipe_result")),
        ("Raffinerie", ("refine", "refine_result")),
        ("Kochen", ("cook_mode", "cook_fish", "cook", "cook_result")),
    )

    def __init__(self):
        super().__init__()
        self.parts = {
            "popup_info": CheckBox("Fundorte in der Info-Box des Spiels anzeigen (Test)"),
            "ride": Slider("Reittempo x", 0.5, 3.0),
            "fetch": CheckBox("Knopf \"holen\" anzeigen (verschiebt Stapel in den Exoanzug, Test)"),
            "hazard": CheckBox("Gefahrenschutz + Schutzmodule automatisch nachladen (aus dem Exoanzug, Test)"),
            "hazard_min": Slider("nachladen unter %", 5.0, 90.0, 20.0, "%.0f"),
            "search": SuggestInput("Suche:"),
            "results": DataView(),
            "recipe": SuggestInput("Bauplan:"),
            "recipe_result": DataView(),
            "refine": SuggestInput("Raffinerie-Ziel:"),
            "refine_result": DataView(),
            "cook_mode": Combo("Zweck:", Inventarsuche_COOK_MODES),
            "cook_fish": CheckBox("Fischgerichte zeigen"),
            "cook": SuggestInput("Gericht:"),
            "cook_result": DataView(),
        }
        self._status = None

    def draw(self):
        self.ids["STATUS"] = dpg.add_text("", color=GRAY)
        self.parts["popup_info"].draw()
        self.parts["ride"].draw()
        self.parts["fetch"].draw()
        self.parts["hazard"].draw()
        self.parts["hazard_min"].draw()
        bar = dpg.add_tab_bar()
        self.ids["TABS"] = bar
        for label, names in self.TABS:
            tab = dpg.add_tab(label=label, parent=bar)
            dpg.push_container_stack(tab)
            try:
                for n in names:
                    self.parts[n].draw()
            finally:
                dpg.pop_container_stack()

    def redraw(self, **values):
        status = values.get("status", "")
        if status != self._status:
            self._status = status
            dpg.set_value(self.ids["STATUS"], status)
        changes = {}
        for name, part in self.parts.items():
            res = part.redraw(**values.get(name, {}))
            if res:
                changes[name] = res.get("value", "")
        return changes or None


# =====================================================================================================
# Die eigentliche Mod. pyMHF erzeugt eine Instanz beim Start und bei jedem "Reload Mod" neu; alles,
# was einen Reload ueberleben soll, liegt ausserhalb (Modulebene bzw. im nmspy-Modul, siehe _ride_store).
# Wichtig fuer den Reload: die Property main_panel muss das LETZTE GUI-Element der Klasse bleiben.
# =====================================================================================================
class Inventarsuche(Mod):
    __author__ = "Kismet23 + Claude"
    __description__ = "Live-Suche ueber alle Inventare, optional Hervorhebung im Inventar-Bildschirm"
    __version__ = "0.27.4"

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._search = ""
        self._show_tech = False
        self._snapshot: list[tuple[str, list[tuple]]] = []
        self._snapshot_time = 0.0
        self._status = "Warte auf geladenen Spielstand ..."
        self._result_text = "Noch keine Daten. Lade einen Spielstand."
        self._result_key = None
        self._force_refresh = True
        self._name_keys: dict[str, str] | None = None   # ID -> Sprachschluessel
        self._name_cache: dict[str, str] = {}           # ID -> uebersetzter Name
        self._dump_requested = False
        self._read_errors_logged = 0
        self._ui_inventories_seen: set[int] = set()
        self._explore_armed = False
        self._explore_done: set[int] = set()
        self._draw_calls = 0
        self._ingame_dim = True
        self._ingame_outline = False
        # Pro Inventar-Ebene: Adresse ElementData -> urspruenglicher IsHidden-Wert
        self._touched: dict[int, dict[int, bool]] = {}
        self._match_cache: dict[int, tuple] = {}
        self._shared_check_logged = False
        self._mapping_logged: set[int] = set()
        # Rezept-Suche (v0.6): Eingabe kommt aus dem GUI-Thread, berechnet wird im Spiel-Thread.
        self._recipe_query = ""
        self._recipe_key = None
        self._recipe_text = "Gegenstand eingeben, den du herstellen willst (z. B. hyperantrieb)."
        # v0.9: Raffinerie-Ziel
        self._refine_query = ""
        self._refine_key = None
        self._refine_text = "Stoff eingeben, den du raffinieren willst (z. B. verdichteter kohlenstoff)."
        self._refine_names: dict[str, str] | None = None
        self._refiner_catalog: dict[str, tuple[str, int]] = {}
        self._refine_show_all = False
        self._refine_hide_unusable = False
        self._recipes: dict[str, tuple[str, list[tuple[str, int]]]] | None = None  # ID -> (Art, [(Zutat, Menge)])
        self._recipe_names: dict[str, str] | None = None  # ID -> uebersetzter Name
        self._recipe_highlight = False
        self._recipe_ids: frozenset[str] = frozenset()  # Zutaten des aktuell gezeigten Rezepts
        self._icons = IconStore()  # Item-Icons aus dem Spielarchiv (nur lesend), Cache im Projektordner
        # v0.20: Fundorte in der Info-Box des Spiels (eigener Puffer, Spieldaten bleiben unveraendert)
        self._popup_info = False
        # v0.23: Reittier-Tempo (aendert nur GcCreatureGlobals im Speicher, keine Dateien/Spielstaende)
        self._ride_factor = 1.0
        self._ride_applied = None  # erzwingt nach jedem Laden/Reload einmal das Setzen (auf Original x 1.0)
        self._ride_thread = None
        # v0.24: Knopf "holen" (schreibt Inventare ueber die Spielfunktionen Add/Remove, standardmaessig aus)
        self._fetch_enabled = False
        self._fetch_msg = ""
        # v0.26: Gefahrenschutz automatisch nachladen (standardmaessig aus)
        self._hazard_on = False
        self._hazard_min = 20.0
        self._hazard_info = ""
        self._hazard_cfg = None
        self._hazard_check_at = 0.0
        self._hazard_next_action = 0.0
        self._hazard_last: dict[str, int] = {}
        self._hazard_pending = None
        self._hazard_warn = ""
        self._hazard_blocked: set = set()  # Module, deren geschriebener Wert nicht hielt
        self._hazard_dumped = False
        self._hazard_changes_logged = 0
        self._hazard_none_logged_at = -1e9
        self._store_refs: dict = {}
        self._popup_item: str | None = None
        self._title_to_id: dict[str, str] = {}
        self._where_cache: dict[str, str] = {}
        self._where_cache_t = -1.0
        self._popup_buf = basic.cTkFixedString0x200()
        self._en = EnglishNames()  # englische Namen fuer zweisprachige Suche (nur lesend)
        self._farm = FarmPlants()  # v0.25: anbaubar = Pflanze im Baumenue (nur lesend)
        # v0.11: freigeschaltete Bauplaene aus dem Spielstand (nur lesend) und Vorschlaege beim Tippen
        self._known = KnownRecipes()
        self._only_known = True
        self._known_check_at = 0.0
        self._recipe_suggest: list[str] = []
        self._refine_suggest: list[str] = []
        # v0.19: Kochen (Naehrstoffprozessor)
        self._cooking: dict[str, list] = {}
        self._cook_query = ""
        self._cook_key = None
        self._cook_text = "Gericht oder Zutat eingeben. Leer lassen = alles, was du jetzt kochen kannst."
        self._cook_suggest: list[str] = []
        self._cook_names: dict[str, str] | None = None
        self._cook_mode = "Units (Handel)"
        self._cook_fish = False
        self._cook_props: dict[str, dict] = {}
        self._cook_memo: dict = {}
        self._search_sug_key = None
        self._search_sug: list[str] = []
        self._refiner: dict[str, list] | None = None
        # v0.7: Rezept automatisch aus dem Spiel (Herstellungsfenster / Baum)
        self._recipe_auto = True
        self._recipe_forced: tuple[str, str] | None = None  # (Name klein, ID) des zuletzt im Spiel gezeigten Produkts
        self._auto_last_id = ""
        self._page = -1
        # Erkundung Herstellungsfenster (v0.6.1, nur lesend)
        self._craft_explore = False
        self._craft_last: dict[str, str] = {}
        self._craft_lines = 0

    # ------------------------------------------------------------------ GUI
    # Aufbau (v0.8): oben die beiden Eingabefelder mit ihren Ergebnissen ueber die volle Fensterbreite
    # (mit Zeilenumbruch), darunter aufklappbare Abschnitte fuer Einstellungen und Diagnose.

    with gui_group("Einstellungen"):

        @property
        @BOOLEAN("Technik-Module in der Suche anzeigen")
        def show_tech(self) -> bool:
            return self._show_tech

        @show_tech.setter
        def show_tech(self, value: bool):
            self._show_tech = bool(value)

        @property
        @BOOLEAN("Im Spiel: Nicht-Treffer abdunkeln")
        def ingame_dim(self) -> bool:
            return self._ingame_dim

        @ingame_dim.setter
        def ingame_dim(self, value: bool):
            self._ingame_dim = bool(value)

        @property
        @BOOLEAN("Im Spiel: Treffer umrahmen")
        def ingame_outline(self) -> bool:
            return self._ingame_outline

        @ingame_outline.setter
        def ingame_outline(self, value: bool):
            self._ingame_outline = bool(value)

        @property
        @BOOLEAN("Bauplan automatisch aus Herstellungsfenster")
        def recipe_auto(self) -> bool:
            return self._recipe_auto

        @recipe_auto.setter
        def recipe_auto(self, value: bool):
            self._recipe_auto = bool(value)

        @property
        @BOOLEAN("Alle Raffinerie-Rezepte zeigen (sonst beste 3 je Kategorie)")
        def refine_show_all(self) -> bool:
            return self._refine_show_all

        @refine_show_all.setter
        def refine_show_all(self, value: bool):
            self._refine_show_all = bool(value)
            self._refine_key = None

        @property
        @BOOLEAN("Rezepte ausblenden, die meine Raffinerien nicht schaffen")
        def refine_hide_unusable(self) -> bool:
            return self._refine_hide_unusable

        @refine_hide_unusable.setter
        def refine_hide_unusable(self, value: bool):
            self._refine_hide_unusable = bool(value)
            self._refine_key = None

        @property
        @BOOLEAN("Nur freigeschaltete Bauplaene (aus Spielstand)")
        def only_known(self) -> bool:
            return self._only_known

        @only_known.setter
        def only_known(self, value: bool):
            self._only_known = bool(value)

        @property
        @BOOLEAN("Zutaten im Spiel hervorheben")
        def recipe_highlight(self) -> bool:
            return self._recipe_highlight

        @recipe_highlight.setter
        def recipe_highlight(self, value: bool):
            self._recipe_highlight = bool(value)
            self._mapping_logged = set()

    with gui_group("Diagnose"):

        @gui_button("Jetzt aktualisieren")
        def refresh_now(self):
            self._force_refresh = True

        @gui_button("Alle Inventare ins Log schreiben")
        def dump_inventories(self):
            self._dump_requested = True
            self._force_refresh = True

        @gui_button("Raffinerie-Rezepte als Datei exportieren")
        def export_refiner(self):
            self._export_refiner_requested = True

        @gui_button("Slot-Aufbau beim naechsten Oeffnen ins Log schreiben")
        def arm_explore(self):
            self._explore_done = set()
            self._draw_calls = 0
            self._explore_armed = True
            logger.info("Erkundung aktiv: Bitte jetzt im Spiel das Inventar oeffnen.")

        @property
        @BOOLEAN("Herstell-Erkundung (Log)")
        def craft_explore(self) -> bool:
            return self._craft_explore

        @craft_explore.setter
        def craft_explore(self, value: bool):
            self._craft_explore = bool(value)
            if self._craft_explore:
                self._craft_last = {}
                self._craft_lines = 0
                logger.info("Herstell-Erkundung aktiv: Bitte Herstellungsfenster oeffnen und Produkte anklicken.")

    # Das Haupt-Panel MUSS das letzte GUI-Element sein: pyMHF zeichnet eigene Widgets beim "Reload Mod"
    # neu und haengt sie nur ohne Fehler ans Ende an (ein Nachfolger fuehrt zu "Item not found").
    @property
    @MainPanel()
    def main_panel(self) -> dict:
        # Laeuft im GUI-Thread: nur fertige Python-Daten, nie Spielspeicher.
        # Trefferliste nur neu berechnen, wenn sich etwas geaendert hat (wird jedes Bild abgefragt).
        key = (self._search, self._show_tech, self._snapshot_time, self._en.version, self._fetch_enabled,
               self._fetch_msg)
        if key != self._result_key:
            self._result_key = key
            self._result_text = self._build_result_text()
        return {
            "popup_info": {"value": self._popup_info},
            "ride": {"value": self._ride_factor},
            "fetch": {"value": self._fetch_enabled},
            "hazard": {"value": self._hazard_on},
            "hazard_min": {"value": self._hazard_min},
            "search": {"value": self._search, "suggestions": self._search_suggestions()},
            "status": (self._status or "") + (("   |   " + self._hazard_info) if self._hazard_info else ""),
            "results": {"spec": self._result_text, "icons": self._icons},
            "recipe": {"value": self._recipe_query, "suggestions": self._recipe_suggest},
            "recipe_result": {"spec": self._recipe_text, "icons": self._icons},
            "refine": {"value": self._refine_query, "suggestions": self._refine_suggest},
            "refine_result": {"spec": self._refine_text, "icons": self._icons},
            "cook_mode": {"value": self._cook_mode},
            "cook_fish": {"value": self._cook_fish},
            "cook": {"value": self._cook_query, "suggestions": self._cook_suggest},
            "cook_result": {"spec": self._cook_text, "icons": self._icons},
        }

    @main_panel.setter
    def main_panel(self, value: dict):
        # Bekommt nur die geaenderten Eingabefelder.
        if "search" in value:
            self._search = value["search"] or ""
            self._mapping_logged = set()
        if "recipe" in value:
            self._recipe_query = value["recipe"] or ""
        if "refine" in value:
            self._refine_query = value["refine"] or ""
        if "fetch" in value:
            self._fetch_enabled = bool(value["fetch"])
        if "hazard_min" in value:
            self._hazard_min = min(90.0, max(5.0, float(value["hazard_min"] or 20.0)))
        if "hazard" in value:
            self._hazard_on = bool(value["hazard"])
            if self._hazard_on:
                self._hazard_warn = ""
                self._hazard_blocked = set()
            logger.info(f"Gefahrenschutz-Automatik {'EIN' if self._hazard_on else 'aus'} (unter {self._hazard_min:.0f} %)")
        if "ride" in value:
            self._ride_factor = min(3.0, max(0.5, float(value["ride"] or 1.0)))
        if "popup_info" in value:
            self._popup_info = bool(value["popup_info"])
        if "cook_mode" in value:
            self._cook_mode = value["cook_mode"] or self._cook_mode
        if "cook_fish" in value:
            self._cook_fish = bool(value["cook_fish"])
        if "cook" in value:
            self._cook_query = value["cook"] or ""
    # ------------------------------------------------------- Spiel-Thread

    # ------------------------------------------------------- Reittier-Tempo

    # Reittempo = Original x Faktor fuer GcCreatureGlobals.RideSpeedFast (@0x20A0) und RideSpeedSlow (@0x20A4).
    # Die Adresse der Globals wird einmalig aus NMS.exe ermittelt (Hintergrund-Thread), danach im Spiel-Thread
    # geschrieben. Bei Faktor 1.0 und nach jedem Reload werden die gemerkten Originalwerte zurueckgeschrieben.
    @staticmethod
    def _ride_store():
        """Ablage, die einen "Reload Mod" ueberlebt (das nmspy-Modul wird nicht neu geladen)."""
        import nmspy.globals as ng

        return ng

    def _ride_lookup(self):
        """Hintergrund: Adresse von GcCreatureGlobals ermitteln (liest einmalig NMS.exe, danach Cache)."""
        try:
            ng = self._ride_store()
            if getattr(ng, "_inv_creature_globals", None) is None:
                offsets = ng.extract_global_offsets({"GcCreatureGlobals"})
                cg = ng._map_struct(offsets, ng.nms_types.cGcCreatureGlobals)
                if cg is None:
                    logger.info("Reittempo: GcCreatureGlobals nicht gefunden, Regler wirkungslos.")
                    ng._inv_creature_globals = False
                    return
                ng._inv_creature_globals = cg
            cg = ng._inv_creature_globals
            if cg and getattr(ng, "_inv_ride_orig", None) is None:
                fast, slow = float(cg.RideSpeedFast), float(cg.RideSpeedSlow)
                if not (0.0 < slow < 1000.0 and 0.0 < fast < 1000.0):
                    logger.error(f"Reittempo: unplausible Werte (schnell {fast}, langsam {slow}), nichts geaendert.")
                    ng._inv_creature_globals = False
                    return
                ng._inv_ride_orig = (fast, slow)
                logger.info(f"Reittempo: Originalwerte gemerkt (schnell {fast:.2f}, langsam {slow:.2f})")
        except Exception:
            logger.error("Reittempo: " + traceback.format_exc())

    def _apply_ride(self):
        if self._ride_applied == self._ride_factor:
            return
        ng = self._ride_store()
        cg = getattr(ng, "_inv_creature_globals", None)
        orig = getattr(ng, "_inv_ride_orig", None)
        if cg is None or (cg and orig is None):
            if self._ride_thread is None or not self._ride_thread.is_alive():
                self._ride_thread = threading.Thread(target=self._ride_lookup, name="Inventarsuche-Reiten", daemon=True)
                self._ride_thread.start()
            return
        if not cg or not orig:
            self._ride_applied = self._ride_factor  # nicht verfuegbar, nicht weiter versuchen
            return
        f = self._ride_factor
        cg.RideSpeedFast = orig[0] * f
        cg.RideSpeedSlow = orig[1] * f
        self._ride_applied = f
        logger.info(f"Reittempo: x{f:.2f} (schnell {orig[0] * f:.2f}, langsam {orig[1] * f:.2f})")

    # ------------------------------------------------------- Knopf "holen"

    # "holen": Klick in der Tabelle legt (Inventar, ID, x, y) in FETCH_QUEUE; der Spiel-Thread arbeitet die
    # Schlange in on_main_loop ab. Ablauf je Stapel: Kopie per Add in den Exoanzug -> tatsaechlich
    # hinzugefuegte Menge zaehlen -> genau diese Menge an der Quelle per Remove entfernen -> pruefen.
    def _resolve_store(self, ps, ref):
        kind, i = ref
        return {"inv": ps.mInventories, "ship": ps.mShipInventories, "cargo": ps.mShipInventoriesCargo,
                "veh": ps.mVehicleInventories}[kind][i]

    @staticmethod
    def _count(store, cid: str) -> int:
        vec = store.mStore
        return sum(_as_int(vec[k].Amount) for k in range(len(vec))
                   if _clean_id(str(vec[k].Id)) == cid and _as_int(vec[k].Amount) > 0)

    def _do_fetch(self, label: str, cid: str, x: int, y: int):
        """Verschiebt einen Stapel in den Exoanzug. Nutzt die Spielfunktionen cGcInventoryStore::Add/Remove
        (kein direktes Umschreiben der Inventar-Vektoren). Erst hinzufuegen, pruefen, dann genau die
        hinzugefuegte Menge an der Quelle entfernen."""
        ps = gameData.player_state
        ref = self._store_refs.get(label)
        if ps is None or ref is None:
            self._fetch_msg = ("holen: Inventar nicht gefunden", RED)
            return
        src_store = self._resolve_store(ps, ref)
        dst = ps.mInventories[0]
        vec = src_store.mStore
        el = None
        for k in range(len(vec)):
            e = vec[k]
            if _as_int(e.Index.X) == x and _as_int(e.Index.Y) == y and _clean_id(str(e.Id)) == cid:
                el = e
                break
        if el is None or _as_int(el.Amount) <= 0:
            self._fetch_msg = ("holen: Stapel nicht mehr da (Liste aktualisiert sich gleich)", YELLOW)
            self._force_refresh = True
            return
        amount = _as_int(el.Amount)
        before_dst, before_src = self._count(dst, cid), self._count(src_store, cid)
        item = type(el).from_buffer_copy(el)
        res = nmse.cGcInventoryIndex()
        dst.Add(ctypes.byref(res), ctypes.byref(item))
        moved = self._count(dst, cid) - before_dst
        name = self._display_name(cid)
        if moved <= 0:
            self._fetch_msg = (f"holen: {name} passt nicht in den Exoanzug (kein Platz?). Nichts veraendert.", YELLOW)
            logger.info(f"holen: {cid} aus {label} -> Exoanzug: nichts hinzugefuegt (Add ohne Wirkung)")
            return
        idx = nmse.cGcInventoryIndex()
        idx.X, idx.Y = x, y
        src_store.Remove(idx, min(moved, amount))
        removed = before_src - self._count(src_store, cid)
        logger.info(f"holen: {name} ({cid}) aus {label} [{x + 1}/{y + 1}]: Stapel {amount}, im Exoanzug +{moved}, "
                    f"an der Quelle -{removed}")
        if removed != moved:
            self._fetch_msg = (f"holen: {name}: Exoanzug +{moved}, Quelle -{removed} (Abweichung, siehe Log)", RED)
        else:
            rest = f", {amount - moved} bleiben in {label}" if moved < amount else ""
            self._fetch_msg = (f"holen: {moved} {name} aus {label} in den Exoanzug verschoben{rest}", GREEN)
        self._force_refresh = True

    # ------------------------------------------------ Gefahrenschutz nachladen
    # v0.27.2: alle Schutz-Techniken, die sich mit Ionenakku laden lassen (aus der Technik-Tabelle), in Exoanzug
    # Technik/Allgemein/Fracht; jede Instanz einzeln (z. B. zwei Hitzemodule). Basis-Schutz (PROTECT) nur lesen:
    # sein Wert ist eine Anzeige-Kopie (Test 12.11.: geschrieben 80, nach 1 s wieder 10).
    # Sicher: erst Wert schreiben, nach >= 1 s pruefen, ob er haelt, NUR DANN den Ladestoff abziehen.
    # Haelt ein Wert nicht, wird nichts verbraucht und nur dieses Modul fuer die Automatik gesperrt.

    HAZARD_TECHS = {"PROTECT": "Basis", "T_HOTPROT": "Hitze", "T_COLDPROT": "Kaelte", "T_TOX": "Gift",
                    "T_RAD": "Strahlung"}
    HAZARD_READ_ONLY = {"PROTECT"}
    HAZARD_FALLBACK = {  # aus den Spieldaten (Stand 2026-11), falls die Tabellen nicht lesbar sind
        "PROTECT": ({"CATALYST1": 2, "CATALYST2": 5, "POWERCELL": 80}, 1.0),
        "T_HOTPROT": ({"HOT1": 5, "POWERCELL": 80}, 1.0),
        "T_COLDPROT": ({"COLD1": 5, "POWERCELL": 80}, 1.0),
        "T_TOX": ({"TOXIC1": 5, "POWERCELL": 80}, 1.0),
        "T_RAD": ({"RADIO1": 5, "POWERCELL": 80}, 1.0),
    }

    def _hazard_config(self) -> dict:
        """Je Technik, die sich mit Ionenakku laden laesst: Ladestoffe (ChargeBy) mit Ladewert (ChargeValue) und
        ChargeMultiplier. Nur lesend."""
        if self._hazard_cfg is not None:
            return self._hazard_cfg
        cfg: dict = {}
        from_tables = False
        try:
            rm = gameData.data.mRealityManager
            charge_by: dict[str, tuple[list, float]] = {}
            for entry in rm.mpTechnologyTable.contents.Table:
                tid = _clean_id(str(entry.ID))
                ids = [_clean_id(str(x)) for x in entry.ChargeBy]
                if tid in self.HAZARD_TECHS or "POWERCELL" in ids:
                    charge_by[tid] = (ids, float(entry.ChargeMultiplier) or 1.0)
            want = {c for ids, _m in charge_by.values() for c in ids}
            values: dict[str, int] = {}
            for ptr in (rm.mpSubstanceTable, rm.mpProductTable):
                for entry in ptr.contents.Table:
                    cid = _clean_id(str(entry.ID))
                    if cid in want and cid not in values:
                        v = _as_int(entry.ChargeValue)
                        if v > 0:
                            values[cid] = v
            for tid, (ids, mult) in charge_by.items():
                vals = {c: values[c] for c in ids if c in values}
                if vals and 0.0 < mult < 100.0:
                    cfg[tid] = (vals, mult)
            from_tables = bool(cfg)
        except Exception:
            pass
        for tid, fb in self.HAZARD_FALLBACK.items():
            cfg.setdefault(tid, (dict(fb[0]), fb[1]))
        if not from_tables:
            # v0.27.3: Tabellen noch nicht da (Hauptmenue) -> Standardwerte nur vorlaeufig, spaeter erneut lesen
            return cfg
        self._hazard_cfg = cfg
        for tid, (vals, mult) in cfg.items():
            logger.info(f"Gefahrenschutz: {self._hazard_short(tid)} ({tid}) laden mit "
                        + ", ".join(f"{self._display_name(k)} ({k}) = {v}" for k, v in vals.items())
                        + f", Faktor {mult}" + (" [nur lesen]" if tid in self.HAZARD_READ_ONLY else ""))
        return cfg

    # Anzeigename eines Moduls fuer Statuszeile/Log (bekannte Kurzname, sonst Spielname).
    def _hazard_short(self, tid: str) -> str:
        return self.HAZARD_TECHS.get(tid) or self._display_name(tid)

    @staticmethod
    def _find_el(store, cid: str, x: int | None = None, y: int | None = None):
        vec = store.mStore
        for k in range(len(vec)):
            e = vec[k]
            if _clean_id(str(e.Id)) == cid and (x is None or (_as_int(e.Index.X) == x and _as_int(e.Index.Y) == y)):
                return e
        return None

    def _hazard_dump(self, ps):
        """Einmal je Reload: alle ladbaren Elemente in Exoanzug (0/1/2) ins Log, zur Kontrolle der IDs."""
        if self._hazard_dumped or self._hazard_cfg is None or len(ps.mInventories[1].mStore) == 0:
            return  # erst wenn Spielstand und Tabellen geladen sind
        self._hazard_dumped = True
        for inv in (0, 1, 2):
            vec = ps.mInventories[inv].mStore
            rows = []
            for k in range(len(vec)):
                e = vec[k]
                mx = _as_int(e.MaxAmount)
                if inv == 1 or _clean_id(str(e.Id)) in (self._hazard_cfg or {}):
                    rows.append(f"{str(e.Id).strip(chr(0))}@{_as_int(e.Index.X) + 1}/{_as_int(e.Index.Y) + 1}="
                                f"{_as_int(e.Amount)}/{mx}")
            logger.info(f"Gefahrenschutz-Diagnose: {INVENTORY_LABELS.get(inv, inv)}: " + (", ".join(rows) or "-"))

    # Jede Sekunde im Spiel-Thread: Ladewerte aller Schutz-Module lesen (Statuszeile, Log bei Aenderung),
    # offene Probe auswerten (_hazard_settle) oder bei Bedarf eine neue Probe starten (_hazard_start).
    def _hazard_tick(self):
        now = time.monotonic()
        if now - self._hazard_check_at < 1.0:
            return
        self._hazard_check_at = now
        ps = gameData.player_state
        if ps is None:
            self._hazard_info = ""
            return
        cfg = self._hazard_config()
        self._hazard_dump(ps)
        states = []  # (inv, x, y, tid, short, amt, mx)
        for inv in (1, 0, 2):  # Technik zuerst
            vec = ps.mInventories[inv].mStore
            for k in range(len(vec)):
                e = vec[k]
                tid = _clean_id(str(e.Id))
                if tid not in cfg:
                    continue
                amt, mx = _as_int(e.Amount), _as_int(e.MaxAmount)
                if not (0 < mx <= 100000) or not (0 <= amt <= mx):
                    continue
                x, y = _as_int(e.Index.X), _as_int(e.Index.Y)
                states.append((inv, x, y, tid, self._hazard_short(tid), amt, mx))
                key = (inv, x, y)
                if amt != self._hazard_last.get(key):
                    if self._hazard_changes_logged < 120:
                        self._hazard_changes_logged += 1
                        logger.info(f"Gefahrenschutz: {self._hazard_short(tid)} ({tid} @{x + 1}/{y + 1}) Wert "
                                    f"{self._hazard_last.get(key)} -> {amt} (max {mx})")
                    self._hazard_last[key] = amt
        info = ("Schutz: " + ", ".join(f"{s} {a * 100 // m} %" for _i, _x, _y, _t, s, a, m in states)) if states else ""
        if self._hazard_on:
            info += f" (Automatik unter {self._hazard_min:.0f} %)"
        if self._hazard_warn:
            info += " - " + self._hazard_warn
        self._hazard_info = info
        if self._hazard_pending is not None:
            if now - self._hazard_pending["t"] >= 1.0:
                self._hazard_settle(ps)
            return
        if not self._hazard_on or now < self._hazard_next_action:
            return
        for inv, x, y, tid, short, amt, mx in states:
            if tid in self.HAZARD_READ_ONLY or (inv, x, y) in self._hazard_blocked:
                continue
            if amt * 100.0 / mx < self._hazard_min:
                self._hazard_start(ps, inv, x, y, tid, short, amt, mx)
                return

    def _hazard_start(self, ps, tinv: int, tx: int, ty: int, tid: str, short: str, amt: int, mx: int):
        """Schritt 1: passenden Ladestoff suchen und NUR den Wert schreiben (noch nichts verbrauchen)."""
        vals, mult = self._hazard_config().get(tid, ({}, 1.0))
        need = mx - amt
        # Reihenfolge (Wunsch Kismet23): Ionenakku zuerst (billig, leicht zu finden), dann Rohstoffe (Uran usw. seltener)
        order = sorted(vals, key=lambda c: (vals[c] < 50, vals[c]))
        for cid in order:
            per = max(1, int(vals[cid] * mult))
            for inv in (0, 2):  # Exoanzug, Exoanzug Fracht
                e = self._find_el(ps.mInventories[inv], cid)
                if e is None or _as_int(e.Amount) <= 0:
                    continue
                n = min(_as_int(e.Amount), max(1, -(-need // per)))
                el = self._find_el(ps.mInventories[tinv], tid, tx, ty)
                if el is None:
                    return
                target = min(mx, amt + n * per)
                el.Amount = target
                self._hazard_pending = {"tinv": tinv, "tx": tx, "ty": ty, "tid": tid, "short": short, "before": amt,
                                        "target": target, "cid": cid, "inv": inv, "x": _as_int(e.Index.X),
                                        "y": _as_int(e.Index.Y), "n": n, "t": time.monotonic()}
                logger.info(f"Gefahrenschutz: {short} ({tid} @{tx + 1}/{ty + 1}) {amt} -> {target} geschrieben "
                            f"(Probe, noch nichts verbraucht; danach {n} {self._display_name(cid)})")
                return
        now = time.monotonic()
        self._hazard_next_action = now + 10.0
        if now - self._hazard_none_logged_at > 60.0:
            self._hazard_none_logged_at = now
            logger.info(f"Gefahrenschutz: {short}: kein Ladestoff im Exoanzug ("
                        + ", ".join(self._display_name(c) for c in order) + ")")
        self._hazard_info += f" - {short}: kein Ladestoff im Exoanzug!"

    def _hazard_settle(self, ps):
        """Schritt 2 (>= 1 s spaeter): Wert gehalten? Dann Ladestoff abziehen, sonst dieses Modul sperren."""
        p, self._hazard_pending = self._hazard_pending, None
        self._hazard_next_action = time.monotonic() + 3.0
        el = self._find_el(ps.mInventories[p["tinv"]], p["tid"], p["tx"], p["ty"])
        now_amt = _as_int(el.Amount) if el is not None else -1
        gained = p["target"] - p["before"]
        # gehalten = nach 1 s noch mindestens die Haelfte des Zugewinns da (normaler Verbrauch ist ~5/s)
        if el is None or now_amt < p["before"] + gained // 2:
            self._hazard_blocked.add((p["tinv"], p["tx"], p["ty"]))
            self._hazard_warn = f"{p['short']}: Wert hielt nicht, fuer dieses Modul aus (nichts verbraucht)"
            logger.info(f"Gefahrenschutz: {p['short']} ({p['tid']} @{p['tx'] + 1}/{p['ty'] + 1}) geschrieben "
                        f"{p['target']}, nach 1 s {now_amt} -> das Spiel ueberschreibt den Wert. Nichts verbraucht, "
                        f"Modul fuer die Automatik gesperrt.")
            return
        store = ps.mInventories[p["inv"]]
        src = self._find_el(store, p["cid"], p["x"], p["y"])
        if src is None:
            src = self._find_el(store, p["cid"])
        name = self._display_name(p["cid"])
        if src is None:
            logger.info(f"Gefahrenschutz: {name} inzwischen weg, Ladung bleibt (nicht bezahlt)")
            return
        before = self._count(store, p["cid"])
        idx = nmse.cGcInventoryIndex()
        idx.X, idx.Y = _as_int(src.Index.X), _as_int(src.Index.Y)
        store.Remove(idx, min(p["n"], _as_int(src.Amount)))
        removed = before - self._count(store, p["cid"])
        self._hazard_warn = ""
        logger.info(f"Gefahrenschutz: {p['short']} gehalten ({p['target']}, nach 1 s {now_amt}), "
                    f"{removed} {name} ({p['cid']}) aus {INVENTORY_LABELS.get(p['inv'], p['inv'])} verbraucht"
                    + ("" if removed == p["n"] else f" (geplant {p['n']}, Abweichung)"))

    # Wird nach jedem Bild der Spiel-Hauptschleife aufgerufen (Spiel-Thread). Reihenfolge: Reittempo,
    # Gefahrenschutz, "holen"-Schlange, Spielstand-Pruefung (alle 30 s), Rezept-/Raffinerie-/Koch-Ansichten
    # neu berechnen, wenn sich Eingabe oder Schnappschuss geaendert haben, und den Inventar-Schnappschuss
    # erneuern (alle REFRESH_SECONDS). Fehler werden begrenzt geloggt, damit das Log nicht ueberlaeuft.
    @main_loop.after
    def on_main_loop(self):
        try:
            self._apply_ride()
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Reittempo: " + traceback.format_exc())
        try:
            self._hazard_tick()
        except Exception:
            self._hazard_next_action = time.monotonic() + 30.0
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Gefahrenschutz: " + traceback.format_exc())
        try:
            while FETCH_QUEUE:
                act = FETCH_QUEUE.pop(0)
                if self._fetch_enabled and act:
                    self._do_fetch(*act)
        except Exception:
            self._fetch_msg = ("holen: Fehler, Details im Log-Fenster", RED)
            logger.error("holen: " + traceback.format_exc())
        # Spielstand alle 30 s auf Aenderung pruefen (Lesen laeuft im Hintergrund-Thread, nicht hier).
        if getattr(self, "_export_refiner_requested", False):
            self._export_refiner_requested = False
            try:
                self._export_refiner()
            except Exception:
                logger.error("Export fehlgeschlagen:\n" + traceback.format_exc())
        t = time.monotonic()
        if t - self._known_check_at > 30.0:
            self._known_check_at = t
            self._known.refresh_async()
        try:
            key = (self._recipe_query.strip().lower(), self._snapshot_time, self._only_known, self._known.version, self._en.version)
            if key != self._recipe_key:
                self._recipe_key = key
                old_ids = self._recipe_ids
                self._recipe_text = self._build_recipe_text(key[0])
                if self._recipe_ids != old_ids:
                    self._mapping_logged = set()
        except Exception:
            self._recipe_text = "Fehler bei der Rezept-Suche, Details im Log-Fenster."
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler bei der Rezept-Suche:\n" + traceback.format_exc())
        try:
            key = (self._refine_query.strip().lower(), self._snapshot_time, self._known.version, self._en.version)
            if key != self._refine_key:
                self._refine_key = key
                self._refine_text = self._build_refine_text(key[0])
        except Exception:
            self._refine_text = "Fehler bei der Raffinerie-Suche, Details im Log-Fenster."
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler bei der Raffinerie-Suche:\n" + traceback.format_exc())
        try:
            key = (self._cook_query.strip().lower(), self._snapshot_time, self._en.version, self._cook_mode,
                   self._cook_fish, self._farm.version)
            if key != self._cook_key:
                self._cook_key = key
                self._cook_text = self._build_cook_text(key[0])
        except Exception:
            self._cook_text = "Fehler bei der Koch-Suche, Details im Log-Fenster."
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler bei der Koch-Suche:\n" + traceback.format_exc())
        now = time.monotonic()
        if not self._force_refresh and now - self._snapshot_time < REFRESH_SECONDS:
            return
        self._force_refresh = False
        try:
            ps = gameData.player_state
            if ps is None:
                self._status = "Kein Spielstand geladen."
                self._snapshot_time = now
                return
            snapshot = self._read_all(ps)
            with self._lock:
                self._snapshot = snapshot
                self._snapshot_time = now
            n_items = sum(len(items) for _, items in snapshot)
            self._status = (
                f"{len(snapshot)} Inventare mit Inhalt, {n_items} Stapel  "
                f"(Stand {time.strftime('%H:%M:%S')})"
            )
            if self._dump_requested:
                self._dump_requested = False
                self._dump(ps)
        except Exception:
            self._status = "Fehler beim Auslesen, Details im Log-Fenster."
            logger.error(traceback.format_exc())
            self._snapshot_time = now

    # ------------------------------------------------------------ Auslesen

    # Ein cGcInventoryStore -> Liste (ID, Name, Menge, Max, Typ, x, y). mStore ist ein std::vector von
    # cGcInventoryElement (0x30 Bytes: Id, Index x/y, Amount, MaxAmount, Type). Unplausible Groessen werden
    # verworfen (MAX_ITEMS_PER_STORE), weil der Speicher waehrend Ladebildschirmen inkonsistent sein kann.
    def _read_store(self, store) -> list[tuple]:
        items = []
        try:
            if store.miWidth <= 0 or store.miHeight <= 0:
                return items
            vec = store.mStore
            n = len(vec)
            if n <= 0 or n > MAX_ITEMS_PER_STORE:
                return items
            for i in range(n):
                el = vec[i]
                typ = _as_int(el.Type)
                if el.Amount <= 0 and typ != TECH:
                    continue
                raw_id = str(el.Id)
                items.append(
                    (
                        raw_id,
                        self._display_name(raw_id),
                        _as_int(el.Amount),
                        _as_int(el.MaxAmount),
                        typ,
                        _as_int(el.Index.X),
                        _as_int(el.Index.Y),
                    )
                )
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler beim Lesen eines Inventars:\n" + traceback.format_exc())
        return items

    def _store_name(self, store) -> str:
        try:
            return str(store.mInventoryName).strip("\x00").strip()
        except Exception:
            return ""

    # Alle Inventare des Spielers einsammeln: Exoanzug/Lager (mInventories), Schiffe inkl. Fracht
    # (mShipInventories/-Cargo), Fahrzeuge. Merkt fuer "holen" zu jedem Anzeigenamen, wo der Store liegt.
    def _read_all(self, ps):
        result = []

        refs: dict[str, tuple] = {}
        self._store_refs = refs

        def add(label, store, ref=None):
            items = self._read_store(store)
            if ref is not None:
                refs[label] = ref
            if items:
                result.append((label, items))

        # Exoanzug, Frachter, Lagercontainer usw. -> Zuordnung der Nummern klaeren wir beim ersten Test.
        for i, store in enumerate(ps.mInventories):
            label = INVENTORY_LABELS.get(i)
            name = self._store_name(store)
            if label is None:
                label = f"Inventar {i}"
                if name:
                    label += f" ({self._translate(name)})"
            elif name and name != "BLD_STORAGE_NAME":
                label += f" \"{name}\""  # im Spiel selbst vergebener Name, z. B. Lagercontainer 0 "Lager 0"
            add(label, store, ("inv", i))

        primary = -1
        try:
            primary = _as_int(ps.miPrimaryShip)
        except Exception:
            pass
        for i in range(len(ps.mShipInventories)):
            try:
                ship_name = str(ps.maCustomShipNames[i]).strip("\x00").strip()
            except Exception:
                ship_name = ""
            base = f"Schiff {i + 1}" + (f" \"{ship_name}\"" if ship_name else "")
            if i == primary:
                base += " [aktiv]"
            add(base, ps.mShipInventories[i], ("ship", i))
            add(base + " Fracht", ps.mShipInventoriesCargo[i], ("cargo", i))
            add(base + " Technik", ps.mShipInventoriesTechOnly[i])

        for i in range(len(ps.mVehicleInventories)):
            vname = VEHICLE_NAMES[i] if i < len(VEHICLE_NAMES) else f"Fahrzeug {i + 1}"
            add(vname, ps.mVehicleInventories[i], ("veh", i))
            add(f"{vname} Technik", ps.mVehicleTechInventories[i])
        return result

    def _store_raw(self, store) -> str:
        try:
            vec = store.mStore
            return (
                f"@0x{ctypes.addressof(store):X} {store.miWidth}x{store.miHeight} kap={store.miCapacity} "
                f"vec={vec.vector_size}/{vec.allocated_size} name='{self._store_name(store)}'"
            )
        except Exception as e:
            return f"FEHLER {e!r}"

    def _dump(self, ps):
        logger.info("===== Inventar-Uebersicht =====")
        try:
            logger.info(
                f"GcApplication=0x{ctypes.addressof(gameData.GcApplication):X} "
                f"PlayerState=0x{ctypes.addressof(ps):X}"
            )
        except Exception:
            logger.error(traceback.format_exc())
        for i, store in enumerate(ps.mInventories):
            items = self._read_store(store)
            preview = ", ".join(f"{it[1]} x{it[2]}" for it in items[:4])
            logger.info(f"Inventar {i:2d}: {self._store_raw(store)} -> {len(items)} Stapel. {preview}")
        for i in range(len(ps.mShipInventories)):
            logger.info(f"Schiff {i + 1}: {self._store_raw(ps.mShipInventories[i])}")
        logger.info("===== Ende =====")

    # ------------------------------------------- Diagnose ueber Inventar-Bildschirm

    # Hook: das Spiel zeichnet ein Inventar im Menue (jedes Bild, solange es offen ist). Liefert Seite, GUI-
    # Ebene und Inventar-Zeiger; genutzt fuer die Zuordnung Bildschirm -> Inventar und die Hervorhebung.
    @nms.cGcFrontendPageFunctions.DoInventorySlots.after
    def after_do_inventory_slots(self, lpPage, lpInventoryGuiLayer, lpInventory, *args):
        # Wird aufgerufen, wenn das Spiel ein Inventar im Menue zeichnet. Nur lesen.
        try:
            addr = get_addressof(lpInventory)
            if not addr:
                return
            try:
                self._highlight(lpInventory, lpInventoryGuiLayer, args)
            except Exception:
                if self._read_errors_logged < 5:
                    self._read_errors_logged += 1
                    logger.error("Fehler bei der Hervorhebung:\n" + traceback.format_exc())
            if self._explore_armed:
                self._draw_calls += 1
                layer_addr = get_addressof(lpInventoryGuiLayer)
                if addr not in self._explore_done and layer_addr:
                    self._explore_done.add(addr)
                    self._explore(lpInventory.contents, lpInventoryGuiLayer.contents, args)
                    if len(self._explore_done) >= 3:
                        self._explore_armed = False
                        logger.info(f"Erkundung beendet ({self._draw_calls} Zeichenaufrufe gesehen).")
            if addr in self._ui_inventories_seen or len(self._ui_inventories_seen) > 50:
                return
            self._ui_inventories_seen.add(addr)
            store = lpInventory.contents
            msg = f"Spiel zeigt Inventar: {self._store_raw(store)}"
            ps = gameData.player_state
            if ps is not None:
                rel = addr - ctypes.addressof(ps)
                msg += f" | Abstand zu PlayerState: 0x{rel:X}"
            logger.info(msg)
        except Exception:
            logger.error(traceback.format_exc())

    # ------------------------------------- Erkundung Herstellungsfenster (nur lesend)

    MAX_CRAFT_LINES = 200

    def _craft_log(self, hook: str, msg: str):
        """Nur loggen, wenn sich der Wert fuer diesen Hook geaendert hat; insgesamt begrenzt."""
        # Jede Meldung nur einmal pro Erkundung (das Spiel ruft manche Popups jedes Bild abwechselnd auf).
        seen_key = f"{hook}|{msg}"
        if seen_key in self._craft_last or self._craft_lines >= self.MAX_CRAFT_LINES:
            return
        self._craft_last[seen_key] = msg
        self._craft_lines += 1
        logger.info(f"Herstellung [{hook}] {msg}")
        if self._craft_lines >= self.MAX_CRAFT_LINES:
            logger.info("Herstell-Erkundung: Zeilenlimit erreicht.")

    @staticmethod
    def _cstr(v, limit: int = 80) -> str:
        try:
            if v is None:
                return "-"
            if hasattr(v, "contents"):
                if not v:
                    return "-"
                v = v.contents
            return _fix_text(str(v).strip("\x00").strip())[:limit]
        except Exception as e:
            return f"<{e!r}>"

    # ------------------------------------------- Fundorte in der Info-Box des Spiels (v0.20)
    # Sicher: Die Spieldaten werden NICHT veraendert. Die Diagnose (v0.19.1) hat gezeigt, dass die Beschreibung
    # direkt auf feste Spieldaten zeigt. Deshalb bekommt das Spiel fuer diesen einen Aufruf einen Zeiger auf
    # einen EIGENEN Textpuffer der Mod (uebersetzte Beschreibung + Fundorte); das Original bleibt unberuehrt.

    POPUP_MAX = 0x1FF  # Puffer cTkFixedString<0x200>, letztes Byte bleibt 0

    def _where_text(self, cid: str) -> str:
        # Wird jedes Bild aufgerufen -> pro Schnappschuss zwischenspeichern.
        if self._where_cache_t != self._snapshot_time:
            self._where_cache = {}
            self._where_cache_t = self._snapshot_time
        if cid in self._where_cache:
            return self._where_cache[cid]
        txt = self._where_text_calc(cid)
        self._where_cache[cid] = txt
        return txt

    def _where_text_calc(self, cid: str) -> str:
        where = self._holdings().get(cid, [])
        if not where:
            return ""
        total = sum(a for _, a in where)
        parts = [f"{lbl}: {amt}" for lbl, amt in where[:6]]
        more = f" (+{len(where) - 6} weitere)" if len(where) > 6 else ""
        return f"Insgesamt {total}. Liegt in: " + ", ".join(parts) + more

    # Hooks rund um die Info-Box: DoItemPopup verraet, welches Item gezeigt wird; SetPopupBasics bekommt
    # Titel und Beschreibung. Der before-Hook darf ein neues Argument-Tupel zurueckgeben - pyMHF ruft dann
    # das Original damit auf. So wird nur fuer diesen Aufruf der Beschreibungszeiger auf den eigenen Puffer
    # der Mod umgebogen (_popup_buf), die Spieldaten bleiben unveraendert.
    @nms.cGcFrontendPageFunctions.DoItemPopup.before
    def before_do_item_popup(self, lpPage, lpParentSlot, lItem, leType, *args):
        try:
            self._popup_item = _clean_id(str(lItem.contents.Id)) if (self._popup_info and lItem) else None
        except Exception:
            self._popup_item = None

    @nms.cGcFrontendPageFunctions.SetPopupBasics.before
    def before_set_popup_basics(self, lpParentLayer, lpacTitle, lColour, lpacDescription, lpacSubtitle, *args):
        cid = None
        if self._popup_info and lpacTitle:
            try:
                cid = self._title_to_id.get(str(lpacTitle).strip())
            except Exception:
                cid = None
        if self._popup_info and getattr(self, "_popup_dbg", 0) < 5:
            self._popup_dbg = getattr(self, "_popup_dbg", 0) + 1
            logger.info(f"Info-Box: SetPopupBasics vorher, Item={cid}, Beschreibung vorhanden={bool(lpacDescription)}")
        if not self._popup_info or not cid or not lpacDescription:
            return None
        try:
            info = self._where_text(cid)
            if not info:
                return None
            raw = str(lpacDescription.contents).strip("\x00").strip()
            # Schluessel (z. B. UI_WATER1_DESC) selbst uebersetzen, da wir dem Spiel fertigen Text geben.
            base = self._translate(raw) if raw and " " not in raw and raw.upper() == raw else raw
            text = f"{base}\n\n{info}" if base else info
            data = text.encode("utf-8")[: self.POPUP_MAX]
            while data:
                try:
                    data.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    data = data[:-1]  # nicht mitten in einem Umlaut abschneiden
            buf = self._popup_buf
            ctypes.memset(ctypes.addressof(buf), 0, 0x200)
            ctypes.memmove(ctypes.addressof(buf), data, len(data))
            self._popup_count = getattr(self, "_popup_count", 0) + 1
            if self._popup_count <= 5:
                logger.info(f"Info-Box: Text ersetzt fuer {cid} ({len(data)} Bytes): {text[-80:]!r}")
            return (lpParentLayer, lpacTitle, lColour, ctypes.pointer(buf), lpacSubtitle, *args)
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler Info-Box:\n" + traceback.format_exc())
            return None

    @nms.cGcFrontendPageFunctions.DoItemPopup.after
    def after_do_item_popup(self, lpPage, lpParentSlot, lItem, leType, lbDisabled, lRecipeId, lpStore, *args):
        # v0.7: Zeigt der Spieler im Herstellungsfenster/Baum auf ein herstellbares Produkt (leType 2),
        # wird es als Rezept uebernommen. Nur lesen.
        try:
            if self._recipe_auto and lItem and _as_int(leType) == 2:
                cid = _clean_id(str(lItem.contents.Id))
                if cid and cid != self._auto_last_id:
                    self._auto_last_id = cid
                    name = self._display_name(cid)
                    self._recipe_forced = (name.lower(), cid)
                    self._recipe_query = name
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler bei Rezept-Automatik:\n" + traceback.format_exc())
        if not self._craft_explore:
            return
        try:
            item = "-"
            if lItem:
                el = lItem.contents
                item = f"Id={_clean_id(str(el.Id))} Amount={_as_int(el.Amount)} Type={_as_int(el.Type)}"
            recipe = self._cstr(lRecipeId) if lRecipeId else "-"
            self._craft_log(
                "DoItemPopup",
                f"item({item}) leType={_as_int(leType)} disabled={bool(_as_int(lbDisabled))} "
                f"recipe={recipe} store=0x{get_addressof(lpStore):X}",
            )
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler Herstell-Erkundung DoItemPopup:\n" + traceback.format_exc())

    @nms.cGcFrontendPageFunctions.SetPopupBasics.after
    def after_set_popup_basics(
        self, lpParentLayer, lpacTitle, lColour, lpacDescription, lpacSubtitle, lpacType, *args
    ):
        if not self._craft_explore:
            return
        try:
            amount = _as_int(args[3]) if len(args) > 3 else "?"
            techtree = self._cstr(args[0]) if len(args) > 0 else "-"
            self._craft_log(
                "SetPopupBasics",
                f"title='{self._cstr(lpacTitle)}' subtitle='{self._cstr(lpacSubtitle)}' "
                f"type='{self._cstr(lpacType)}' techtree='{techtree}' desc='{self._cstr(lpacDescription, 60)}' "
                f"amount={amount} desc@0x{get_addressof(lpacDescription):X} sub@0x{get_addressof(lpacSubtitle):X}",
            )
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler Herstell-Erkundung SetPopupBasics:\n" + traceback.format_exc())

    # Hook: Seitenwechsel im Menue (z. B. Herstellungsfenster/Baum) -> merken, welche Seite offen ist.
    @nms.cGcFrontendPage.SetPage.after
    def after_set_page(self, this, lePage, *args):
        try:
            self._page = int(getattr(lePage, "value", lePage))
        except Exception:
            self._page = -1
        if not self._craft_explore:
            return
        try:
            self._craft_log("SetPage", f"page={lePage!s} ({getattr(lePage, 'value', lePage)})")
        except Exception:
            if self._read_errors_logged < 5:
                self._read_errors_logged += 1
                logger.error("Fehler Herstell-Erkundung SetPage:\n" + traceback.format_exc())

    # ------------------------------------------------ Hervorhebung im Spiel

    @staticmethod
    def _layer_id(lyr) -> str:
        if not lyr.mpElementData:
            return ""
        return str(lyr.mpElementData.contents.ID).strip("\x00")

    def _child_layer(self, lyr, wanted: str):
        vec = lyr.mapLayerElements
        n = len(vec)
        if n > 300:
            return None
        for k in range(n):
            ptr = vec[k]
            if ptr:
                child = ptr.contents
                if self._layer_id(child) == wanted:
                    return child
        return None

    def _set_hidden(self, elem, hidden, touched: dict[int, bool]):
        """hidden=True/False setzt den Wert und merkt das Original; hidden=None stellt das Original wieder her."""
        if not elem.mpElementData:
            return
        ed = elem.mpElementData.contents
        key = ctypes.addressof(ed)
        if hidden is None:
            if key in touched:
                orig = touched.pop(key)
                if bool(ed.IsHidden) != orig:
                    ed.IsHidden = orig
            return
        if key not in touched:
            touched[key] = bool(ed.IsHidden)
        if bool(ed.IsHidden) != hidden:
            ed.IsHidden = hidden

    def _matching_coords(self, store, query: str, ids: frozenset | None = None) -> set[tuple[int, int]]:
        """Treffer-Koordinaten: per Suchtext oder (wenn ids gesetzt) per Zutaten-ID-Menge."""
        key = ctypes.addressof(store)
        cached = self._match_cache.get(key)
        stamp = (query, ids, self._snapshot_time)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        coords = set()
        vec = store.mStore
        n = len(vec)
        if 0 < n <= MAX_ITEMS_PER_STORE:
            for i in range(n):
                el = vec[i]
                raw_id = str(el.Id)
                cid = _clean_id(raw_id)
                if ids is not None:
                    hit = cid in ids and _as_int(el.Amount) > 0
                else:
                    name = self._display_name(raw_id)
                    hit = bool(self._matches(query, cid, name))
                if hit:
                    coords.add((_as_int(el.Index.X), _as_int(el.Index.Y)))
        self._match_cache[key] = (stamp, coords)
        return coords

    # Hervorhebung: In der GUI-Ebene des Inventars hat jeder Slot Unter-Ebenen. Fuer Treffer wird eine
    # Rahmen-Ebene sichtbar geschaltet, fuer alle anderen das Original wiederhergestellt (_set_hidden merkt
    # sich den Ausgangszustand). Es werden nur Sichtbarkeits-Flags geaendert, keine Inventardaten.
    def _highlight(self, lpInventory, lpInventoryGuiLayer, args):
        layer_addr = get_addressof(lpInventoryGuiLayer)
        if not layer_addr or len(args) < 5:
            return
        query = self._search.strip().lower()
        ids = self._recipe_ids if (self._recipe_highlight and self._recipe_ids) else None
        active = bool(ids) or bool(query)
        dim = active and self._ingame_dim
        outline_on = active and self._ingame_outline
        touched = self._touched.get(layer_addr)
        if not dim and not outline_on and not touched:
            return  # nichts zu tun, nichts zurueckzusetzen
        if touched is None:
            touched = self._touched[layer_addr] = {}
        restore_only = (not dim and not outline_on) or bool(args[3])  # lbDisabled -> Spiel bestimmt alles
        wide = int(args[1]) if int(args[1]) > 0 else 10
        min_x = min_y = 0
        try:
            if args[4]:
                mi = args[4].contents
                min_x, min_y = _as_int(mi.X), _as_int(mi.Y)
        except Exception:
            pass
        store = lpInventory.contents
        matches = set() if restore_only else self._matching_coords(store, query, ids)
        inv_layer = lpInventoryGuiLayer.contents
        vec = inv_layer.mapLayerElements
        n = len(vec)
        if n > 300:
            return
        log_mapping = not restore_only and ctypes.addressof(store) not in self._mapping_logged
        logged = []
        shared_check = []
        for k in range(n):
            ptr = vec[k]
            if not ptr:
                continue
            slot = ptr.contents
            sid = self._layer_id(slot)
            if not sid.startswith("SLOT") or not sid[4:].isdigit():
                continue
            idx = int(sid[4:]) - 1
            coord = (min_x + idx % wide, min_y + idx // wide)
            is_match = coord in matches
            if log_mapping and is_match:
                logged.append(f"{sid}->{coord}")
            wrapper = self._child_layer_first(slot)
            if wrapper is None:
                continue
            inner = self._child_layer(wrapper, "SLOT_INNER")
            if inner is not None:
                disabled = self._child_layer(inner, "DISABLED")
                if disabled is not None:
                    if len(shared_check) < 2 and disabled.mpElementData:
                        shared_check.append(ctypes.addressof(disabled.mpElementData.contents))
                    if restore_only or not dim:
                        self._set_hidden(disabled, None, touched)
                    else:
                        self._set_hidden(disabled, is_match, touched)
            outline = self._child_layer(wrapper, "OUTLINE")
            if outline is not None:
                elems = [outline]
                for e in range(min(len(outline.mapElements), 10)):
                    eptr = outline.mapElements[e]
                    if eptr:
                        elems.append(eptr.contents)
                for el in elems:
                    if restore_only or not outline_on:
                        self._set_hidden(el, None, touched)
                    else:
                        self._set_hidden(el, not is_match, touched)
        if not touched:
            self._touched.pop(layer_addr, None)
        if not self._shared_check_logged and len(shared_check) == 2:
            self._shared_check_logged = True
            same = shared_check[0] == shared_check[1]
            logger.info(
                f"Pruefung: DISABLED-Daten Slot1=0x{shared_check[0]:X} Slot2=0x{shared_check[1]:X} "
                f"-> {'GETEILT (Problem!)' if same else 'pro Slot (ok)'}"
            )
        if log_mapping:
            self._mapping_logged.add(ctypes.addressof(store))
            mode = f"Zutaten {sorted(ids)}" if ids else f"Suche '{query}'"
            logger.info(
                f"Hervorhebung ({mode}): Inventar {self._store_raw(store)} breite={wide} min=({min_x},{min_y}) "
                f"Treffer-Koordinaten={sorted(matches)} -> Slots {logged}"
            )

    def _child_layer_first(self, lyr):
        vec = lyr.mapLayerElements
        if len(vec) < 1:
            return None
        ptr = vec[0]
        return ptr.contents if ptr else None

    # ------------------------------------------------ Erkundung der Slot-Ebenen

    MAX_EXPLORE_LINES = 400
    MAX_EXPLORE_DEPTH = 6

    def _elem_desc(self, elem) -> str:
        parts = []
        try:
            if elem.mpElementData:
                ed = elem.mpElementData.contents
                parts.append(f"id='{str(ed.ID).strip(chr(0))}'")
                if ed.IsHidden:
                    parts.append("versteckt")
        except Exception as e:
            parts.append(f"ed-fehler {e!r}")
        return " ".join(parts)

    def _colour(self, c) -> str:
        return f"rgba({c.r & 0xFF},{c.g & 0xFF},{c.b & 0xFF},{c.a & 0xFF})"

    def _explore(self, store, layer, args):
        lines: list[str] = []
        try:
            lines.append(
                f"===== Slot-Aufbau fuer Inventar {self._store_raw(store)} | weitere Parameter: "
                f"{[repr(a)[:40] for a in args[:12]]} ====="
            )
        except Exception:
            lines.append("===== Slot-Aufbau =====")

        def walk(lyr, depth: int):
            if len(lines) > self.MAX_EXPLORE_LINES or depth > self.MAX_EXPLORE_DEPTH:
                return
            ind = "  " * depth
            try:
                n_el = len(lyr.mapElements)
                n_ly = len(lyr.mapLayerElements)
            except Exception as e:
                lines.append(f"{ind}[Ebene] FEHLER {e!r}")
                return
            style = ""
            try:
                if lyr.mpLayerData:
                    st = lyr.mpLayerData.contents.Style
                    style = (
                        f" farbe default={self._colour(st.Default.Colour)} "
                        f"highlight={self._colour(st.Highlight.Colour)} active={self._colour(st.Active.Colour)}"
                    )
            except Exception as e:
                style = f" style-fehler {e!r}"
            lines.append(
                f"{ind}[Ebene @0x{ctypes.addressof(lyr):X}] {self._elem_desc(lyr)} "
                f"elemente={n_el} unterebenen={n_ly}{style}"
            )
            if n_el > 200 or n_ly > 200:
                lines.append(f"{ind}  (zu viele Kinder, uebersprungen)")
                return
            for k in range(n_el):
                if len(lines) > self.MAX_EXPLORE_LINES:
                    return
                try:
                    ptr = lyr.mapElements[k]
                    if not ptr:
                        continue
                    el = ptr.contents
                    lines.append(f"{ind}  - Element @0x{get_addressof(ptr):X} {self._elem_desc(el)}")
                except Exception as e:
                    lines.append(f"{ind}  - Element FEHLER {e!r}")
            for k in range(n_ly):
                try:
                    ptr = lyr.mapLayerElements[k]
                    if ptr:
                        walk(ptr.contents, depth + 1)
                except Exception as e:
                    lines.append(f"{ind}  [Unterebene] FEHLER {e!r}")

        walk(layer, 0)
        lines.append("===== Ende Slot-Aufbau =====")
        for line in lines:
            logger.info(line)

    # --------------------------------------------------------------- Namen

    # Namen: Item-ID -> Sprachschluessel (aus den Spieltabellen, _build_name_keys) -> deutscher Text ueber
    # cTkLanguageManager::Translate. Ergebnisse werden zwischengespeichert; solange das Spiel noch im
    # Hauptmenue ist (Tabellen leer), wird nichts gemerkt und spaeter erneut versucht.
    def _translate(self, key: str) -> str:
        try:
            res = nms.cTkLanguageManager.Translate(key)
            return _fix_text(res) if res else key
        except Exception:
            return key

    def _build_name_keys(self):
        keys: dict[str, str] = {}
        try:
            data = gameData.data
            if data is None:
                return None
            rm = data.mRealityManager
            for ptr, cls in (
                (rm.mpSubstanceTable, "sub"),
                (rm.mpProductTable, "prod"),
                (rm.mpTechnologyTable, "tech"),
                (rm.mpProceduralTechTable, "proctech"),  # Upgrades wie UP_UNW3
                (getattr(rm, "mpBasePartProductTable", None), "basepart"),  # z. B. BUILD_REFINER1
            ):
                if not ptr:
                    continue
                table = ptr.contents.Table
                for entry in table:
                    try:
                        id_ = _clean_id(str(entry.ID))
                        name = str(entry.NameLower).strip("\x00").strip() or str(entry.Name).strip()
                        if id_ and name and id_ not in keys:
                            keys[id_] = name
                        # Titel-Schluessel der Info-Box (z. B. UI_MAINTAIN_SUB3_NAME) -> Item-ID
                        for k in (str(entry.Name).strip("\x00").strip(), name):
                            if k and k not in self._title_to_id:
                                self._title_to_id[k] = id_
                    except Exception:
                        continue
                    try:
                        icon = str(entry.Icon.Filename).strip("\x00").strip().replace("\\", "/").lower()
                        if id_ and icon.endswith(".dds") and id_ not in self._icons.paths:
                            self._icons.paths[id_] = icon
                    except Exception:
                        pass
            if self._icons.pak is None and not self._icons.disabled:
                self._icons.find_pak()
            if self._icons.pak:
                self._en.start(os.path.dirname(self._icons.pak))
                self._farm.start(os.path.dirname(self._icons.pak))
        except Exception:
            logger.error(traceback.format_exc())
            return None
        if len(keys) < 100:
            # v0.27.3: Spiel noch im Hauptmenue/Ladebildschirm, Tabellen leer -> nicht merken, spaeter erneut
            if not getattr(self, "_name_keys_empty_logged", False):
                self._name_keys_empty_logged = True
                logger.info(f"Namenstabelle noch leer ({len(keys)} Eintraege), Spieldaten noch nicht geladen - "
                            "neuer Versuch alle 10 s")
            return None
        logger.info(f"Namenstabelle geladen: {len(keys)} Eintraege")
        # Alles, was bisher mit IDs statt Namen zwischengespeichert wurde, neu aufbauen lassen
        self._name_cache.clear()
        self._en.version += 1
        return keys

    def _display_name(self, raw_id: str) -> str:
        id_ = _clean_id(raw_id)
        if id_ in self._name_cache:
            return self._name_cache[id_]
        if self._name_keys is None:
            # Nach einem Fehlschlag nicht bei jedem Item erneut versuchen (sonst Log-Flut), sondern alle 10 s.
            now = time.monotonic()
            if now - getattr(self, "_name_keys_failed_at", -1e9) < 10.0:
                return id_
            self._name_keys = self._build_name_keys()
            if self._name_keys is None:
                self._name_keys_failed_at = now
                return id_
        key = self._name_keys.get(id_)
        name = self._translate(key) if key else id_
        if not name or name == key:
            name = id_
        if name != id_ or not key:
            # v0.27.3: nur echte Namen merken; ist die Uebersetzung noch nicht bereit (Name == Schluessel),
            # beim naechsten Aufruf erneut versuchen
            self._name_cache[id_] = name
        return name

    # ------------------------------------------------------------- Rezepte

    MAX_RECIPE_CANDIDATES = 10

    def _export_refiner(self):
        """Schreibt alle Raffinerie-Rezepte (inkl. Koch-Rezepte, markiert) als JSON in den Projektordner.
        Laeuft im Spiel-Thread, liest nur Spieldaten; geschrieben wird nur in den eigenen Projektordner."""
        import json

        data = gameData.data
        if data is None or not data.mRealityManager.mpRecipeTable:
            logger.info("Export: Rezepttabelle nicht verfuegbar.")
            return
        rows = []
        for rec in data.mRealityManager.mpRecipeTable.contents.Table:
            try:
                out_id = _clean_id(str(rec.Result.Id))
                ins = [(_clean_id(str(i.Id)), _as_int(i.Amount)) for i in rec.Ingredients]
                rows.append({
                    "rezept": str(rec.Id).strip("\x00"),
                    "name": self._translate(str(rec.RecipeName).strip("\x00").strip()),
                    "kochen": bool(rec.Cooking),
                    "zeit": round(float(rec.TimeToMake), 2),
                    "ergebnis": {"id": out_id, "name": self._display_name(out_id), "menge": _as_int(rec.Result.Amount)},
                    "zutaten": [{"id": i, "name": self._display_name(i), "menge": a} for i, a in ins],
                })
            except Exception:
                continue
        folder = os.path.join(os.path.dirname(ICON_DIR), "daten")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "raffinerie_rezepte.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        logger.info(f"Export: {len(rows)} Raffinerie-Rezepte nach {path}")

    def _export_cooking(self, rm, cook):
        """Einmalig: Eigenschaften aller Koch-Produkte und -Zutaten als JSON in den Projektordner (daten/).
        Nur lesen aus den Spieldaten; geschrieben wird nur in den eigenen Projektordner."""
        import json

        ids = set(cook)
        for recs in cook.values():
            for _amt, ins in recs:
                ids.update(i for i, _ in ins)
        fields = ("BaseValue", "CookingValue", "FoodBonusStatAmount", "Level", "RecipeCost", "ChargeValue",
                  "Consumable", "CookingIngredient", "EggModifierIngredient", "GoodForSelling", "IsCraftable")
        enums_ = ("FoodBonusStat", "Type", "Category", "Rarity", "TradeCategory", "WikiCategory")
        rows = {}
        for ptr, table in ((rm.mpProductTable, "produkt"), (rm.mpSubstanceTable, "substanz")):
            if not ptr:
                continue
            for entry in ptr.contents.Table:
                try:
                    cid = _clean_id(str(entry.ID))
                    if cid not in ids or cid in rows:
                        continue
                    row = {"id": cid, "name": self._display_name(cid), "tabelle": table}
                    for f in fields:
                        if hasattr(entry, f):
                            v = getattr(entry, f)
                            row[f] = round(float(v), 4) if isinstance(v, float) else (bool(v) if isinstance(v, bool) else _as_int(v))
                    for f in enums_:
                        if hasattr(entry, f):
                            row[f] = str(getattr(entry, f))
                    try:
                        sub = str(entry.Subtitle).strip("\x00").strip()
                        row["untertitel"] = self._translate(sub) if sub else ""
                    except Exception:
                        pass
                    rows[cid] = row
                except Exception:
                    continue
        self._cook_props = rows
        self._cook_memo = {}
        recipes = [{"ergebnis": out, "menge": amt, "zutaten": [[i, a] for i, a in ins]}
                   for out, recs in cook.items() for amt, ins in recs]
        folder = os.path.join(os.path.dirname(ICON_DIR), "daten")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "koch_daten.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"produkte": rows, "rezepte": recipes}, f, ensure_ascii=False, indent=1)
        logger.info(f"Koch-Daten exportiert: {len(rows)} Produkte/Zutaten, {len(recipes)} Rezepte -> {path}")

    def _load_recipes(self):
        """Liest einmalig alle Produkt- und Technik-Rezepte (Zutatenlisten) aus den Spieldaten. Nur lesend."""
        recipes: dict[str, tuple[str, list[tuple[str, int]]]] = {}
        data = gameData.data
        if data is None:
            return None
        rm = data.mRealityManager
        t0 = time.monotonic()
        for ptr, kind in ((rm.mpProductTable, "Produkt"), (rm.mpTechnologyTable, "Technik")):
            if not ptr:
                continue
            for entry in ptr.contents.Table:
                try:
                    reqs = []
                    for r in entry.Requirements:
                        rid = _clean_id(str(r.ID))
                        amount = _as_int(r.Amount)
                        if rid and amount > 0:
                            reqs.append((rid, amount))
                    id_ = _clean_id(str(entry.ID))
                    if id_ and reqs and id_ not in recipes:
                        recipes[id_] = (kind, reqs)
                except Exception:
                    continue
        # Raffinerie-Rezepte (ohne Koch-Rezepte): Ergebnis-ID -> [(Ergebnismenge, [(Zutat, Menge)])]
        refiner: dict[str, list[tuple[int, list[tuple[str, int]]]]] = {}
        cook: dict[str, list[tuple[int, list[tuple[str, int]]]]] = {}  # Naehrstoffprozessor
        if rm.mpRecipeTable:
            for rec in rm.mpRecipeTable.contents.Table:
                try:
                    cooking = bool(rec.Cooking)
                    out_id = _clean_id(str(rec.Result.Id))
                    out_amt = _as_int(rec.Result.Amount)
                    ins = []
                    for ing in rec.Ingredients:
                        iid = _clean_id(str(ing.Id))
                        iamt = _as_int(ing.Amount)
                        if iid and iamt > 0:
                            ins.append((iid, iamt))
                    # Auch "Vermehrungs"-Rezepte (Ziel ist selbst Zutat, z. B. Chrom. Metall + Cadmium -> Cadmium)
                    # werden aufgenommen; sie haben oft das beste Verhaeltnis.
                    if out_id and out_amt > 0 and ins:
                        (cook if cooking else refiner).setdefault(out_id, []).append((out_amt, ins))
                except Exception:
                    continue
        if not recipes:
            # v0.27.3: Spieldaten noch nicht geladen (Hauptmenue) -> nichts merken, spaeter erneut
            return None
        self._refiner = refiner
        self._cooking = cook
        self._cook_names = None
        try:
            self._export_cooking(rm, cook)
        except Exception:
            logger.error("Koch-Export fehlgeschlagen:\n" + traceback.format_exc())
        # Raffinerien selbst finden (statt Kennungen zu raten): alle Produkte/Technik mit "Raffinerie" im
        # Namen oder REFINER in der ID. Slotzahl: bekannte IDs, sonst aus dem Namen; unbekannt = 0.
        catalog: dict[str, tuple[str, int]] = {}
        # Basis-Bauteile (z. B. BUILD_REFINER1-3) stehen in einer eigenen Tabelle.
        for ptr in (rm.mpProductTable, rm.mpTechnologyTable, getattr(rm, "mpBasePartProductTable", None)):
            if not ptr:
                continue
            for entry in ptr.contents.Table:
                try:
                    cid = _clean_id(str(entry.ID))
                    if not cid or "RECIPE" in cid or "HINT" in cid or cid.startswith("MAINT_"):
                        continue
                    name = self._display_name(cid)
                    low = name.lower()
                    if "raffinerie" not in low and "REFINER" not in cid:
                        continue
                    slots = REFINER_SLOTS.get(cid, 0)
                    if not slots:
                        if "gross" in low or "groß" in low:
                            slots = 3
                        elif "mittler" in low:
                            slots = 2
                        elif "tragbar" in low:
                            slots = 1
                    catalog[cid] = (name, slots)
                except Exception:
                    continue
        self._refiner_catalog = catalog
        logger.info("Raffinerien in den Spieldaten: " + ", ".join(
            f"{cid}='{n}' ({s or '?'} Slots)" for cid, (n, s) in sorted(catalog.items())))
        names = {id_: self._display_name(id_) for id_ in recipes}
        self._recipe_names = names
        logger.info(
            f"Rezepte geladen: {len(recipes)} Bauplaene, {sum(len(v) for v in refiner.values())} "
            f"Raffinerie-Rezepte in {time.monotonic() - t0:.2f}s"
        )
        return recipes

    def _holdings(self) -> dict[str, list[tuple[str, int]]]:
        """Aus dem Schnappschuss: ID -> [(Inventar, Menge)]. Technik-Module zaehlen je Stueck als 1."""
        with self._lock:
            snapshot = list(self._snapshot)
        result: dict[str, dict[str, int]] = {}
        for label, items in snapshot:
            for raw_id, _name, amount, _max, typ, _x, _y in items:
                count = 1 if typ == TECH else amount
                if count <= 0:
                    continue
                per = result.setdefault(_clean_id(raw_id), {})
                per[label] = per.get(label, 0) + count
        return {k: sorted(v.items(), key=lambda kv: -kv[1]) for k, v in result.items()}

    MAX_TREE_DEPTH = 8
    MAX_TREE_LINES = 250

    def _resolve_tree(self, item: str, need: int, holdings, avail: dict[str, int], shopping: dict[str, int],
                      used: set[str], rows: list, level: int, path: tuple[str, ...]):
        """Loest den Bauplan rekursiv auf (Tabellenzeilen in 'rows'). Vorhandenes wird von 'avail' abgebucht,
        Fehlendes hergestellt (wenn freigeschaltetes Rezept) oder in die Einkaufsliste 'shopping' geschrieben."""
        used.add(item)
        have = avail.get(item, 0)
        take = min(have, need)
        avail[item] = have - take
        missing = need - take
        where = holdings.get(item, [])
        total = sum(a for _, a in where)
        recipe = self._recipes.get(item) if self._recipes else None
        locked = bool(recipe) and not self._allowed(item)
        craft = bool(recipe) and not locked and len(path) < self.MAX_TREE_DEPTH and item not in path
        if missing == 0:
            status = {"t": "ok", "c": GREEN}
        elif craft:
            status = {"t": f"fehlt {missing} -> herstellen", "c": YELLOW}
        elif locked:
            status = {"t": f"fehlt {missing} (Bauplan gesperrt)", "c": RED}
        else:
            status = {"t": f"fehlt {missing}", "c": RED}
        if len(rows) < self.MAX_TREE_LINES:
            loc = ", ".join(f"{lbl} {amt}" for lbl, amt in where) or "-"
            if total > have:
                loc += f"  (davon {total - have} schon verplant)"
            rows.append([{"t": self._display_name(item), "i": item, "ind": level}, str(need), str(total), status, loc])
        if missing <= 0:
            return
        if craft:
            for rid, amt in recipe[1]:
                self._resolve_tree(rid, amt * missing, holdings, avail, shopping, used, rows, level + 1,
                                   path + (item,))
        else:
            shopping[item] = shopping.get(item, 0) + missing

    def _refiner_hints(self, item: str, missing: int, holdings, avail: dict[str, int]) -> list[str]:
        """Bis zu 3 Raffinerie-Wege fuer einen fehlenden Grundstoff, machbare zuerst."""
        options = []
        for out_amt, ins in (self._refiner or {}).get(item, []):
            runs = -(-missing // out_amt)  # aufrunden
            parts, ok = [], True
            for iid, iamt in ins:
                need = iamt * runs
                have = avail.get(iid, 0)
                if have < need:
                    ok = False
                where = holdings.get(iid, [])
                loc = ", ".join(f"{lbl} {amt}" for lbl, amt in where[:3]) or "nirgends"
                parts.append(f"{need} {self._display_name(iid)} (frei {have}: {loc})")
            ratio = " + ".join(f"{iamt} {self._display_name(iid)}" for iid, iamt in ins)
            share = sum(min(1.0, avail.get(iid, 0) / (iamt * runs)) for iid, iamt in ins) / len(ins)
            label = "machbar" if ok else ("teilweise" if share > 0 else "fehlt alles")
            options.append((0 if ok else (1 if share > 0 else 2), -share, len(ins),
                            f"{label} ({len(ins)} Zutat{'en' if len(ins) > 1 else ''}): {ratio} -> {out_amt} "
                            f"{self._display_name(item)}; fuer {missing}: " + ", ".join(parts)))
        options.sort(key=lambda o: o[:3])
        return [o[3] for o in options[:3]]

    def _en_name(self, id_: str) -> str:
        """Englischer Name (klein geschrieben) zu einer Item-ID, leer wenn unbekannt. Reine Python-Daten."""
        keys = self._name_keys or {}
        key = keys.get(_clean_id(id_))
        if not key or not self._en.texts:
            return ""
        txt = self._en.texts.get(key) or self._en.texts.get(key.removesuffix("_L")) or ""
        return txt.lower()

    def _matches(self, query: str, id_: str, name: str) -> int:
        """0 = kein Treffer, 1 = Teiltreffer, 2 = exakt. Prueft deutschen Namen, englischen Namen und ID."""
        low, en, cid = name.lower(), self._en_name(id_), id_.lower()
        if query in (low, en, cid):
            return 2
        if query in low or (en and query in en) or query in cid:
            return 1
        return 0

    def _allowed(self, id_: str) -> bool:
        """Bauplan freigeschaltet? Ohne gelesenen Spielstand oder bei ausgeschaltetem Filter immer ja."""
        known = self._known.known
        return not self._only_known or known is None or id_ in known

    def _search_suggestions(self) -> list[str]:
        """Vorschlaege fuer das Suchfeld aus den Item-Namen im Schnappschuss (GUI-Thread, kein Spielspeicher)."""
        q = self._search.strip().lower()
        key = (q, self._snapshot_time, self._en.version)
        if key == self._search_sug_key:
            return self._search_sug
        self._search_sug_key = key
        names = set()
        if q:
            with self._lock:
                snapshot = list(self._snapshot)
            for _label, items in snapshot:
                for raw, name, *_rest in items:
                    if self._matches(q, _clean_id(raw), name):
                        names.add(name)
        self._search_sug = sorted(names, key=lambda n: (not n.lower().startswith(q), n))[:8]
        return self._search_sug

    def _suggest(self, query: str, ids: list[str], names: dict[str, str]) -> list[str]:
        """Bis zu 8 Namen; zuerst die, die mit dem Getippten beginnen (deutsch oder englisch)."""
        out, seen = [], set()

        def starts(i):
            return names[i].lower().startswith(query) or self._en_name(i).startswith(query)

        for i in sorted(ids, key=lambda i: (not starts(i), names[i])):
            n = names[i]
            if n not in seen:
                seen.add(n)
                out.append(n)
            if len(out) >= 8:
                break
        return out

    # Bauplan-Ansicht: Suchbegriff -> passende Produkte -> Baum der Zutaten mit Bestand je Inventar,
    # Einkaufsliste fehlender Grundstoffe und Raffinerie-Wegen. Ergebnis ist ein "spec"-Dict fuer DataView.
    def _build_recipe_text(self, query: str) -> str:
        self._recipe_ids = frozenset()
        self._recipe_suggest = []
        if not query:
            return "Gegenstand eingeben, den du herstellen willst (z. B. hyperantrieb)."
        if self._recipes is None:
            self._recipes = self._load_recipes()
            if self._recipes is None:
                return "Rezepte noch nicht verfuegbar. Lade einen Spielstand."
        names = self._recipe_names or {}
        forced = self._recipe_forced
        if forced and forced[0] == query and forced[1] in self._recipes:
            # Aus dem Spiel uebernommen: genau dieses Produkt, auch wenn der Name mehrdeutig ist.
            names.setdefault(forced[1], self._display_name(forced[1]))
            return self._render_recipe(forced[1], [], names)
        exact, partial = [], []
        hidden = 0
        for id_, name in names.items():
            hit = self._matches(query, id_, name)
            if not hit:
                continue
            hit_exact = hit == 2
            if not self._allowed(id_):
                hidden += 1
                continue
            (exact if hit_exact else partial).append(id_)
        candidates = sorted(exact, key=lambda i: names[i]) + sorted(partial, key=lambda i: names[i])
        self._recipe_suggest = self._suggest(query, candidates, names)
        if not candidates:
            if hidden:
                return f"Kein freigeschalteter Bauplan fuer \"{query}\" ({hidden} noch nicht freigeschaltet)."
            return f"Kein Rezept gefunden fuer \"{query}\"."
        # Bauplan des ersten Treffers (exakter Name bevorzugt) als ganzen Baum aufloesen.
        return self._render_recipe(candidates[0], candidates[1:], names)

    def _render_recipe(self, id_: str, rest: list[str], names: dict[str, str]) -> dict:
        holdings = self._holdings()
        kind, reqs = self._recipes[id_]
        avail = {k: sum(a for _, a in v) for k, v in holdings.items()}
        shopping: dict[str, int] = {}
        used: set[str] = set()
        tree: list = []
        for rid, amt in reqs:
            self._resolve_tree(rid, amt, holdings, avail, shopping, used, tree, 0, (id_,))
        self._recipe_ids = frozenset(used)
        spec: dict = {"head": [], "sections": [], "foot": []}
        if shopping:
            spec["head"].append((f"{names[id_]} ({kind}): es fehlen {len(shopping)} Grundstoffe", RED))
            rows = []
            for sid, miss in sorted(shopping.items(), key=lambda kv: self._display_name(kv[0])):
                hints = self._refiner_hints(sid, miss, holdings, avail)
                rows.append([{"t": self._display_name(sid), "i": sid}, str(miss), hints[0] if hints else "-"])
            spec["sections"].append({"title": "Einkaufsliste", "cols": ["Grundstoff", "fehlt", "bester Raffinerie-Weg"],
                                     "rows": rows})
        else:
            spec["head"].append((f"{names[id_]} ({kind}): alles vorhanden, kann hergestellt werden.", GREEN))
        spec["sections"].append({
            "title": "Bauplan (vorhandene Zwischenprodukte werden zuerst verwendet)",
            "cols": ["Zutat", "benoetigt", "vorhanden", "Status", "Fundort"],
            "rows": tree,
        })
        if len(tree) >= self.MAX_TREE_LINES:
            spec["foot"].append("... (Baum gekuerzt)")
        if rest:
            shown = ", ".join(names[i] for i in rest[: self.MAX_RECIPE_CANDIDATES])
            more = (f" und {len(rest) - self.MAX_RECIPE_CANDIDATES} weitere"
                    if len(rest) > self.MAX_RECIPE_CANDIDATES else "")
            spec["foot"].append(f"Weitere Treffer (genauer eintippen): {shown}{more}")
        return spec

    # ------------------------------------------------------- Raffinerie-Ziel

    @staticmethod
    def _refiner_kind(n: int) -> str:
        """Welche Raffinerie das Rezept verarbeiten kann (nach Anzahl der Zutaten-Slots)."""
        if n <= 1:
            return "1 Zutat: jede Raffinerie (tragbar, mittel, gross)"
        if n == 2:
            return "2 Zutaten: mittlere oder grosse Raffinerie"
        return f"{n} Zutaten: nur grosse Raffinerie"

    REFINE_CATEGORIES = (
        ("Vermehren", "Ziel + Hilfsstoff -> mehr vom Ziel"),
        ("Veredeln", "1 Zutat"),
        ("Mischen (2 Zutaten)", ""),
        ("Mischen (3 Zutaten)", ""),
    )
    REFINE_TOP = 3

    def _refiner_status(self) -> tuple[list[str], int, int]:
        """Welche Raffinerien hat der Spieler? Rueckgabe: (Beschreibungen, groesste gebaute Slotzahl,
        groesste Slotzahl inkl. freigeschalteter Bauplaene). Quellen: gebaute Objekte in Basen (Spielstand),
        Technik in Exoanzug/Fahrzeugen (Schnappschuss), freigeschaltete Bauplaene (Spielstand)."""
        catalog = self._refiner_catalog or {}
        desc, built_max, known_max = [], 0, 0
        for base, objs in (self._known.placed or {}).items():
            for oid, n in objs.items():
                if oid in catalog:
                    slots = catalog[oid][1]
                    built_max = max(built_max, slots or 0)
                    desc.append(f"{catalog[oid][0]} x{n} (Basis {base or '?'})")
        with self._lock:
            snapshot = list(self._snapshot)
        for label, items in snapshot:
            for raw_id, *_rest in items:
                cid = _clean_id(raw_id)
                if cid in catalog:
                    slots = catalog[cid][1]
                    built_max = max(built_max, slots or 0)
                    desc.append(f"{catalog[cid][0]} ({label})")
        known = self._known.known or set()
        for cid, (name, slots) in catalog.items():
            if cid in known and slots:
                known_max = max(known_max, slots)
        return desc, built_max, max(known_max, built_max)

    @staticmethod
    def _slots_name(n: int) -> str:
        return {1: "tragbare", 2: "mittlere", 3: "grosse"}.get(n, f"{n}-Slot")

    def _build_refine_text(self, query: str) -> str:
        """Raffinerie-Rezepte fuer ein Ziel, nach Kategorie gruppiert, mit Bestand und Fundort. Nur lesend."""
        self._refine_suggest = []
        if not query:
            return "Stoff eingeben, den du raffinieren willst (z. B. verdichteter kohlenstoff)."
        if self._recipes is None:
            self._recipes = self._load_recipes()
            if self._recipes is None:
                return "Rezepte noch nicht verfuegbar. Lade einen Spielstand."
        refiner = self._refiner or {}
        if self._refine_names is None:
            # Ergebnisse UND Zutaten, damit man auch nach dem Ausgangsstoff suchen kann (z. B. Zytophosphat).
            ids = set(refiner)
            for recs in refiner.values():
                for _amt, ins in recs:
                    ids.update(i for i, _ in ins)
            self._refine_names = {i: self._display_name(i) for i in ids}
        names = self._refine_names
        exact = [i for i, n in names.items() if i in refiner and self._matches(query, i, n) == 2]
        partial = [i for i, n in names.items() if i in refiner and self._matches(query, i, n) == 1]
        candidates = sorted(exact, key=lambda i: names[i]) + sorted(partial, key=lambda i: names[i])
        ing_hits = sorted((i for i, n in names.items() if self._matches(query, i, n)),
                          key=lambda i: (self._matches(query, i, names[i]) != 2, names[i]))
        self._refine_suggest = self._suggest(query, ing_hits, names)
        if not candidates and not ing_hits:
            return f"Kein Raffinerie-Rezept mit \"{query}\"."
        holdings = self._holdings()
        owned = {k: sum(a for _, a in v) for k, v in holdings.items()}
        desc, built_max, known_max = self._refiner_status()
        if not candidates:
            # Nur als Zutat bekannt (z. B. Zytophosphat): zeigen, was sich daraus raffinieren laesst.
            spec = {"head": [(f"\"{names[ing_hits[0]]}\" ist kein Raffinerie-Ergebnis, aber eine Zutat:", HEAD)],
                    "sections": [], "foot": []}
            spec["sections"].append(self._refine_uses_section(ing_hits[0], names, holdings, owned, built_max, True))
            return spec
        target = candidates[0]
        spec: dict = {"head": [], "sections": [], "foot": []}
        if desc:
            spec["head"].append(f"Deine Raffinerien: {', '.join(desc)}  "
                                f"(groesste: {built_max} Zutat{'en' if built_max != 1 else ''})")
        else:
            spec["head"].append(("Deine Raffinerien: keine gebaute gefunden (Spielstand/Inventare)", GRAY))

        cats: dict[str, list] = {c: [] for c, _ in self.REFINE_CATEGORIES}
        for out_amt, ins in refiner.get(target, []):
            n_in = len(ins)
            self_amt = sum(a for i, a in ins if i == target)
            if self_amt:
                cat = "Vermehren"
            elif n_in == 1:
                cat = "Veredeln"
            else:
                cat = f"Mischen ({min(n_in, 3)} Zutaten)"
            runs = min((owned.get(i, 0) // a for i, a in ins), default=0)
            share = sum(min(1.0, owned.get(i, 0) / a) for i, a in ins) / n_in if ins else 0.0
            can_process = built_max == 0 or n_in <= built_max
            if not can_process:
                group = 3
            elif runs > 0:
                group = 0
            elif share > 0:
                group = 1
            else:
                group = 2
            used_units = sum(a for i, a in ins if i != target) or 1
            gain = out_amt - self_amt
            yield_per_unit = gain / used_units
            if group == 0:
                status = {"t": f"machbar: {runs}x = {runs * out_amt}", "c": GREEN}
            elif group == 1:
                status = {"t": f"teilweise: {round(share * 100)} %", "c": YELLOW}
            elif group == 2:
                status = {"t": "nichts vorhanden", "c": GRAY}
            else:
                need = self._slots_name(n_in)
                status = {"t": (f"braucht {need} Raffinerie (Bauplan vorhanden)" if n_in <= known_max
                                else f"braucht {need} Raffinerie (Bauplan gesperrt)"), "c": GRAY}
            recipe_txt = f"{' + '.join(f'{a} {self._display_name(i)}' for i, a in ins)}  ->  {names[target]}"
            amount_txt = f"{out_amt}" + (f" (netto +{gain})" if self_amt else "")
            rows = [[{"t": recipe_txt, "c": GRAY if group == 3 else None}, amount_txt, status,
                     self._refiner_kind(n_in)]]
            for i, a in ins:
                where = holdings.get(i, [])
                loc = ", ".join(f"{lbl} {amt}" for lbl, amt in where) or "nirgends"
                have = owned.get(i, 0)
                st = {"t": "ok", "c": GREEN} if have >= a else {"t": f"fehlt {a - have} fuer 1x", "c": RED}
                rows.append([{"t": self._display_name(i), "i": i, "ind": 1}, f"{have} / {a}", st, loc])
            cats.setdefault(cat, []).append((group, -runs * out_amt, -share, -yield_per_unit, n_in, rows))

        hide_unusable = self._refine_hide_unusable
        total = sum(len(v) for v in cats.values())
        spec["head"].append((f"{names[target]}: {total} Raffinerie-Rezepte", HEAD))
        first = True
        for cat, hint in self.REFINE_CATEGORIES:
            opts = sorted(cats.get(cat, []), key=lambda o: o[:5])
            if hide_unusable:
                opts = [o for o in opts if o[0] != 3]
            if not opts:
                continue
            n_ok = sum(1 for o in opts if o[0] == 0)
            shown = opts if self._refine_show_all else opts[: self.REFINE_TOP]
            rows = []
            for o in shown:
                rows.extend(o[5])
            if len(opts) > len(shown):
                rows.append([{"t": f"+{len(opts) - len(shown)} weitere (Einstellung 'Alle Raffinerie-Rezepte zeigen')",
                              "c": GRAY}, "", "", ""])
            spec["sections"].append({
                "title": f"{cat} ({len(opts)}, davon {n_ok} machbar)",
                "collapse": True,
                "open": first or n_ok > 0,
                "note": hint,
                "cols": ["Rezept / Zutat", "Menge", "Status", "Raffinerie / Fundort"],
                "rows": rows,
            })
            first = False
        uses = self._refine_uses_section(target, names, holdings, owned, built_max, False)
        if uses["rows"]:
            spec["sections"].append(uses)
        rest = candidates[1:]
        if rest:
            spec["foot"].append("Weitere Treffer (genauer eintippen): " + ", ".join(names[i] for i in rest[:10]))
        return spec

    def _refine_uses_section(self, ing: str, names, holdings, owned, built_max: int, is_open: bool) -> dict:
        """Abschnitt "Was du aus X raffinieren kannst": alle Raffinerie-Rezepte, die X als Zutat nutzen."""
        opts = []
        for out_id, recs in (self._refiner or {}).items():
            for out_amt, ins in recs:
                if not any(i == ing for i, _ in ins):
                    continue
                n_in = len(ins)
                runs = min((owned.get(i, 0) // a for i, a in ins), default=0)
                share = sum(min(1.0, owned.get(i, 0) / a) for i, a in ins) / n_in
                ok_slots = built_max == 0 or n_in <= built_max
                group = 3 if not ok_slots else (0 if runs > 0 else (1 if share > 0 else 2))
                status = ({"t": f"machbar: {runs}x = {runs * out_amt}", "c": GREEN} if group == 0 else
                          {"t": f"teilweise: {round(share * 100)} %", "c": YELLOW} if group == 1 else
                          {"t": "nichts vorhanden", "c": GRAY} if group == 2 else
                          {"t": f"braucht {self._slots_name(n_in)} Raffinerie", "c": GRAY})
                recipe_txt = " + ".join(f"{a} {self._display_name(i)}" for i, a in ins)
                row = [{"t": f"{out_amt} {self._display_name(out_id)}", "i": out_id}, recipe_txt, status,
                       self._refiner_kind(n_in)]
                opts.append((group, n_in, self._display_name(out_id), row))
        opts.sort(key=lambda o: o[:3])
        n_ok = sum(1 for o in opts if o[0] == 0)
        return {"title": f"Was du aus {names.get(ing, ing)} raffinieren kannst ({len(opts)}, davon {n_ok} machbar)",
                "collapse": True, "open": is_open,
                "cols": ["Ergebnis", "Zutaten", "Status", "Raffinerie"], "rows": [o[3] for o in opts]}

    # ------------------------------------------------------------- Kochen

    COOK_TOP_NOW = 40
    COOK_MODES = ("Units (Handel)", "Anomalie (Koch-Bewertung)", "Lebenserhaltung", "Schild / Leben",
                  "Gefahrenschutz", "Ausdauer / Jetpack", "Entdecker-Boni")
    COOK_OVERVIEW_MAX = 60

    # Kochen: Rezepte aus der Raffinerie-Tabelle mit Koch-Flag (Naehrstoffprozessor). Stufe (Tier) =
    # Laenge der Kochkette bis zu Rohzutaten; Herkunft der Rohzutaten: anbaubar (Pflanze im Baumenue,
    # siehe FarmPlants), Sammeln/Tier, Angeln, Sonstiges. Hinweis: FoodBonusStat ist in den aktuellen
    # Spieldaten wenig aussagekraeftig (Effektstaerke ueberall 0), die Zweck-Gruppen sind daher nur grob.
    @staticmethod
    def _effect_group(stat: str) -> str:
        s = (stat or "").lower()
        if "suit_energy" in s:
            return "Lebenserhaltung"
        if "shield" in s or "health" in s or "armour" in s:
            return "Schild / Leben"
        if "protection" in s or "drain" in s:
            return "Gefahrenschutz"
        if "stamina" in s or "jetpack" in s:
            return "Ausdauer / Jetpack"
        if "discovery" in s or "mining" in s or "scan" in s:
            return "Entdecker-Boni"
        return ""

    def _origin(self, cid: str) -> tuple[int, str]:
        """Herkunft einer Rohzutat: 0 anbaubar, 1 Tier/Sammeln, 2 Angeln, 3 Sonstiges.
        anbaubar = Ernte einer Pflanze aus dem Baumenue (Spieldaten, nicht der Untertitel)."""
        if cid in self._farm.harvest:
            return 0, "anbaubar"
        p = (self._cook_props or {}).get(cid, {})
        sub = (p.get("untertitel") or "").lower()
        if p.get("Type") == "Fish" or "fisch" in sub:
            return 2, "Angeln"
        if ("agrar" in sub or p.get("Category") == "Flora" or "rohe zutat" in sub or "köder" in sub
                or "koeder" in sub):
            return 1, "Sammeln/Tier"
        return 3, "Sonstiges"

    def _cook_info(self, cid: str, stack: tuple = ()) -> tuple[int, frozenset]:
        """(Tier, Rohzutaten) ueber das Rezept mit der niedrigsten Stufe. Rohzutat = Tier 1."""
        memo = self._cook_memo
        if cid in memo:
            return memo[cid]
        recs = (self._cooking or {}).get(cid)
        if not recs or cid in stack:
            res = (1, frozenset([cid]))
        else:
            best = None
            for _amt, ins in recs:
                parts = [self._cook_info(i, stack + (cid,)) for i, _ in ins]
                tier = 1 + max((p[0] for p in parts), default=0)
                raws = frozenset().union(*(p[1] for p in parts)) if parts else frozenset()
                if best is None or (tier, len(raws)) < (best[0], len(best[1])):
                    best = (tier, raws)
            res = best
        if not stack:
            memo[cid] = res
        return res

    def _cook_overview(self, holdings, owned) -> dict:
        """Uebersicht aller Gerichte fuer den gewaehlten Zweck: Tier, Wert, Effekt, Herkunft, Aufwand, Status."""
        mode = self._cook_mode
        props = self._cook_props or {}
        rows_data = []
        for dish, recs in (self._cooking or {}).items():
            p = props.get(dish, {})
            effect = self._effect_group(p.get("FoodBonusStat", ""))
            if mode not in ("Units (Handel)", "Anomalie (Koch-Bewertung)") and effect != mode:
                continue
            tier, raws = self._cook_info(dish)
            origins = [self._origin(r) for r in raws]
            hardest = max(origins, default=(3, "Sonstiges"))
            if not self._cook_fish and any(o[0] == 2 for o in origins):
                continue
            effort = (tier - 1) + len(raws) + (3 if hardest[0] == 2 else 0)
            value = int(p.get("BaseValue", 0) or 0)
            cv = float(p.get("CookingValue", 0) or 0)
            best_runs, best_share = 0, 0.0
            for _amt, ins in recs:
                runs = min((owned.get(i, 0) // a for i, a in ins), default=0)
                share = sum(min(1.0, owned.get(i, 0) / a) for i, a in ins) / len(ins)
                best_runs, best_share = max(best_runs, runs), max(best_share, share)
            group = 0 if best_runs > 0 else (1 if best_share > 0 else 2)
            if mode == "Units (Handel)":
                score = value / max(effort, 1)
            elif mode == "Anomalie (Koch-Bewertung)":
                score = cv * 1000 + value / 1000.0 / max(effort, 1)
            else:
                score = -effort
            status = ({"t": f"kochbar: {best_runs}x", "c": GREEN} if group == 0 else
                      {"t": f"teilweise: {round(best_share * 100)} %", "c": YELLOW} if group == 1 else
                      {"t": "fehlt", "c": GRAY})
            herkunft = ", ".join(sorted({o[1] for o in origins}))
            rows_data.append((group, -score, self._display_name(dish), [
                {"t": self._display_name(dish), "i": dish}, str(tier), f"{value:,}".replace(",", "."),
                f"{cv:.2f}", effect or "-", herkunft, str(effort), status]))
        rows_data.sort(key=lambda r: r[:3])
        n_ok = sum(1 for r in rows_data if r[0] == 0)
        spec = {"head": [(f"{mode}: {len(rows_data)} Gerichte, davon {n_ok} jetzt kochbar"
                          f"{'' if self._cook_fish else ' (ohne Fischgerichte)'}", HEAD)],
                "sections": [{"cols": ["Gericht", "Tier", "Wert (Units)", "Koch-Bew.", "Effekt", "Herkunft",
                                       "Aufwand", "Status"],
                              "widths": [3.0, 0.6, 1.2, 0.9, 1.6, 2.0, 0.8, 1.4],
                              "rows": [r[3] for r in rows_data[: self.COOK_OVERVIEW_MAX]]}],
                "foot": []}
        if len(rows_data) > self.COOK_OVERVIEW_MAX:
            spec["foot"].append(f"+{len(rows_data) - self.COOK_OVERVIEW_MAX} weitere (Gericht oder Zutat eintippen "
                                "zum Eingrenzen)")
        spec["foot"].append("Aufwand = Kochschritte + verschiedene Rohzutaten (+3 bei Fisch). "
                            "Koch-Bewertung ist eine Naeherung fuer die Anomalie (Formel unbekannt).")
        return spec


    def _cook_option(self, target: str, out_amt: int, ins, holdings, owned) -> tuple:
        """Ein Kochrezept als Tabellenzeilen + Sortierschluessel (machbar, teilweise, nichts)."""
        n_in = len(ins)
        runs = min((owned.get(i, 0) // a for i, a in ins), default=0)
        share = sum(min(1.0, owned.get(i, 0) / a) for i, a in ins) / n_in if ins else 0.0
        group = 0 if runs > 0 else (1 if share > 0 else 2)
        if group == 0:
            status = {"t": f"kochbar: {runs}x", "c": GREEN}
        elif group == 1:
            status = {"t": f"teilweise: {round(share * 100)} %", "c": YELLOW}
        else:
            status = {"t": "nichts vorhanden", "c": GRAY}
        rows = [[{"t": self._display_name(target), "i": target}, str(out_amt), status,
                 f"{n_in} Zutat{'en' if n_in > 1 else ''}"]]
        for i, a in ins:
            where = holdings.get(i, [])
            loc = ", ".join(f"{lbl} {amt}" for lbl, amt in where) or "nirgends"
            have = owned.get(i, 0)
            if have >= a:
                st = {"t": "ok", "c": GREEN}
            elif self._cooking.get(i):
                st = {"t": f"fehlt {a - have} (selbst kochbar)", "c": YELLOW}
            else:
                st = {"t": f"fehlt {a - have}", "c": RED}
            rows.append([{"t": self._display_name(i), "i": i, "ind": 1}, f"{have} / {a}", st, loc])
        return (group, -runs, -share, n_in, rows)

    def _build_cook_text(self, query: str):
        """Kochrezepte (Naehrstoffprozessor): Suche nach Gericht ODER Zutat; leer = jetzt kochbar. Nur lesend."""
        self._cook_suggest = []
        if self._recipes is None:
            self._recipes = self._load_recipes()
            if self._recipes is None:
                return "Rezepte noch nicht verfuegbar. Lade einen Spielstand."
        cooking = self._cooking or {}
        if self._cook_names is None:
            ids = set(cooking)
            for opts in cooking.values():
                for _amt, ins in opts:
                    ids.update(i for i, _ in ins)
            self._cook_names = {i: self._display_name(i) for i in ids}
        names = self._cook_names
        holdings = self._holdings()
        owned = {k: sum(a for _, a in v) for k, v in holdings.items()}
        cols = ["Gericht / Zutat", "Menge", "Status", "Zutaten / Fundort"]
        spec: dict = {"head": [], "sections": [], "foot": []}

        if not query:
            return self._cook_overview(holdings, owned)
        if False:  # alte "Jetzt kochbar"-Liste (ersetzt durch Uebersicht nach Zweck)
            opts = []
            for target, recs in cooking.items():
                for out_amt, ins in recs:
                    o = self._cook_option(target, out_amt, ins, holdings, owned)
                    if o[0] == 0:
                        opts.append(o)
            opts.sort(key=lambda o: o[:4])
            spec["head"].append((f"Jetzt kochbar: {len(opts)} Rezepte (Gericht oder Zutat eintippen fuer mehr)", HEAD))
            rows = []
            for o in opts[: self.COOK_TOP_NOW]:
                rows.extend(o[4])
            if rows:
                spec["sections"].append({"cols": cols, "rows": rows})
            if len(opts) > self.COOK_TOP_NOW:
                spec["foot"].append(f"+{len(opts) - self.COOK_TOP_NOW} weitere")
            return spec

        # Treffer als Gericht (Ergebnis) und als Zutat
        as_result = [i for i in cooking if self._matches(query, i, names.get(i, i))]
        as_result.sort(key=lambda i: (self._matches(query, i, names.get(i, i)) != 2, names.get(i, i)))
        self._cook_suggest = self._suggest(query, [i for i in names if self._matches(query, i, names[i])], names)
        if not as_result and not any(self._matches(query, i, names[i]) for i in names):
            return f"Kein Kochrezept mit \"{query}\"."
        if as_result:
            target = as_result[0]
            opts = sorted((self._cook_option(target, a, ins, holdings, owned) for a, ins in cooking[target]),
                          key=lambda o: o[:4])
            n_ok = sum(1 for o in opts if o[0] == 0)
            rows = []
            for o in opts:
                rows.extend(o[4])
            spec["sections"].append({"title": f"So kochst du {names.get(target, target)} ({len(opts)} Rezepte, "
                                              f"{n_ok} kochbar)", "collapse": True, "open": True,
                                     "cols": cols, "rows": rows})
        # Wofuer laesst sich der eingegebene Stoff verwenden?
        ing_ids = [i for i in names if self._matches(query, i, names[i]) == 2] or \
                  [i for i in names if self._matches(query, i, names[i])][:1]
        if ing_ids:
            ing = ing_ids[0]
            uses = []
            for target, recs in cooking.items():
                for out_amt, ins in recs:
                    if any(i == ing for i, _ in ins):
                        uses.append(self._cook_option(target, out_amt, ins, holdings, owned))
            uses.sort(key=lambda o: o[:4])
            if uses:
                rows = []
                for o in uses[: self.COOK_TOP_NOW]:
                    rows.extend(o[4])
                spec["sections"].append({"title": f"Was du mit {names.get(ing, ing)} kochen kannst ({len(uses)})",
                                         "collapse": True, "open": not as_result, "cols": cols, "rows": rows})
        if len(as_result) > 1:
            spec["foot"].append("Weitere Gerichte (genauer eintippen): " +
                                ", ".join(names.get(i, i) for i in as_result[1:11]))
        return spec

    # ----------------------------------------------------------- Ergebnis

    def _build_result_text(self) -> dict:
        """Trefferliste als Tabelle (GUI-Thread, nur Schnappschuss-Daten)."""
        with self._lock:
            snapshot = list(self._snapshot)
        if not snapshot:
            return {"msg": "Noch keine Daten. Lade einen Spielstand."}
        query = self._search.strip().lower()
        if not query:
            return {"msg": "Suchbegriff eingeben (deutsch oder englisch). Gross/Kleinschreibung egal."}
        hits = []
        for label, items in snapshot:
            for raw_id, name, amount, max_amount, typ, x, y in items:
                if typ == TECH and not self._show_tech:
                    continue
                cid = _clean_id(raw_id)
                if self._matches(query, cid, name):
                    hits.append((name, label, amount, max_amount, typ, x, y, cid))
        if not hits:
            return {"msg": f"Nichts gefunden fuer \"{self._search}\"."}
        hits.sort(key=lambda h: (h[0], h[1], h[6], h[5]))
        totals: dict[str, int] = {}
        for h in hits:
            totals[h[0]] = totals.get(h[0], 0) + h[2]
        fetch = self._fetch_enabled
        refs = getattr(self, "_store_refs", {})
        rows = []
        for name, label, amount, max_amount, typ, x, y, cid in hits[:MAX_RESULT_LINES]:
            row = [{"t": name, "i": cid}, str(amount), str(max_amount), label, f"{x + 1} / {y + 1}"]
            if fetch:
                can = typ != TECH and amount > 0 and label in refs and refs[label] != ("inv", 0)
                row.append({"btn": "holen", "act": (label, cid, x, y)} if can else "")
            rows.append(row)
        head = [(", ".join(f"{n}: {t}" for n, t in sorted(totals.items())[:12]), HEAD)]
        if fetch and self._fetch_msg:
            head.append(self._fetch_msg)
        cols = ["Gegenstand", "Menge", "Max", "Inventar", "Spalte / Zeile"] + (["Aktion"] if fetch else [])
        spec = {"head": head, "sections": [{"cols": cols, "rows": rows}], "foot": []}
        if len(hits) > MAX_RESULT_LINES:
            spec["foot"].append("... gekuerzt, bitte Suche genauer machen")
        return spec
