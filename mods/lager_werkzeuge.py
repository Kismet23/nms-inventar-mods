# Lager-Werkzeuge (v1.5) fuer No Man's Sky (NMS.py / pyMHF)
# Autor: Kismet23
#
# Ueberblick
# ---------
# Die Mod haengt sich ueber NMS.py (pyMHF) in das laufende Spiel ein und arbeitet nur im Arbeitsspeicher.
# Spieldateien werden nie veraendert. Inventare werden ueber die spieleigenen Funktionen
# cGcInventoryStore.Add/Remove geaendert und danach nachgezaehlt; stimmt die Gesamtmenge ueber
# Exoanzug + alle zehn Lager nicht, steht ein FEHLER im Log (dann nicht speichern, Sicherung zurueckspielen).
#
# Funktionen
# ----------
# Im Lager-Bildschirm (Lagercontainer geoeffnet, Exoanzug links, Container rechts):
#   - Umschalten: rechts jeden der zehn Lagercontainer anzeigen und benutzen, egal welcher geoeffnet ist.
#     Das Spiel verschiebt Items dabei wirklich in den angezeigten Container.
#     Tastatur Bild-auf / Bild-ab, Controller Back/Ansicht halten + LB / RB.
#   - Sortieren: das angezeigte Lager sortieren (Rohstoffe, Produkte, Technik; darin nach deutschem Namen)
#     und halbe Stapel desselben Items zusammenlegen. Tastatur Pos1, Controller Back + Y.
#   - Schnell einlagern: alles aus dem Exoanzug, was es im angezeigten Lager schon gibt, dorthin legen.
#     Tastatur Ende, Controller Back + X.
#
# Ueberall (oder optional nur in Lagernaehe, siehe unten):
#   - Packen (F8 / Controller Back + Steuerkreuz unten): Exoanzug fuer die Packliste vorbereiten.
#     Erst alles, was nicht gebraucht wird, einlagern (zuerst in Lager, die das Item schon haben, sonst ins
#     erste Lager mit freiem Platz), dann fehlende Mengen aus den Lagern holen.
#     Packliste z. B. "Teleporter x2; Zutaten: Ionenakku x5; Projektilmunition x500":
#     Bauteile (Basis) -> deren Baukosten, "Zutaten: X" -> Zutaten von X, sonst das Item selbst.
#   - Auffuellen (F9 / Controller Back + Steuerkreuz oben), Liste z. B. "Ionenakku x200":
#     erst fertige Items aus den Lagern bis zur Zielmenge holen; fehlt dann noch etwas, die Zutaten fuer
#     den Rest holen (so viel, wie die Lager hergeben), damit man sie selbst herstellen kann.
#   - "Im Exoanzug behalten": Items, die beim Einlagern im Anzug bleiben. "Sauerstoff x250" behaelt 250,
#     ohne Menge bleibt genau ein voller Stapel. Vom Ueberschuss werden zuerst die kleinen Stapel eingelagert.
#   - Nach Packen, Alles einlagern und Auffuellen werden die beteiligten Lager automatisch sortiert
#     (abschaltbar).
#   - "F8/F9 nur in Lagernaehe": die Tasten wirken nur, solange ein Lager-Bildschirm offen ist oder in den
#     letzten N Minuten offen war und man noch am selben Ort (Planet/System) ist. Ausgeschaltet wirken sie
#     ueberall. Die Knoepfe im Mod-Reiter wirken immer.
#
# Alle Tasten und Listen sind im Reiter "LagerWerkzeuge" des pyMHF-Fensters einstellbar.
# Neue Einstellungsfelder brauchen einen Neustart des Spiels, Code-Aenderungen gehen mit "Reload Mod".
# Meldungen stehen zusaetzlich in logs\lager_werkzeuge.txt (eine Ebene ueber dem mods-Ordner).
#
# Versionen: v1.0 Umschalten + Sortieren, v1.1 Einlagern + Packen, v1.2 Auffuellen,
#            v1.3 Behalten mit Mengen, v1.4 Sortieren nach Packen, v1.5 Lagernaehe.
#
# Grenzen: Die Lager-Bezeichnung oben wechselt beim Umschalten nicht mit (das Spiel liest sie nur beim
# Oeffnen). Weiter entfernte Lager kann das Spiel selbst nicht anzeigen; die Mod umgeht das nur im
# Lager-Bildschirm, weil die Lager-Inventare des Spielers immer geladen sind.
# Getestet gegen: No Man's Sky Build 184271, nmspy 184271.0 / pymhf 0.2.5, Python 3.13 (Windows).

import ctypes
import os
import time
import traceback
from logging import getLogger

from pymhf import Mod
from pymhf.core.memutils import get_addressof
from pymhf.gui.decorators import BOOLEAN, STRING, gui_button

import nmspy.data.enums as enums
import nmspy.data.exported_types as nmse
import nmspy.data.types as nms
from nmspy.common import gameData
from nmspy.decorators import main_loop

logger = getLogger("LagerWerkzeuge")

# Meldungen zusaetzlich in eine Textdatei schreiben: <Mod-Ordner>\..\logs\lager_werkzeuge.txt
_LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "lager_werkzeuge.txt")


class _FileLog:
    """Huelle um den pyMHF-Logger: jede Meldung geht auch in die Log-Datei (Fehler beim Schreiben werden ignoriert)."""

    def __init__(self, inner):
        self._inner = inner

    def _file(self, level, msg):
        try:
            os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {level} {msg}\n")
        except Exception:
            pass

    def info(self, msg, *a, **k):
        self._inner.info(msg, *a, **k)
        self._file("", msg)

    def error(self, msg, *a, **k):
        self._inner.error(msg, *a, **k)
        self._file("FEHLER", msg)


logger = _FileLog(logger)

