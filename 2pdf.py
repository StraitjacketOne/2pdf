#!/usr/bin/env python3
"""
2pdf — Datei → PDF im Windows-Kontextmenü.

- .txt  → A4-PDF mit Zeilennummern (EMLex-Layout, ohne Struktur-Header)
- Bilder → A4-PDF, Bild seitenfüllend (Seitenverhältnis erhalten)
- Mehrfachauswahl Bilder → ein PDF, Seiten in natürlicher Dateinamen-Reihenfolge

PDF landet neben der Quelle im gleichen Ordner.

Aufruf:
    python 2pdf.py <datei>
    python 2pdf.py <bild1> <bild2> <bild3>   # ein Sammel-PDF
    python 2pdf.py --collect <datei>         # Explorer: Instanzen sammeln
    python 2pdf.py --install                 # Kontextmenü einrichten (HKCU)
    python 2pdf.py --uninstall               # Kontextmenü entfernen
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "2pdf"
VERSION = "1.2.0"
LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
LOG_FILE = LOG_DIR / "2pdf.log"
QUEUE_DIR = LOG_DIR / "queue"
LOCK_FILE = LOG_DIR / "collector.lock"

# Explorer startet pro Datei einen Prozess; der Leader wartet, bis nichts
# Neues mehr eintrifft, und schreibt dann ein Sammel-PDF.
COLLECT_IDLE_S = 0.7
COLLECT_TAIL_S = 0.25
COLLECT_MAX_S = 45.0
COLLECT_POLL_S = 0.05
TICKET_MAX_AGE_S = 3600.0

TXT_EXTS = frozenset({".txt"})
IMAGE_EXTS = frozenset({
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
})
SUPPORTED_EXTS = TXT_EXTS | IMAGE_EXTS

# Registry: Shell-Verb pro Dateiendung (HKCU, kein Admin)
REG_SHELL_TMPL = r"Software\Classes\SystemFileAssociations\{ext}\shell\2pdf"

_NAT_SPLIT = re.compile(r"(\d+)")
_TRAILING_NUM = re.compile(r"^(.*?)(\d+)$")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logging.raiseExceptions = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
        delay=True,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if sys.stdout and getattr(sys.stdout, "isatty", lambda: False)():
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Fonts (Windows: Arial für Umlaute; Fallback Helvetica)
# ---------------------------------------------------------------------------

_FONTS: tuple[str, str] | None = None


def _require_reportlab() -> None:
    try:
        import reportlab  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "reportlab ist nicht installiert. "
            "Bitte: python -m pip install reportlab"
        ) from exc


def _register_fonts() -> tuple[str, str]:
    global _FONTS
    if _FONTS is not None:
        return _FONTS
    _require_reportlab()
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = [
        (windir / "Fonts" / "arial.ttf", windir / "Fonts" / "arialbd.ttf", "2pdfArial", "2pdfArial-Bold"),
        (windir / "Fonts" / "calibri.ttf", windir / "Fonts" / "calibrib.ttf", "2pdfCalibri", "2pdfCalibri-Bold"),
    ]

    for regular_path, bold_path, reg_name, bold_name in candidates:
        if regular_path.is_file() and bold_path.is_file():
            try:
                pdfmetrics.registerFont(TTFont(reg_name, str(regular_path)))
                pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
                _FONTS = (reg_name, bold_name)
                return _FONTS
            except Exception as exc:
                log.warning("Font-Registrierung fehlgeschlagen (%s): %s", regular_path, exc)

    _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


def _default_pdf_path(src: Path) -> Path:
    return src.with_suffix(".pdf")


def _set_pdf_meta(c, *, title: str, subject: str) -> None:
    c.setTitle(title)
    c.setAuthor(APP_NAME)
    c.setSubject(subject)
    c.setCreator(f"{APP_NAME} {VERSION}")


def natural_key(name: str) -> tuple:
    """Sortierschlüssel: image2 vor image10 (nicht lexikographisch)."""
    parts = []
    for part in _NAT_SPLIT.split(name):
        if part.isdigit():
            parts.append((0, int(part)))
        elif part:
            parts.append((1, part.casefold()))
    return tuple(parts)


def combined_pdf_filename(paths: list[Path]) -> str:
    """image01.jpg + image02.jpg + image03.jpg → image01-03.pdf, sonst stem+N.pdf."""
    parsed: list[tuple[str, str]] = []
    for path in paths:
        match = _TRAILING_NUM.match(path.stem)
        if not match:
            break
        parsed.append((match.group(1), match.group(2)))
    else:
        prefixes = {prefix for prefix, _ in parsed}
        if len(prefixes) == 1:
            first_num, last_num = parsed[0][1], parsed[-1][1]
            if int(first_num) != int(last_num):
                return f"{parsed[0][0]}{first_num}-{last_num}.pdf"

    extra = len(paths) - 1
    return f"{paths[0].stem}+{extra}.pdf"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


# ---------------------------------------------------------------------------
# Text lesen
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# TXT → PDF (Zeilennummern, ohne Struktur-Header)
# ---------------------------------------------------------------------------

def txt_to_pdf(txt_path: Path, pdf_path: Path | None = None) -> Path:
    _require_reportlab()
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    txt_path = Path(txt_path).resolve()
    if not txt_path.is_file():
        raise FileNotFoundError(f"Datei nicht gefunden: {txt_path}")
    if txt_path.suffix.lower() not in TXT_EXTS:
        raise ValueError(f"Keine TXT-Datei: {txt_path.name}")

    pdf_path = Path(pdf_path).resolve() if pdf_path else _default_pdf_path(txt_path)
    content = read_text(txt_path)
    font_reg, font_bold = _register_fonts()

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    doc.title = txt_path.stem
    doc.author = APP_NAME
    doc.subject = f"Konvertiert aus {txt_path.name}"
    doc.creator = f"{APP_NAME} {VERSION}"

    styles = getSampleStyleSheet()
    line_style = ParagraphStyle(
        "TwoPdfLine",
        parent=styles["Normal"],
        fontName=font_reg,
        fontSize=8.5,
        leading=10.5,
    )
    section_style = ParagraphStyle(
        "TwoPdfSection",
        parent=line_style,
        fontName=font_bold,
        textColor=colors.HexColor("#222222"),
    )
    number_style = ParagraphStyle(
        "TwoPdfLineNumber",
        parent=styles["Normal"],
        fontName=font_reg,
        fontSize=7,
        leading=10.5,
        textColor=colors.HexColor("#777777"),
        alignment=2,
    )

    rows = []
    for number, line in enumerate(content.splitlines(), start=1):
        escaped = html.escape(line) if line else "&nbsp;"
        current_style = section_style if line.strip().endswith(":") else line_style
        rows.append(
            [
                Paragraph(str(number), number_style),
                Paragraph(escaped, current_style),
            ]
        )

    if not rows:
        rows.append([Paragraph("1", number_style), Paragraph("&nbsp;", line_style)])

    table = Table(rows, colWidths=[13 * mm, 157 * mm], repeatRows=0, splitByRow=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                ("LINEBEFORE", (1, 0), (1, -1), 0.25, colors.HexColor("#D0D0D0")),
            ]
        )
    )

    doc.build([table])
    return pdf_path


# ---------------------------------------------------------------------------
# Bild → PDF (eine Seite pro Bild, Seitenverhältnis erhalten, zentriert)
# ---------------------------------------------------------------------------

def _load_image_for_pdf(path: Path):
    """Lädt Bild; EXIF-Orientierung; RGB für PDF. Gibt (ImageReader, w_px, h_px) zurück."""
    _require_reportlab()
    from reportlab.lib.utils import ImageReader

    try:
        from PIL import Image, ImageOps
        pil_available = True
    except ImportError:
        pil_available = False

    if not pil_available:
        reader = ImageReader(str(path))
        w, h = reader.getSize()
        return reader, w, h

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if getattr(im, "is_animated", False):
            im.seek(0)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            background = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            im = background
        elif im.mode != "RGB":
            im = im.convert("RGB")
        rgb = im.copy()

    reader = ImageReader(rgb)
    w, h = rgb.size
    return reader, w, h


def _draw_image_page(c, reader, px_w: int, px_h: int) -> None:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm

    page = landscape(A4) if px_w > px_h else A4
    c.setPageSize(page)
    page_w, page_h = page
    margin = 10 * mm
    max_w = page_w - 2 * margin
    max_h = page_h - 2 * margin

    scale = min(max_w / px_w, max_h / px_h)
    draw_w = px_w * scale
    draw_h = px_h * scale
    x = (page_w - draw_w) / 2
    y = (page_h - draw_h) / 2

    c.drawImage(
        reader,
        x,
        y,
        width=draw_w,
        height=draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )
    c.showPage()


def images_to_pdf(img_paths: list[Path], pdf_path: Path) -> tuple[Path, list[Path]]:
    """Mehrere Bilder → ein PDF (eine Seite pro Bild). Gibt (pdf, übersprungene) zurück."""
    _require_reportlab()
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas as pdf_canvas

    resolved: list[Path] = []
    for img_path in img_paths:
        img_path = Path(img_path).resolve()
        if not img_path.is_file():
            raise FileNotFoundError(f"Datei nicht gefunden: {img_path}")
        if img_path.suffix.lower() not in IMAGE_EXTS:
            raise ValueError(f"Kein unterstütztes Bild: {img_path.name}")
        resolved.append(img_path)

    if not resolved:
        raise ValueError("Keine Bilder angegeben")

    pdf_path = Path(pdf_path).resolve()
    skipped: list[Path] = []
    canvas = None

    for img_path in resolved:
        try:
            reader, px_w, px_h = _load_image_for_pdf(img_path)
            if px_w <= 0 or px_h <= 0:
                raise ValueError(f"Ungültige Bildgröße: {img_path.name}")
            if canvas is None:
                page = landscape(A4) if px_w > px_h else A4
                canvas = pdf_canvas.Canvas(str(pdf_path), pagesize=page)
                if len(resolved) == 1:
                    _set_pdf_meta(
                        canvas,
                        title=resolved[0].stem,
                        subject=f"Konvertiert aus {resolved[0].name}",
                    )
                else:
                    _set_pdf_meta(
                        canvas,
                        title=pdf_path.stem,
                        subject=(
                            f"Konvertiert aus {len(resolved)} Bildern: "
                            f"{resolved[0].name} … {resolved[-1].name}"
                        ),
                    )
            _draw_image_page(canvas, reader, px_w, px_h)
        except Exception as exc:
            log.error("Bild übersprungen %s: %s", img_path, exc)
            skipped.append(img_path)

    if canvas is None:
        raise RuntimeError("Kein Bild konvertierbar")

    canvas.save()
    return pdf_path, skipped


def image_to_pdf(img_path: Path, pdf_path: Path | None = None) -> Path:
    img_path = Path(img_path).resolve()
    if not img_path.is_file():
        raise FileNotFoundError(f"Datei nicht gefunden: {img_path}")
    if img_path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"Kein unterstütztes Bild: {img_path.name}")

    pdf_path = Path(pdf_path).resolve() if pdf_path else _default_pdf_path(img_path)
    out, skipped = images_to_pdf([img_path], pdf_path)
    if skipped:
        raise RuntimeError(f"Bild nicht konvertierbar: {img_path.name}")
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def convert_to_pdf(src: Path, pdf_path: Path | None = None) -> Path:
    src = Path(src)
    ext = src.suffix.lower()
    if ext in TXT_EXTS:
        return txt_to_pdf(src, pdf_path)
    if ext in IMAGE_EXTS:
        return image_to_pdf(src, pdf_path)
    raise ValueError(
        f"Nicht unterstützt: {src.name} "
        f"(erlaubt: {', '.join(sorted(SUPPORTED_EXTS))})"
    )


def convert_one(path: Path) -> int:
    log.info("Start: %s", path)
    try:
        out = convert_to_pdf(path)
        size = out.stat().st_size
        log.info("OK: %s -> %s (%d bytes)", path, out, size)
        print(f"[OK] {out}")
        return 0
    except Exception as exc:
        log.error("Fehler bei %s: %s\n%s", path, exc, traceback.format_exc())
        print(f"[ERR] {exc}", file=sys.stderr)
        return 1


def process_batch(paths: list[Path]) -> int:
    """TXT einzeln; Bilder pro Ordner zu einem PDF (natürliche Reihenfolge)."""
    existing: list[Path] = []
    rc = 0
    for raw in _dedupe_paths([Path(p) for p in paths]):
        try:
            path = raw.resolve()
        except OSError:
            path = raw
        if not path.is_file():
            log.error("Datei nicht gefunden: %s", path)
            print(f"[ERR] Datei nicht gefunden: {path}", file=sys.stderr)
            rc = 1
            continue
        existing.append(path)

    images = [p for p in existing if p.suffix.lower() in IMAGE_EXTS]
    txts = [p for p in existing if p.suffix.lower() in TXT_EXTS]
    others = [
        p for p in existing
        if p.suffix.lower() not in SUPPORTED_EXTS
    ]
    for other in others:
        log.error("Nicht unterstützt: %s", other)
        print(f"[ERR] Nicht unterstützt: {other.name}", file=sys.stderr)
        rc = 1

    by_folder: dict[Path, list[Path]] = defaultdict(list)
    for img in images:
        by_folder[img.parent].append(img)

    for folder, imgs in by_folder.items():
        imgs = sorted(imgs, key=lambda p: natural_key(p.name))
        if len(imgs) == 1:
            if convert_one(imgs[0]) != 0:
                rc = 1
            continue

        dest = folder / combined_pdf_filename(imgs)
        log.info(
            "Sammel-PDF (%d Bilder, %s): %s",
            len(imgs),
            folder,
            dest.name,
        )
        try:
            out, skipped = images_to_pdf(imgs, dest)
            size = out.stat().st_size
            log.info("OK: %s (%d bytes, %d übersprungen)", out, size, len(skipped))
            print(f"[OK] {out}")
            if skipped:
                rc = 1
                for skipped_path in skipped:
                    print(f"[ERR] übersprungen: {skipped_path.name}", file=sys.stderr)
        except Exception as exc:
            log.error("Sammel-PDF fehlgeschlagen %s: %s\n%s", dest, exc, traceback.format_exc())
            print(f"[ERR] {exc}", file=sys.stderr)
            rc = 1

    for txt in txts:
        if convert_one(txt) != 0:
            rc = 1
    return rc


# ---------------------------------------------------------------------------
# Collector (Explorer startet einen Prozess pro Datei)
# ---------------------------------------------------------------------------

def _write_ticket(path: Path) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}_{os.getpid()}_{uuid.uuid4().hex[:8]}.json"
    payload = json.dumps({"path": str(Path(path).resolve())}, ensure_ascii=False)
    tmp = QUEUE_DIR / f"{name}.tmp"
    dest = QUEUE_DIR / name
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(dest)


def _ticket_count() -> int:
    if not QUEUE_DIR.is_dir():
        return 0
    return sum(1 for _ in QUEUE_DIR.glob("*.json"))


def _drain_tickets() -> list[Path]:
    if not QUEUE_DIR.is_dir():
        return []
    now = time.time()
    paths: list[Path] = []
    for ticket in QUEUE_DIR.glob("*.json"):
        try:
            age = now - ticket.stat().st_mtime
            if age > TICKET_MAX_AGE_S:
                ticket.unlink(missing_ok=True)
                continue
            data = json.loads(ticket.read_text(encoding="utf-8"))
            paths.append(Path(data["path"]))
            ticket.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("Ticket unlesbar %s: %s", ticket.name, exc)
            try:
                ticket.unlink(missing_ok=True)
            except OSError:
                pass
    return paths


def _wait_queue_idle() -> None:
    start = time.monotonic()
    last_count = -1
    last_change = start
    while True:
        now = time.monotonic()
        count = _ticket_count()
        if count != last_count:
            last_count = count
            last_change = now
        if now - last_change >= COLLECT_IDLE_S:
            return
        if now - start >= COLLECT_MAX_S:
            log.warning("Collector: Maxwartezeit, starte mit %d Datei(en)", count)
            return
        time.sleep(COLLECT_POLL_S)


def _try_leader_lock():
    """Sperre, die der OS beim Prozessende freigibt. Leader = File-Handle, sonst None."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return None
            return fh

        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh
    except Exception:
        fh.close()
        raise


