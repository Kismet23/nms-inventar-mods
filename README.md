# Inventar-Mods für No Man's Sky (NMS.py)

Zwei Mods, die direkt im laufenden Spiel arbeiten (kein Bearbeiten von Spielständen, kein Neuladen nötig).
Sie basieren auf [NMS.py](https://github.com/monkeyman192/NMS.py) und [pyMHF](https://github.com/monkeyman192/pyMHF).

| Mod | Datei | Was sie macht |
|---|---|---|
| **Inventarsuche** (v0.27.4) | `mods/inventarsuche.py` | Live-Suche über alle Inventare (Exoanzug, Schiffe, Frachter, Lager, Fahrzeuge) in einem Fenster neben dem Spiel, optional Hervorhebung der Treffer im Inventar-Bildschirm, Bauplan-Baum mit Einkaufsliste. Liest Inventare nur, verändert sie nie. |
| **Lager-Werkzeuge** (v1.5) | `mods/lager_werkzeuge.py` | Im Lager-Bildschirm alle zehn Lagercontainer umschalten, sortieren, Stapel zusammenlegen, schnell einlagern, Packlisten und Auffüllen. **Verschiebt Items** über die Spielfunktionen und zählt danach die Gesamtmengen nach. |

> **Achtung:** Mods, die in den Spielspeicher eingreifen, laufen auf eigenes Risiko. Vor dem ersten Test eine
> Sicherung der Spielstände anlegen (`%APPDATA%\HelloGames\NMS`). Nach jedem Spiel-Update können die Mods
> nicht mehr funktionieren, bis eine passende NMS.py-Version erscheint.

## Voraussetzungen

- Windows, No Man's Sky über Steam
- **Getestet mit:** NMS-Build 184271, `nmspy==184271.0`, `pymhf==0.2.5`
- Python 3.13 (Python 3.14 wird von pyMHF noch nicht unterstützt)

## Installation

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install nmspy==184271.0
```

pyMHF muss wissen, wo die Mods liegen. In `%APPDATA%\pymhf\nmspy\pymhf.local.toml`:

```
mod_dir = "C:/Pfad/zu/diesem/Ordner/mods"
```

## Start

`start.bat` doppelklicken. Das Skript startet das Spiel über pyMHF und öffnet das NMS.py-Fenster, in dem jede Mod
einen eigenen Reiter hat. Beim ersten Start legen die Mods die Ordner `icons_cache/`, `daten/` und `logs/` selbst an
(sie enthalten aus dem Spiel gelesene Daten und sind deshalb nicht Teil des Repos).

## Wie der Code entstanden ist

Die Mods sind in Zusammenarbeit mit einer KI (Claude von Anthropic) entstanden: Ich habe Ziele vorgegeben, im Spiel
getestet und die Ergebnisse zurückgemeldet; der Code wurde daraufhin geschrieben und angepasst. Damit
nachvollziehbar ist, was er tut, ist der Code kommentiert, und Kopf-Kommentare der Dateien listen jede Version mit
ihren Änderungen. Besonders kommentiert sind alle Stellen, die in den Spielspeicher schreiben.

## Lizenz

MIT, siehe [LICENSE](LICENSE). No Man's Sky ist eine Marke von Hello Games; dieses Projekt ist nicht mit Hello Games
verbunden.