# Inventar-Nummern im Spielerzustand (cGcPlayerState.mInventories):
#   0 = Exoanzug, 1 = Exoanzug-Technik, 12..21 = Lagercontainer 0..9
CONTAINERS = range(12, 22)
# Spielseite "Inventar-Uebertragung", die beim Oeffnen eines Lagercontainers aktiv ist
TRANSFER_PAGE = int(enums.eFrontendPage.InventoryTransferScreen)
# Sortierreihenfolge nach Item-Typ (Element.Type: 0 Substanz, 1 Technik, 2 Produkt)
TYPE_ORDER = {0: 0, 2: 1, 1: 2}  # Substanz, Produkt, Technik

# XInput-Tastenbits (Controller): Back/Ansicht, LB, RB, Y, X, Steuerkreuz unten/oben
XI_BACK, XI_LB, XI_RB, XI_Y, XI_X, XI_DDOWN, XI_DUP = 0x0020, 0x0100, 0x0200, 0x8000, 0x4000, 0x0002, 0x0001

# Tastennamen, die in den Einstellungen erlaubt sind -> Windows Virtual-Key-Codes
KEY_NAMES = {
    "PGUP": 0x21, "BILDAUF": 0x21, "PGDN": 0x22, "BILDAB": 0x22, "END": 0x23, "ENDE": 0x23,
    "HOME": 0x24, "POS1": 0x24, "INS": 0x2D, "EINFG": 0x2D, "DEL": 0x2E, "ENTF": 0x2E,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "NUM+": 0x6B, "NUM-": 0x6D, "NUM*": 0x6A, "NUM/": 0x6F,
}
KEY_NAMES.update({f"F{i}": 0x6F + i for i in range(1, 13)})
KEY_NAMES.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
KEY_NAMES.update({str(d): 0x30 + d for d in range(10)})
KEY_NAMES.update({f"NUM{d}": 0x60 + d for d in range(10)})


def _as_int(v) -> int:
    """ctypes-Werte und Enums (mit .value) einheitlich in int umwandeln."""
    return int(getattr(v, "value", v))


def _clean_id(raw: str) -> str:
    """Item-ID aus dem Spiel saeubern: Nullbytes, fuehrendes '^' und Varianten-Anhang '#...' entfernen."""
    s = raw.strip("\x00").strip()
    if s.startswith("^"):
        s = s[1:]
    if "#" in s:
        s = s.split("#", 1)[0]
    return s