def _release_leader_lock(fh) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def collect_and_run(paths: list[Path]) -> int:
    """Alle --collect-Instanzen legen Tickets; ein Leader macht den Batch."""
    queued = 0
    for raw in paths:
        path = Path(raw)
        try:
            _write_ticket(path)
            queued += 1
            log.debug("Queue: %s", path)
        except Exception as exc:
            log.error("Ticket fehlgeschlagen %s: %s", path, exc)
            if convert_one(path) != 0:
                return 1

    if queued == 0:
        return 1

    lock = _try_leader_lock()
    if lock is None:
        log.debug("Collector-Follower, %d Ticket(s) übergeben", queued)
        return 0

    log.info("Collector-Leader, warte auf weitere Dateien...")
    try:
        rc = 0
        while True:
            _wait_queue_idle()
            batch = _drain_tickets()
            if not batch:
                time.sleep(COLLECT_TAIL_S)
                batch = _drain_tickets()
            if not batch:
                break
            log.info("Batch: %d Datei(en)", len(batch))
            if process_batch(batch) != 0:
                rc = 1
        return rc
    finally:
        _release_leader_lock(lock)


# ---------------------------------------------------------------------------
# Windows Kontextmenü (HKCU)
# ---------------------------------------------------------------------------

def _python_for_shell() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.is_file():
        return str(pythonw)
    return str(exe)


