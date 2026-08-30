# 2pdf

Rechtsklick → **2pdf** → PDF liegt im selben Ordner neben der Quelle.

## Was passiert

| Quelle | Ergebnis |
|--------|----------|
| `.txt` | A4-PDF mit **Zeilennummern** (nur Originaltext, kein Extra-Header) |
| ein Bild (`.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.webp`, `.tif`, `.tiff`) | A4-PDF, Bild zentriert, Seitenverhältnis erhalten (Querformat-Seite bei Querformat-Bild) |
| **mehrere Bilder** (Mehrfachauswahl oder CLI) | **ein** PDF, eine Seite pro Bild, Reihenfolge nach natürlichem Dateinamen (`image2` vor `image10`) |

- Schreibt die PDF **im gleichen Ordner** (überschreibt vorhandene PDF gleichen Namens)
- Log: `%LOCALAPPDATA%\2pdf\2pdf.log`

### Mehrfachauswahl (Bilder)

Explorer startet 2pdf einmal pro Datei. Ein kurzer Sammler (~0,7 s nach der letzten Datei) fasst die Auswahl zusammen.

| Auswahl | PDF-Name |
|---------|----------|
| `image01.jpg` … `image03.jpg` | `image01-03.pdf` |
| gleiche Nummerierung, Lücken | `IMG_0008-0012.pdf` (erste–letzte Nummer nach Sortierung) |
| gemischte Namen | `foto+2.pdf` (erste Datei + restliche Anzahl) |
| eine Datei | unverändert `foto.pdf` |

TXT-Dateien in derselben Auswahl werden weiterhin **einzeln** konvertiert. Bilder aus verschiedenen Ordnern ergeben **ein PDF pro Ordner**.

Windows fragt ab **16 Dateien** einmal nach, ob alle geöffnet werden sollen — das ist Explorer, nicht 2pdf. Mit Ja bestätigen.

Die Klickreihenfolge im Explorer ist nicht rekonstruierbar; sortiert wird immer nach Dateiname.

## Installation

Doppelklick auf:

```
install.bat
```

Oder manuell:

```bat
python -m pip install -r requirements.txt
python 2pdf.py --install
```

Kein Admin nötig (`HKCU`). Nach Update der unterstützten Formate oder des Kontextmenü-Befehls **install.bat erneut** ausführen.

## Nutzung

1. Rechtsklick auf `.txt` oder Bild im Explorer — oder mehrere Bilder markieren  
2. **2pdf** wählen  
3. PDF erscheint am gleichen Ort

CLI:

```bat
python 2pdf.py C:\pfad\notiz.txt
python 2pdf.py C:\pfad\foto.jpg
python 2pdf.py image01.jpg image02.jpg image03.jpg
```

Mehrere Bilder in einem CLI-Aufruf ergeben dasselbe Sammel-PDF wie die Explorer-Mehrfachauswahl.

## Deinstallation

Doppelklick auf `uninstall.bat`, oder:

```bat
python 2pdf.py --uninstall
```

## Technik

| Teil | Details |
|------|---------|
| Engine | reportlab (+ Pillow für Bilder/EXIF/WebP) |
| TXT | EMLex-Zeilennummern-Layout, ohne Struktur-Header |
| Bild | eine Seite, Rand 10 mm, EXIF-Orientierung, Hoch-/Querformat-A4 |
| Sammel-PDF | eine Seite pro Bild, natürliche Dateinamen-Sortierung |
| Kontextmenü | `HKCU\Software\Classes\SystemFileAssociations\<ext>\shell\2pdf` |
| Aufruf | `pythonw.exe …\2pdf.py --collect "%1"` |
| Sammler | Tickets unter `%LOCALAPPDATA%\2pdf\queue\`, ein Leader-Prozess schreibt das PDF |

## Abhängigkeiten

- Python 3.11+
- `reportlab`, `Pillow`

## Log

```
%LOCALAPPDATA%\2pdf\2pdf.log
```

Rotation bei ~2 MB, 3 Backups.