def _fix_text(s: str) -> str:
    """Uebersetzte Texte kommen als UTF-8-Bytes, die als Latin-1 gelesen wurden; hier zurueckwandeln (Umlaute)."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _addr(p) -> int:
    """Speicheradresse eines ctypes-Zeigers/-Objekts, 0 wenn nicht ermittelbar."""
    try:
        return get_addressof(p)
    except Exception:
        try:
            return ctypes.cast(p, ctypes.c_void_p).value or 0
        except Exception:
            return 0


# Strukturen fuer XInputGetState (Windows-Controller-API)
class _XGamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _XState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", _XGamepad)]


def _load_xinput():
    """Die erste verfuegbare XInput-DLL laden (None, wenn keine da ist)."""
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except Exception:
            continue
    return None


_XINPUT = _load_xinput()
_USER32 = ctypes.windll.user32


def _key_down(vk) -> bool:
    """True, solange die Taste (Virtual-Key-Code) gerade gedrueckt ist."""
    if not vk:
        return False
    try:
        return bool(_USER32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def _pad_buttons() -> int:
    """Tastenbits des ersten angeschlossenen Controllers (0, wenn keiner da ist)."""
    if _XINPUT is None:
        return 0
    st = _XState()
    for i in range(4):
        try:
            if _XINPUT.XInputGetState(i, ctypes.byref(st)) == 0:
                return int(st.Gamepad.wButtons)
        except Exception:
            return 0
    return 0


class LagerWerkzeuge(Mod):
    """Die Mod selbst. pyMHF legt fuer sie einen Reiter mit den Einstellungen und Knoepfen unten an."""

    __author__ = "Kismet23"
    __description__ = "Lager-Bildschirm: alle zehn Lager umschalten, sortieren und Stapel zusammenlegen"
    __version__ = "1.5"

    def __init__(self):
        super().__init__()
        logger.info(f"=== Lager-Werkzeuge {self.__version__} gestartet ===")
        # Schalter und Texte aus den Einstellungen (Standardwerte)
        self._switch_on = True
        self._sort_on = True
        self._sort_after_on = True
        self._touched = set()      # Lager 0..9, die der letzte Auftrag veraendert hat (zum Nachsortieren)
        self._pad_on = True
        self._keys_text = "PGUP, PGDN, HOME"
        self._keys = (0x21, 0x22, 0x24)
        self._target = None        # angezeigtes Lager 0..9 (None = das geoeffnete)
        self._opened = None        # geoeffnetes Lager 0..9
        self._page = None          # zuletzt aktivierte Spielseite (eFrontendPage)
        self._last_slots = 0.0     # Zeitpunkt, zu dem der Lager-Bildschirm zuletzt gezeichnet wurde (0 = zu)
        self._prev = (False, False, False)  # vorheriger Zustand der Tasten zurueck/vor/sortieren
        self._near_on = True
        self._near_min_text = "5"
        self._near_time = 0.0      # wann zuletzt ein Lager-Bildschirm offen war
        self._near_loc = None      # Ort (Universumsadresse) zu diesem Zeitpunkt
        self._sort_request = None  # Lager-Nr., das im naechsten Spieltakt sortiert werden soll
        self._names = None         # Item-ID -> deutscher Name
        self._errors = 0           # Anzahl geloggter Fehler (begrenzt, damit das Log nicht volllaeuft)
        self._status = "bereit"
        self._pack_text = "Teleporter x1; Zutaten: Ionenakku x5; Projektilmunition x500"
        self._never_text = ("Ionenakku x200; Natrium; Natriumnitrat; Sauerstoff; Lebenserhaltungsgel x200; "
                            "Raumschiffstart-Treibstoff; Warp-Zelle")
        self._pack_key_text = "F8"
        self._pack_key = 0x77
        self._dep_key_text = "END"
        self._dep_key = 0x23
        self._prev_pack = False    # vorherige Tastenzustaende, damit nur der Druck zaehlt (nicht das Halten)
        self._refill_text = "Ionenakku x200; Lebenserhaltungsgel x200"
        self._refill_key_text = "F9"
        self._refill_key = 0x78
        self._prev_refill = False
        self._prev_dep = False
        # Auftraege werden nur vorgemerkt und im Spieltakt (on_main_loop) ausgefuehrt
        self._job = None           # ("packen"|"einlagern"|"alles"|"pruefen"|"auffuellen", Lager-Nr.)
        self._db = None            # Item-ID -> (Art, deutscher Name, [(Zutat, Menge)])

    # ---------------------------------------------------------------- Einstellungen
    # Jede Einstellung ist eine Property mit pyMHF-Dekorator (BOOLEAN = Haken, STRING = Textfeld).
    # Die Setter rechnen Tastennamen gleich in Tastencodes um.

    @property
    @BOOLEAN("Umschalten zwischen den Lagern")
    def switch_on(self) -> bool:
        return self._switch_on

    @switch_on.setter
    def switch_on(self, value: bool):
        self._switch_on = bool(value)
        if not value:
            self._target = None

    @property
    @BOOLEAN("Sortieren per Taste")
    def sort_on(self) -> bool:
        return self._sort_on

    @sort_on.setter
    def sort_on(self, value: bool):
        self._sort_on = bool(value)

    @property
    @BOOLEAN("Lager nach Packen/Einlagern/Auffuellen sortieren")
    def sort_after_on(self) -> bool:
        return self._sort_after_on

    @sort_after_on.setter
    def sort_after_on(self, value: bool):
        self._sort_after_on = bool(value)

    @property
    @BOOLEAN("Controller (Back + LB / RB / Y)")
    def pad_on(self) -> bool:
        return self._pad_on

    @pad_on.setter
    def pad_on(self, value: bool):
        self._pad_on = bool(value)

    @property
    @STRING("Tasten: zurueck, vor, sortieren (z.B. PGUP, PGDN, HOME)")
    def keys_text(self) -> str:
        return self._keys_text

    @keys_text.setter
    def keys_text(self, value: str):
        self._keys_text = str(value)
        parts = [p.strip().upper() for p in str(value).replace(";", ",").split(",")]
        codes = []
        for p in parts[:3]:
            codes.append(KEY_NAMES.get(p, 0))
        while len(codes) < 3:
            codes.append(0)
        self._keys = tuple(codes)
        unknown = [p for p in parts[:3] if p and p not in KEY_NAMES]
        if unknown:
            logger.info(f"Unbekannte Taste(n): {', '.join(unknown)}. Erlaubt z.B. PGUP, PGDN, HOME, END, F5, A-Z, 0-9, NUM+")

    @property
    @STRING("Packliste (Name xAnzahl; ... / Zutaten: Name xAnzahl)")
    def pack_text(self) -> str:
        return self._pack_text

    @pack_text.setter
    def pack_text(self, value: str):
        self._pack_text = str(value)

    @property
    @STRING("Im Exoanzug behalten (Name xMenge; ohne Menge = 1 Stapel)")
    def never_text(self) -> str:
        return self._never_text

    @never_text.setter
    def never_text(self, value: str):
        self._never_text = str(value)

    @property
    @STRING("Taste Packen (ueberall)")
    def pack_key_text(self) -> str:
        return self._pack_key_text

    @pack_key_text.setter
    def pack_key_text(self, value: str):
        self._pack_key_text = str(value)
        self._pack_key = KEY_NAMES.get(str(value).strip().upper(), 0)

    @property
    @STRING("Taste Schnell einlagern (Lager-Bildschirm)")
    def dep_key_text(self) -> str:
        return self._dep_key_text

    @dep_key_text.setter
    def dep_key_text(self, value: str):
        self._dep_key_text = str(value)
        self._dep_key = KEY_NAMES.get(str(value).strip().upper(), 0)

    @property
    @STRING("Auffuellen (Name xZielmenge; ...)")
    def refill_text(self) -> str:
        return self._refill_text

    @refill_text.setter
    def refill_text(self, value: str):
        self._refill_text = str(value)

    @property
    @BOOLEAN("F8/F9 nur in Lagernaehe (sonst ueberall)")
    def near_on(self) -> bool:
        return self._near_on

    @near_on.setter
    def near_on(self, value: bool):
        self._near_on = bool(value)

    @property
    @STRING("Lagernaehe: Minuten nach Lager-Bildschirm")
    def near_min_text(self) -> str:
        return self._near_min_text

    @near_min_text.setter
    def near_min_text(self, value: str):
        self._near_min_text = str(value)

    def _location(self):
        """Aktuelle Universumsadresse des Spielers als Bytes (zum Vergleichen, ob man noch am selben Ort ist)."""
        try:
            return bytes(gameData.player_state.mLocation)
        except Exception:
            return None

    def _near_ok(self, what):
        """Pruefen, ob F8/F9 gerade erlaubt sind (Einstellung "nur in Lagernaehe"); sonst Grund ins Log."""
        if not self._near_on:
            return True
        try:
            minutes = float(self._near_min_text.strip().replace(",", "."))
        except ValueError:
            minutes = 5.0
        if self._last_slots:
            return True
        if not self._near_time:
            logger.info(f"{what}: nur in Lagernaehe, bitte zuerst ein Lager oeffnen")
            return False
        age = time.time() - self._near_time
        if age > minutes * 60:
            logger.info(f"{what}: letztes Lager ist {age / 60:.0f} Minuten her, bitte ein Lager oeffnen")
            return False
        if self._near_loc is not None and self._location() != self._near_loc:
            logger.info(f"{what}: du bist nicht mehr am Ort des letzten Lagers, bitte ein Lager oeffnen")
            return False
        return True

    @property
    @STRING("Taste Auffuellen (ueberall)")
    def refill_key_text(self) -> str:
        return self._refill_key_text

    @refill_key_text.setter
    def refill_key_text(self, value: str):
        self._refill_key_text = str(value)
        self._refill_key = KEY_NAMES.get(str(value).strip().upper(), 0)

    # Knoepfe im Mod-Reiter: sie merken nur einen Auftrag vor, ausgefuehrt wird im Spieltakt.
    @gui_button("Jetzt auffuellen")
    def b_refill(self):
        self._job = ("auffuellen", None)

    @gui_button("Packliste pruefen (schreibt ins Log, aendert nichts)")
    def b_check(self):
        self._job = ("pruefen", None)

    @gui_button("Jetzt packen")
    def b_pack(self):
        self._job = ("packen", None)

    @gui_button("Alles einlagern (ohne Packliste)")
    def b_all(self):
        self._job = ("alles", None)

    @gui_button("Angezeigtes Lager sortieren (Lager-Bildschirm offen)")
    def b_sort(self):
        shown = self._shown()
        if shown is None:
            logger.info("Sortieren: kein Lager-Bildschirm offen")
            return
        self._sort_request = shown

    # ---------------------------------------------------------------- Hilfen

    def _err(self, where: str):
        """Fehler mit Stacktrace loggen (hoechstens 10, danach still)."""
        if self._errors < 10:
            self._errors += 1
            logger.error(f"Fehler {where}:\n" + traceback.format_exc())

    def _shown(self):
        """Nummer (0..9) des rechts angezeigten Lagers, None wenn kein Lager-Bildschirm offen ist."""
        return self._target if self._target is not None else self._opened

    def _container_index(self, p):
        """Inventar-Zeiger -> Nummer in mInventories (12..21), wenn er auf einen Lagercontainer zeigt."""
        a = _addr(p)
        if not a:
            return None
        ps = gameData.player_state
        for i in CONTAINERS:
            if ctypes.addressof(ps.mInventories[i]) == a:
                return i
        return None

    def _summary(self, store) -> dict:
        """Inhalt eines Inventars als {Item-ID: Gesamtmenge}."""
        out = {}
        vec = store.mStore
        for k in range(len(vec)):
            amt = _as_int(vec[k].Amount)
            if amt > 0:
                cid = _clean_id(str(vec[k].Id))
                out[cid] = out.get(cid, 0) + amt
        return out

    def _name(self, cid: str) -> str:
        """Deutscher Name eines Items fuer die Sortierung (faellt auf die ID zurueck)."""
        if self._names is None:
            self._names = self._load_names()
        return (self._names or {}).get(cid, cid)

    def _load_names(self):
        """Namen aus den Spieltabellen (Substanzen, Produkte, Technik) holen und uebersetzen.
        Gibt None zurueck, solange das Spiel die Tabellen noch nicht geladen hat (z. B. im Hauptmenue)."""
        names = {}
        try:
            rm = gameData.data.mRealityManager
            for ptr in (rm.mpSubstanceTable, rm.mpProductTable, rm.mpTechnologyTable):
                if not ptr:
                    continue
                for entry in ptr.contents.Table:
                    try:
                        cid = _clean_id(str(entry.ID))
                        key = str(entry.NameLower).strip("\x00").strip() or str(entry.Name).strip("\x00").strip()
                        if cid and key and cid not in names:
                            txt = nms.cTkLanguageManager.Translate(key)
                            names[cid] = _fix_text(txt) if txt else cid
                    except Exception:
                        continue
        except Exception:
            self._err("Namen laden")
        if len(names) < 100:
            return None  # Spieldaten noch nicht geladen -> spaeter erneut
        logger.info(f"Namen fuer die Sortierung geladen: {len(names)}")
        return names

    # ---------------------------------------------------------------- Spiel-Hooks
    # Activate/SetPage: merken, welche Spielseite gerade offen ist.
    # DoInventorySlots: zeichnet ein Inventar im Bildschirm; hier wird das angezeigte Lager ausgetauscht.

    @nms.cGcFrontendManager.Activate.after
    def after_activate(self, this, lePage, *args):
        try:
            self._page = _as_int(lePage)
        except Exception:
            self._err("Activate")

    @nms.cGcFrontendPage.SetPage.after
    def after_set_page(self, this, lePage, *args):
        try:
            self._page = _as_int(lePage)
        except Exception:
            self._err("SetPage")

    @nms.cGcFrontendPageFunctions.DoInventorySlots.before
    def before_inventory_slots(self, lpPage, lpLayer, lpInventory, *rest):
        """Laeuft, bevor das Spiel ein Inventar zeichnet. Ist es ein Lagercontainer im Lager-Bildschirm und
        wurde umgeschaltet, wird der Zeiger durch das gewaehlte Lager ersetzt (pyMHF: ein zurueckgegebenes
        Tupel ersetzt die Argumente). Das Spiel zeichnet und befuellt dann das gewaehlte Lager."""
        try:
            if self._page != TRANSFER_PAGE:
                return None
            idx = self._container_index(lpInventory)
            if idx is None:
                return None
            if not self._last_slots:   # Bildschirm frisch geoeffnet
                self._target = None
            self._opened = idx - 12
            self._last_slots = time.time()
            self._near_time = self._last_slots
            self._near_loc = self._location()
            self._poll_keys()
            if self._target is None or not self._switch_on:
                return None
            want = 12 + self._target
            if idx == want:
                return None
            new_ptr = ctypes.cast(ctypes.addressof(gameData.player_state.mInventories[want]), type(lpInventory))
            return (lpPage, lpLayer, new_ptr, *rest)
        except Exception:
            self._err("DoInventorySlots")
            return None

    def _poll_keys(self):
        """Tasten im Lager-Bildschirm abfragen: umschalten, schnell einlagern, sortieren.
        Ausgeloest wird nur beim Druecken (Wechsel von los auf gedrueckt)."""
        btn = _pad_buttons() if self._pad_on else 0
        back = bool(btn & XI_BACK)
        k_prev, k_next, k_sort = self._keys
        prev_d = _key_down(k_prev) or (back and bool(btn & XI_LB))
        next_d = _key_down(k_next) or (back and bool(btn & XI_RB))
        sort_d = _key_down(k_sort) or (back and bool(btn & XI_Y))
        was_prev, was_next, was_sort = self._prev
        self._prev = (prev_d, next_d, sort_d)
        if self._switch_on and ((prev_d and not was_prev) or (next_d and not was_next)):
            step = -1 if (prev_d and not was_prev) else 1
            cur = self._shown() or 0
            new = (cur + step) % 10
            self._target = None if new == self._opened else new
            logger.info(f"Zeige Lager {new}")
        dep_d = _key_down(self._dep_key) or (back and bool(btn & XI_X))
        if dep_d and not self._prev_dep and self._shown() is not None:
            self._job = ("einlagern", self._shown())
        self._prev_dep = dep_d
        if self._sort_on and sort_d and not was_sort:
            shown = self._shown()
            if shown is not None:
                self._sort_request = shown

    def _game_focused(self) -> bool:
        """True, wenn das Spielfenster im Vordergrund ist (damit F8/F9 in anderen Programmen nichts tun)."""
        try:
            hwnd = _USER32.GetForegroundWindow()
            pid = ctypes.c_ulong(0)
            _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value == os.getpid()
        except Exception:
            return False

    @main_loop.after
    def on_main_loop(self):
        """Laeuft einmal pro Spieltakt im Spiel-Thread. Hier werden F8/F9 abgefragt und alle vorgemerkten
        Auftraege ausgefuehrt; Inventare werden so nie gleichzeitig mit dem Spiel veraendert."""
        try:
            btn = _pad_buttons() if self._pad_on else 0
            pack_d = (_key_down(self._pack_key) or (bool(btn & XI_BACK) and bool(btn & XI_DDOWN)))
            if pack_d and not self._prev_pack:
                if self._game_focused():
                    if self._near_ok("Packen"):
                        self._job = ("packen", None)
                else:
                    logger.info("Packen-Taste erkannt, aber das Spielfenster hat nicht den Fokus")
            self._prev_pack = pack_d
            refill_d = (_key_down(self._refill_key) or (bool(btn & XI_BACK) and bool(btn & XI_DUP)))
            if refill_d and not self._prev_refill:
                if self._game_focused():
                    if self._near_ok("Auffuellen"):
                        self._job = ("auffuellen", None)
                else:
                    logger.info("Auffuellen-Taste erkannt, aber das Spielfenster hat nicht den Fokus")
            self._prev_refill = refill_d
        except Exception:
            self._err("Packen-Taste")
        if self._job is not None:
            job, self._job = self._job, None
            logger.info(f"Auftrag: {job[0]}")
            try:
                self._run_job(*job)
            except Exception:
                self._err(f"Auftrag {job[0]}")
        if self._sort_request is not None:
            n, self._sort_request = self._sort_request, None
            try:
                self._sort_store(n)
            except Exception:
                self._err("Sortieren")
        if self._last_slots and time.time() - self._last_slots > 0.5:
            # Lager-Bildschirm geschlossen
            self._last_slots = 0.0
            self._target = None
            self._opened = None

    # ---------------------------------------------------------------- Packen / Einlagern / Auffuellen

    def _load_db(self):
        """Item-Datenbank aus den Spieltabellen: {ID: (Art, deutscher Name, [(Zutat-ID, Menge)])}.
        Art: "sub" Substanz, "prod" Produkt, "bau" Basis-Bauteil. Zutaten = Herstellungs- bzw. Baukosten."""
        db = {}
        try:
            rm = gameData.data.mRealityManager
            for ptr, kind in ((rm.mpSubstanceTable, "sub"), (rm.mpProductTable, "prod"),
                              (rm.mpBasePartProductTable, "bau")):
                if not ptr:
                    continue
                for entry in ptr.contents.Table:
                    try:
                        cid = _clean_id(str(entry.ID))
                        if not cid or cid in db:
                            continue
                        key = str(entry.Name).strip("\x00").strip()
                        txt = nms.cTkLanguageManager.Translate(key) if key else ""
                        name = _fix_text(txt).strip() if txt else cid
                        reqs = []
                        if kind != "sub":
                            for r in entry.Requirements:
                                rid = _clean_id(str(r.ID))
                                if rid:
                                    reqs.append((rid, _as_int(r.Amount)))
                        db[cid] = (kind, name, reqs)
                    except Exception:
                        continue
        except Exception:
            self._err("Spieldaten")
        if len(db) < 100:
            return None
        logger.info(f"Packen: {len(db)} Items/Bauteile geladen")
        return db

    def _dname(self, cid):
        """Deutscher Name aus der Item-Datenbank (sonst die ID)."""
        return self._db[cid][1] if self._db and cid in self._db else cid

    def _lookup(self, text):
        """Name (oder ID) -> Liste passender IDs. Exakter Name zuerst, sonst Teiltreffer.
        Gross-/Kleinschreibung, Bindestriche und Leerzeichen werden ignoriert."""
        def norm(x):
            return x.lower().replace("-", "").replace(" ", "")

        t = norm(text.strip())
        if not t:
            return []
        up = text.strip().upper()
        if up in self._db:
            return [up]
        exact = [c for c, (_k, n, _r) in self._db.items() if norm(n) == t]
        if exact:
            # Bauteil bevorzugen, wenn es denselben Namen als Item und Bauteil gibt
            exact.sort(key=lambda c: 0 if self._db[c][0] == "bau" else 1)
            return exact[:1] if len({self._db[c][1].lower() for c in exact}) == 1 else exact
        return [c for c, (_k, n, _r) in self._db.items() if t in norm(n)]

    def _parse_pack(self, log=True):
        """Packliste -> {Item-ID: Menge}, plus Liste von Problemen.
        Eintraege: "Name xAnzahl", "Zutaten: Name xAnzahl"; Bauteile werden immer in Baukosten aufgeloest."""
        need, problems = {}, []
        for raw in self._pack_text.replace("\n", ";").split(";"):
            part = raw.strip()
            if not part:
                continue
            ingredients = False
            if part.lower().startswith("zutaten:"):
                ingredients, part = True, part[8:].strip()
            count = 1
            low = part.lower()
            pos = low.rfind(" x")
            if pos > 0 and low[pos + 2:].strip().isdigit():
                count = int(low[pos + 2:].strip())
                part = part[:pos].strip()
            hits = self._lookup(part)
            if not hits:
                problems.append(f"'{part}' nicht gefunden")
                continue
            if len(hits) > 1:
                names = ", ".join(sorted({self._dname(c) for c in hits})[:8])
                problems.append(f"'{part}' ist nicht eindeutig: {names}")
                continue
            cid = hits[0]
            kind, name, reqs = self._db[cid]
            if kind == "bau" or ingredients:
                if not reqs:
                    problems.append(f"'{name}' hat keine Zutaten")
                    continue
                for rid, amt in reqs:
                    need[rid] = need.get(rid, 0) + amt * count
                if log:
                    zut = ", ".join(f"{amt * count} {self._dname(rid)}" for rid, amt in reqs)
                    logger.info(f"Packliste: {count}x {name} -> {zut}")
            else:
                need[cid] = need.get(cid, 0) + count
                if log:
                    logger.info(f"Packliste: {count} {name}")
        return need, problems

    def _never(self):
        """Behalten-Liste -> {Item-ID: Menge oder None (= ein voller Stapel)}."""
        out = {}
        for raw in self._never_text.replace("\n", ";").split(";"):
            part = raw.strip()
            if not part:
                continue
            count = None
            low = part.lower()
            pos = low.rfind(" x")
            if pos > 0 and low[pos + 2:].strip().isdigit():
                count = int(low[pos + 2:].strip())
                part = part[:pos].strip()
            hits = self._lookup(part)
            if not hits:
                logger.info(f"Behalten: '{part}' nicht gefunden")
            for cid in hits:
                out[cid] = count
        return out

    @staticmethod
    def _stack_max(store, cid):
        """Groesste Stapelgroesse dieses Items im Inventar (= ein voller Stapel)."""
        vec = store.mStore
        return max([_as_int(vec[k].MaxAmount) for k in range(len(vec))
                    if _as_int(vec[k].Amount) > 0 and _clean_id(str(vec[k].Id)) == cid] or [0])

    @staticmethod
    def _count(store, cid):
        """Gesamtmenge eines Items im Inventar."""
        vec = store.mStore
        return sum(_as_int(vec[k].Amount) for k in range(len(vec))
                   if _as_int(vec[k].Amount) > 0 and _clean_id(str(vec[k].Id)) == cid)

    def _stacks(self, store):
        """Alle Stapel ausser Technik als Liste (ID, X, Y, Menge)."""
        vec = store.mStore
        out = []
        for k in range(len(vec)):
            e = vec[k]
            amt = _as_int(e.Amount)
            if amt > 0 and _as_int(e.Type) != 1:
                out.append((_clean_id(str(e.Id)), _as_int(e.Index.X), _as_int(e.Index.Y), amt))
        return out

    def _move(self, src, x, y, cid, amount, dst):
        """Bis zu 'amount' vom Stapel (x, y) in src nach dst verschieben. Gibt die verschobene Menge zurueck.
        Ablauf: Kopie des Stapels mit der Wunschmenge per Add ins Ziel legen, nachzaehlen, wie viel wirklich
        angekommen ist (Ziel evtl. voll), und genau diese Menge per Remove aus der Quelle nehmen."""
        el = self._find(src, x, y, cid)
        if el is None or amount <= 0:
            return 0
        amount = min(amount, _as_int(el.Amount))
        item = type(el).from_buffer_copy(el)
        item.Amount = amount
        before_dst = self._count(dst, cid)
        res = nmse.cGcInventoryIndex()
        dst.Add(ctypes.byref(res), ctypes.byref(item))
        moved = self._count(dst, cid) - before_dst
        if moved <= 0:
            return 0
        before_src = self._count(src, cid)
        idx = nmse.cGcInventoryIndex()
        idx.X, idx.Y = x, y
        src.Remove(idx, moved)
        removed = before_src - self._count(src, cid)
        if removed != moved:
            logger.error(f"Packen: {cid} Ziel +{moved}, Quelle -{removed} (Abweichung!)")
        return moved

    def _all_totals(self):
        """Gesamtmengen ueber Exoanzug + alle zehn Lager (fuer die Kontrolle vor/nach einem Auftrag)."""
        ps = gameData.player_state
        tot = {}
        for i in [0, *CONTAINERS]:
            for cid, amt in self._summary(ps.mInventories[i]).items():
                tot[cid] = tot.get(cid, 0) + amt
        return tot

    def _deposit(self, keep, never, only_into=None, allow_new=True):
        """Exoanzug-Stapel einlagern. Gibt (eingelagert, nicht untergebracht) als {ID: Menge} zurueck.
        keep: IDs, die ganz im Anzug bleiben (Packliste); never: Behalten-Liste {ID: Menge/None};
        only_into: nur in dieses Lager und nur Items, die es dort schon gibt (Schnell einlagern);
        allow_new: auch in Lager legen, die das Item noch nicht haben."""
        ps = gameData.player_state
        exo = ps.mInventories[0]
        stored, left = {}, {}
        # Behalten-Liste: nur den Ueberschuss einlagern, kleine Stapel zuerst
        excess = {}
        for cid, limit in never.items():
            if limit is None:
                limit = self._stack_max(exo, cid)
            excess[cid] = max(0, self._count(exo, cid) - limit)
        for cid, x, y, amt in sorted(self._stacks(exo), key=lambda s: s[3]):
            if cid in keep:
                continue
            if cid in excess:
                amt = min(amt, excess[cid])
                if amt <= 0:
                    continue
            if only_into is not None:
                targets = [only_into] if self._count(ps.mInventories[12 + only_into], cid) > 0 else []
            else:
                having = [n for n in range(10) if self._count(ps.mInventories[12 + n], cid) > 0]
                targets = having + ([n for n in range(10) if n not in having] if allow_new else [])
            rest = amt
            for n in targets:
                if rest <= 0:
                    break
                moved = self._move(exo, x, y, cid, rest, ps.mInventories[12 + n])
                rest -= moved
                if moved:
                    self._touched.add(n)
                    stored[cid] = stored.get(cid, 0) + moved
                    if cid in excess:
                        excess[cid] -= moved
            if rest > 0 and targets:
                left[cid] = left.get(cid, 0) + rest
        return stored, left

    def _fetch(self, need):
        """Items aus den Lagern in den Exoanzug holen, bis {ID: Zielmenge} erreicht ist.
        Gibt (geholt, fehlt noch) als {ID: Menge} zurueck."""
        ps = gameData.player_state
        exo = ps.mInventories[0]
        got, missing = {}, {}
        for cid, want in need.items():
            have = self._count(exo, cid)
            rest = want - have
            if rest <= 0:
                continue
            sources = []
            for n in range(10):
                for scid, x, y, amt in self._stacks(ps.mInventories[12 + n]):
                    if scid == cid:
                        sources.append((amt, n, x, y))
            sources.sort()  # kleine Stapel zuerst, das macht Lagerplatz frei
            for amt, n, x, y in sources:
                if rest <= 0:
                    break
                moved = self._move(ps.mInventories[12 + n], x, y, cid, min(rest, amt), exo)
                rest -= moved
                if moved:
                    self._touched.add(n)
                    got[cid] = got.get(cid, 0) + moved
                elif self._count(exo, cid) < want:
                    break  # Exoanzug voll
            if rest > 0:
                missing[cid] = rest
        return got, missing

    def _refill(self):
        """Auffuellen-Liste abarbeiten: fertige Items bis zur Zielmenge holen, sonst Zutaten fuer den Rest."""
        ps = gameData.player_state
        exo = ps.mInventories[0]
        for raw in self._refill_text.split(";"):
            part = raw.strip()
            if not part:
                continue
            target = 0
            low = part.lower()
            pos = low.rfind(" x")
            if pos > 0 and low[pos + 2:].strip().isdigit():
                target = int(low[pos + 2:].strip())
                part = part[:pos].strip()
            hits = self._lookup(part)
            if len(hits) != 1 or target <= 0:
                why = "ohne Zielmenge" if target <= 0 else (
                    "nicht gefunden" if not hits else
                    "nicht eindeutig: " + ", ".join(sorted({self._dname(c) for c in hits})[:6]))
                logger.info(f"Auffuellen: '{raw.strip()}' {why}, uebersprungen")
                continue
            cid = hits[0]
            name = self._dname(cid)
            got, _missing = self._fetch({cid: target})
            have = self._count(exo, cid)
            msg = f"Auffuellen {name}: {have}/{target} im Exoanzug"
            if got:
                msg += f" ({got.get(cid, 0)} aus den Lagern geholt)"
            rest = target - have
            reqs = self._db[cid][2]
            if rest > 0 and reqs:
                # So viele herstellen, wie die Zutaten in Exoanzug + Lagern hergeben
                crafts = rest
                short = []
                for rid, amt in reqs:
                    if amt <= 0:
                        continue
                    avail = self._count(exo, rid) + sum(self._count(ps.mInventories[i], rid) for i in CONTAINERS)
                    crafts = min(crafts, avail // amt)
                    if avail < amt * rest:
                        short.append(f"{self._dname(rid)} {avail}/{amt * rest}")
                if crafts > 0:
                    need = {rid: amt * crafts for rid, amt in reqs}
                    got2, missing2 = self._fetch(need)
                    msg += f"; Zutaten fuer {crafts} weitere bereit ({self._fmt(need)})"
                    if missing2:
                        msg += f", fehlt: {self._fmt(missing2)}"
                else:
                    msg += "; fuer weitere fehlen Zutaten"
                if short:
                    msg += " (knapp: " + ", ".join(short) + ")"
            logger.info(msg)

    def _fmt(self, d):
        """{ID: Menge} als lesbarer Text fuers Log ("5 Natrium, 2 Kobalt")."""
        return ", ".join(f"{amt} {self._dname(cid)}" for cid, amt in sorted(d.items(), key=lambda t: self._dname(t[0])))

    def _run_job(self, job, n):
        """Einen vorgemerkten Auftrag ausfuehren (im Spieltakt). Vorher und nachher werden die Gesamtmengen
        verglichen; nur wenn sie stimmen, werden die beteiligten Lager danach sortiert."""
        if self._db is None:
            self._db = self._load_db()
            if self._db is None:
                logger.info("Packen: Spieldaten noch nicht geladen, bitte gleich noch einmal")
                return
        if job == "pruefen":
            need, problems = self._parse_pack()
            logger.info("Packliste braucht: " + (self._fmt(need) or "nichts"))
            for p in problems:
                logger.info("Packliste: " + p)
            return
        before = self._all_totals()
        never = self._never()
        self._touched = set()
        if job == "einlagern":
            stored, left = self._deposit(set(), never, only_into=n)
            logger.info(f"Schnell einlagern in Lager {n}: " + (self._fmt(stored) or "nichts passendes"))
        elif job == "alles":
            stored, left = self._deposit(set(), never)
            logger.info("Alles einlagern: " + (self._fmt(stored) or "nichts"))
            if left:
                logger.info("Kein Platz mehr fuer: " + self._fmt(left))
        elif job == "auffuellen":
            self._refill()
        elif job == "packen":
            need, problems = self._parse_pack(log=False)
            for p in problems:
                logger.info("Packliste: " + p)
            stored, left = self._deposit(set(need), never)
            got, missing = self._fetch(need)
            logger.info("Packen: eingelagert " + (self._fmt(stored) or "nichts") +
                        " | geholt " + (self._fmt(got) or "nichts"))
            if left:
                logger.info("Packen: kein Lagerplatz fuer " + self._fmt(left))
            if missing:
                logger.info("Packen: fehlt noch " + self._fmt(missing))
        after = self._all_totals()
        if after != before:
            logger.error(f"{job}: Gesamtmengen weichen ab! Bitte NICHT speichern und die Sicherung zurueckspielen.")
            self._log_diff(before, after)
            return
        if self._sort_after_on and job != "einlagern":
            for t in sorted(self._touched):
                self._sort_store(t)

    # ---------------------------------------------------------------- Sortieren

    def _find(self, store, x, y, cid):
        """Den Stapel mit dieser ID an Position (x, y) suchen (None, wenn er nicht mehr da ist)."""
        vec = store.mStore
        for k in range(len(vec)):
            e = vec[k]
            if _as_int(e.Index.X) == x and _as_int(e.Index.Y) == y and _clean_id(str(e.Id)) == cid:
                return e
        return None

    def _log_diff(self, a, b):
        """Abweichungen zwischen zwei Mengen-Uebersichten als Fehler loggen."""
        for cid in sorted(set(a) | set(b)):
            d = b.get(cid, 0) - a.get(cid, 0)
            if d:
                logger.error(f"    {cid}: {d:+d}")

    def _sort_store(self, n: int):
        """Lager n (0..9) sortieren: erst halbe Stapel zusammenlegen, dann alle Stapel neu anordnen.
        Nach jedem Schritt werden die Mengen geprueft; bei einer Abweichung wird abgebrochen."""
        store = gameData.player_state.mInventories[12 + n]
        before = self._summary(store)
        w, h = _as_int(store.miWidth), _as_int(store.miHeight)

        # 1) Halbe Stapel desselben Items zusammenlegen (Technik nie)
        merged = 0
        vec = store.mStore
        groups = {}
        for k in range(len(vec)):
            e = vec[k]
            amt = _as_int(e.Amount)
            if amt <= 0 or _as_int(e.Type) == 1:
                continue
            groups.setdefault(_clean_id(str(e.Id)), []).append(
                (_as_int(e.Index.X), _as_int(e.Index.Y), amt, _as_int(e.MaxAmount)))
        for cid, stacks in groups.items():
            mx = max(m for _x, _y, _a, m in stacks)
            partial = [st for st in stacks if st[2] < mx]
            if len(partial) < 2 or mx <= 0:
                continue
            # Groesste halbe Stapel fuellen (i von vorne), aus den kleinsten nehmen (j von hinten)
            partial.sort(key=lambda st: (-st[2], st[1], st[0]))
            amounts = {(x, y): a for x, y, a, _m in partial}
            i, j = 0, len(partial) - 1
            while i < j:
                r, d = partial[i][:2], partial[j][:2]
                space = mx - amounts[r]
                if space <= 0:
                    i += 1
                    continue
                take = min(space, amounts[d])
                if take <= 0:
                    j -= 1
                    continue
                rec = self._find(store, r[0], r[1], cid)
                if rec is None or self._find(store, d[0], d[1], cid) is None:
                    logger.error(f"Sortieren: Stapel {cid} nicht gefunden, Zusammenlegen abgebrochen")
                    break
                rec.Amount = amounts[r] + take
                idx = nmse.cGcInventoryIndex()
                idx.X, idx.Y = d
                store.Remove(idx, take)
                amounts[r] += take
                amounts[d] -= take
                merged += 1
                if amounts[d] <= 0:
                    j -= 1
        mid = self._summary(store)
        if mid != before:
            logger.error(f"Sortieren Lager {n}: Mengen nach dem Zusammenlegen weichen ab! "
                         "Bitte NICHT speichern und die Sicherung zurueckspielen.")
            self._log_diff(before, mid)
            return

        # 2) Neu anordnen: Rohstoffe, Produkte, Technik; darin nach deutschem Namen; leere Eintraege ans Ende
        vec = store.mStore
        items = []
        for k in range(len(vec)):
            e = vec[k]
            amt = _as_int(e.Amount)
            cid = _clean_id(str(e.Id))
            items.append((amt <= 0 and _as_int(e.Type) != 1, TYPE_ORDER.get(_as_int(e.Type), 3),
                          self._name(cid).lower(), cid, -amt, k))
        if w <= 0 or h <= 0 or len(items) > w * h:
            logger.error(f"Sortieren Lager {n}: unplausibel ({len(items)} Stapel fuer {w}x{h}), nichts umgestellt")
            return
        items.sort()
        # Nur die Positionen (Index.X/Y) werden neu geschrieben, Reihe fuer Reihe von links oben
        for pos, item in enumerate(items):
            e = vec[item[-1]]
            e.Index.X = pos % w
            e.Index.Y = pos // w
        after = self._summary(store)
        if after != before:
            logger.error(f"Sortieren Lager {n}: Mengen nach dem Umstellen weichen ab! "
                         "Bitte NICHT speichern und die Sicherung zurueckspielen.")
            self._log_diff(before, after)
            return
        logger.info(f"Lager {n} sortiert: {merged} Stapel zusammengelegt, {len(items)} Stapel angeordnet, "
                    f"Mengen unveraendert ({sum(after.values())} Einheiten)")