def _shell_key(ext: str) -> str:
    if not ext.startswith("."):
        ext = "." + ext
    return REG_SHELL_TMPL.format(ext=ext)


def install_context_menu() -> None:
    """Registriert '2pdf' für alle unterstützten Endungen (aktueller Benutzer)."""
    try:
        import winreg
    except ImportError as exc:
        raise RuntimeError("winreg nur unter Windows verfügbar") from exc

    script = str(Path(__file__).resolve())
    python = _python_for_shell()
    # Document: Explorer startet einmal pro Datei — der Sammler fasst zusammen.
    command = f'"{python}" "{script}" --collect "%1"'

    for ext in sorted(SUPPORTED_EXTS):
        shell = _shell_key(ext)
        cmd = shell + r"\command"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, shell) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "2pdf")
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, python)
            winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, "Document")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)

    log.info("Kontextmenü installiert für %s: %s", sorted(SUPPORTED_EXTS), command)
    print(f"[OK] Kontextmenü '2pdf' eingerichtet für:")
    print(f"     {', '.join(sorted(SUPPORTED_EXTS))}")
    print(f"     Befehl: {command}")
    print(f"     Log:    {LOG_FILE}")


def uninstall_context_menu() -> None:
    try:
        import winreg
    except ImportError as exc:
        raise RuntimeError("winreg nur unter Windows verfügbar") from exc

    def _delete_tree(root, path: str) -> None:
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                while True:
                    try:
                        sub = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    _delete_tree(root, path + "\\" + sub)
            winreg.DeleteKey(root, path)
        except FileNotFoundError:
            pass

    for ext in sorted(SUPPORTED_EXTS):
        shell = _shell_key(ext)
        _delete_tree(winreg.HKEY_CURRENT_USER, shell + r"\command")
        _delete_tree(winreg.HKEY_CURRENT_USER, shell)

    log.info("Kontextmenü entfernt")
    print("[OK] Kontextmenü '2pdf' entfernt.")


# ---------------------------------------------------------------------------
# CLI / Entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="TXT/Bilder → PDF, neben die Quelldatei. Mehrere Bilder → ein PDF.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Datei(en): .txt oder Bilder (.jpg, .png, …)",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Explorer-Modus: kurz warten und Mehrfachauswahl zu einem PDF bündeln",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Windows-Kontextmenü einrichten (HKCU)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Windows-Kontextmenü entfernen",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {VERSION}",
    )
    args = parser.parse_args(argv)

    if args.install:
        try:
            install_context_menu()
            return 0
        except Exception as exc:
            log.error("Install fehlgeschlagen: %s", exc)
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1

    if args.uninstall:
        try:
            uninstall_context_menu()
            return 0
        except Exception as exc:
            log.error("Uninstall fehlgeschlagen: %s", exc)
            print(f"[ERR] {exc}", file=sys.stderr)
            return 1

    if not args.files:
        parser.print_help()
        print(f"\nUnterstützt: {', '.join(sorted(SUPPORTED_EXTS))}")
        print(f"Logdatei: {LOG_FILE}")
        return 2

    paths = [Path(f) for f in args.files]
    if args.collect:
        return collect_and_run(paths)
    return process_batch(paths)


if __name__ == "__main__":
    sys.exit(main())
