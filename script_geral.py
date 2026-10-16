import sys
import re
import os
import shutil
import threading
from pathlib import Path
from datetime import datetime

import numpy as np

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QListWidget, QListWidgetItem,
    QFrame, QProgressBar, QMessageBox, QAbstractItemView, QStatusBar,
    QTabWidget, QTextEdit, QSplitter, QCheckBox, QComboBox, QLineEdit,
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPalette, QDragEnterEvent, QDropEvent

# ── Dependência opcional para a aba de conversão de versão LAZ ──
# pip install laspy[lazrs]   (ou laspy[laszip])
try:
    import laspy
    LASPY_AVAILABLE = True
except ImportError:
    LASPY_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
#  PALETA GLOBAL  (monocromática, profissional)
# ══════════════════════════════════════════════════════════════════════════════
C = {
    "bg":        "#F5F6F7",   # fundo geral
    "surface":   "#FFFFFF",   # superfícies (cards, listas, inputs)
    "border":    "#DDE1E7",   # bordas
    "border2":   "#C8CDD6",   # bordas hover
    "text":      "#1A1D23",   # texto principal
    "text2":     "#4B5563",   # texto secundário
    "text3":     "#8B95A1",   # placeholders / hints
    "accent":    "#2A5FAC",   # ação principal (azul escuro neutro)
    "accentHov": "#1E4A8A",   # hover
    "accentDis": "#8FB3DC",   # disabled
    "accentBg":  "#EAF0FA",   # fundo sutil de ativo
    "danger":    "#B91C1C",   # vermelho
    "dangerBg":  "#FEF2F2",
    "dangerBrd": "#FECACA",
    "selBg":     "#E8EFF9",   # seleção em lista
    "altRow":    "#FAFBFC",   # linha alternada
    "green":     "#1A6B3C",   # sucesso no log
    "greenBg":   "#F0FBF4",
    "gray":      "#6B7280",
    "logBg":     "#FAFBFC",
}

STYLE = f"""
/* ── Reset geral ── */
QMainWindow, QWidget {{
    background: {C['bg']};
    font-family: "Segoe UI";
    font-size: 10px;
    color: {C['text']};
}}

/* ── Cabeçalho ── */
QFrame#header {{
    background: {C['surface']};
    border-bottom: 1px solid {C['border']};
}}
QLabel#headerTitle {{ color: {C['text']};  background: transparent; font-size: 15px; font-weight: 700; }}
QLabel#headerSub   {{ color: {C['text3']}; background: transparent; font-size: 9px; }}

/* ── Abas ── */
QTabWidget#mainTabs::pane {{
    border: none;
    background: {C['bg']};
}}
QTabWidget#mainTabs > QTabBar::tab {{
    background: {C['bg']};
    color: {C['text2']};
    border: 1px solid {C['border']};
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    padding: 9px 22px;
    font-size: 10px;
    font-weight: 500;
    margin-right: 3px;
}}
QTabWidget#mainTabs > QTabBar::tab:selected {{
    background: {C['surface']};
    color: {C['accent']};
    font-weight: 700;
    border-color: {C['border']};
}}
QTabWidget#mainTabs > QTabBar::tab:hover:!selected {{
    background: #ECEEF1;
    color: {C['text']};
}}

/* ── Seções ── */
QFrame#section {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}
QLabel#sectionTitle {{
    color: {C['text2']};
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: transparent;
}}
QLabel#hint {{
    color: {C['text3']};
    font-size: 9px;
    background: transparent;
}}

/* ── Listas ── */
QListWidget {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    font-family: "Segoe UI";
    font-size: 10px;
    color: {C['text']};
    outline: none;
}}
QListWidget::item {{ padding: 5px 10px; border-radius: 4px; }}
QListWidget::item:selected {{ background: {C['selBg']}; color: {C['accent']}; }}
QListWidget::item:alternate {{ background: {C['altRow']}; }}

/* ── TextEdit ── */
QTextEdit {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    color: {C['text']};
    selection-background-color: {C['selBg']};
}}

/* ── LineEdit / ComboBox ── */
QLineEdit, QComboBox {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 6px;
    padding: 0 10px;
    color: {C['text']};
    font-size: 10px;
    min-height: 32px;
}}
QLineEdit:focus, QComboBox:focus {{
    border-color: {C['accent']};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    selection-background-color: {C['selBg']};
    color: {C['text']};
}}

/* ── CheckBox ── */
QCheckBox {{
    color: {C['text2']};
    background: transparent;
    spacing: 7px;
    font-size: 10px;
}}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {C['border2']};
    border-radius: 3px;
    background: {C['surface']};
}}
QCheckBox::indicator:checked {{
    background: {C['accent']};
    border-color: {C['accent']};
}}

/* ── Botão primário ── */
QPushButton#primaryBtn {{
    background: {C['accent']};
    color: #FFFFFF;
    border: none;
    border-radius: 7px;
    font-size: 10px;
    font-weight: 600;
    padding: 0 22px;
    min-height: 36px;
}}
QPushButton#primaryBtn:hover   {{ background: {C['accentHov']}; }}
QPushButton#primaryBtn:pressed {{ background: #183D75; }}
QPushButton#primaryBtn:disabled {{ background: {C['accentDis']}; color: #FFFFFF; }}

/* ── Botão outline (neutro) ── */
QPushButton#outlineBtn {{
    background: {C['surface']};
    color: {C['text2']};
    border: 1px solid {C['border']};
    border-radius: 7px;
    font-size: 10px;
    font-weight: 500;
    padding: 0 14px;
    min-height: 36px;
}}
QPushButton#outlineBtn:hover   {{ background: #ECEEF1; border-color: {C['border2']}; }}
QPushButton#outlineBtn:pressed {{ background: #E2E5EA; }}
QPushButton#outlineBtn:disabled {{ color: {C['text3']}; }}

/* ── Botão danger ── */
QPushButton#dangerBtn {{
    background: {C['surface']};
    color: {C['danger']};
    border: 1px solid {C['dangerBrd']};
    border-radius: 7px;
    font-size: 10px;
    font-weight: 500;
    padding: 0 14px;
    min-height: 36px;
}}
QPushButton#dangerBtn:hover   {{ background: {C['dangerBg']}; border-color: #F87171; }}
QPushButton#dangerBtn:pressed {{ background: #FEE2E2; }}

/* ── Toggle (modo) ── */
QPushButton#toggleBtn {{
    background: #ECEEF1;
    color: {C['text2']};
    border: 1px solid {C['border']};
    border-radius: 7px;
    font-size: 10px;
    font-weight: 600;
    padding: 0 16px;
    min-height: 34px;
    min-width: 110px;
}}
QPushButton#toggleBtn:checked {{
    background: {C['accentBg']};
    color: {C['accent']};
    border-color: #A8C1E8;
}}
QPushButton#toggleBtn:hover:!checked {{ background: #E2E5EA; }}

/* ── Cards de estatística ── */
QFrame#statCard {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 8px;
}}

/* ── Progress ── */
QProgressBar {{
    background: {C['border']};
    border: none;
    border-radius: 3px;
    min-height: 6px;
    max-height: 6px;
}}
QProgressBar::chunk {{
    background: {C['accent']};
    border-radius: 3px;
}}

/* ── Status bar ── */
QStatusBar {{
    background: {C['surface']};
    color: {C['text3']};
    font-size: 9px;
    border-top: 1px solid {C['border']};
}}

/* ── Splitter ── */
QSplitter::handle {{
    background: {C['border']};
    width: 1px;
    height: 1px;
}}

/* ── Scrollbars ── */
QScrollBar:vertical {{
    background: {C['bg']};
    width: 8px;
    margin: 0;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {C['border2']};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {C['text3']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {C['bg']};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {C['border2']};
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {C['text3']}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  WORKERS
# ══════════════════════════════════════════════════════════════════════════════

class RenameWorker(QThread):
    progress  = Signal(int)
    file_done = Signal(str, str, bool)
    finished  = Signal(int, int)

    def __init__(self, files, target_ext):
        super().__init__()
        self.files = files
        self.target_ext = target_ext

    def run(self):
        ok = erros = 0
        for i, src in enumerate(self.files):
            dst = src.with_suffix(self.target_ext)
            try:
                src.rename(dst)
                self.file_done.emit(str(src), str(dst), True)
                ok += 1
            except Exception as e:
                self.file_done.emit(str(src), str(e), False)
                erros += 1
            self.progress.emit(i + 1)
        self.finished.emit(ok, erros)



class LowercaseLazWorker(QThread):
    """Renomeia somente a extensão .LAZ maiúscula para .laz minúscula."""
    progress  = Signal(int)
    file_done = Signal(str, str, bool)
    finished  = Signal(int, int)

    def __init__(self, files):
        super().__init__()
        self.files = files

    def run(self):
        ok = erros = 0
        for i, src in enumerate(self.files):
            # Troca apenas os 4 últimos caracteres: .LAZ -> .laz
            dst = src.with_name(src.name[:-4] + ".laz")
            try:
                # No Windows, renomear mudando apenas maiúsculas/minúsculas
                # pode exigir uma etapa intermediária.
                if src.name != dst.name and src.name.lower() == dst.name.lower():
                    temp = src.with_name(src.name + ".__tmp_case__")
                    src.rename(temp)
                    temp.rename(dst)
                else:
                    src.rename(dst)
                self.file_done.emit(str(src), str(dst), True)
                ok += 1
            except Exception as e:
                self.file_done.emit(str(src), str(e), False)
                erros += 1
            self.progress.emit(i + 1)
        self.finished.emit(ok, erros)

class PrjEditWorker(QThread):
    progress  = Signal(int)
    file_done = Signal(str, int, int, bool, str)
    finished  = Signal(int, int, int, int)

    def __init__(self, files, mode, backup):
        super().__init__()
        self.files  = files
        self.mode   = mode
        self.backup = backup

    def run(self):
        files_ok = files_err = total_las = total_laz = 0
        for i, prj in enumerate(self.files):
            try:
                text = prj.read_text(encoding="utf-8", errors="replace")
                new_text, n_las, n_laz = self._replace(text)
                if new_text == text:
                    self.file_done.emit(str(prj), 0, 0, True, "sem alterações")
                else:
                    if self.backup:
                        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
                        shutil.copy2(prj, prj.with_suffix(f".bak_{ts}"))
                    prj.write_text(new_text, encoding="utf-8")
                    total_las += n_las
                    total_laz += n_laz
                    self.file_done.emit(str(prj), n_las, n_laz, True,
                                        f"{n_las} .las→.laz  |  {n_laz} .laz→.las")
                    files_ok += 1
            except Exception as e:
                self.file_done.emit(str(prj), 0, 0, False, str(e))
                files_err += 1
            self.progress.emit(i + 1)
        self.finished.emit(files_ok, files_err, total_las, total_laz)

    def _replace(self, text):
        n_las = n_laz = 0
        if self.mode in ("las_to_laz", "both"):
            def sub_las(m):
                nonlocal n_las; n_las += 1
                orig = m.group(0)
                if orig.upper().startswith("STORAGE=LAS"):
                    repl = "LAZ" if orig[8:11].isupper() else "laz"
                    return orig[:8] + repl + orig[11:]
                return orig[:-3] + ("LAZ" if orig[-3:].isupper() else "laz")
            text = re.sub(r'(?i)(?:Storage=LAS(?=\d))|(?:\.las\b)', sub_las, text)
        if self.mode in ("laz_to_las", "both"):
            def sub_laz(m):
                nonlocal n_laz; n_laz += 1
                orig = m.group(0)
                if orig.upper().startswith("STORAGE=LAZ"):
                    repl = "LAS" if orig[8:11].isupper() else "las"
                    return orig[:8] + repl + orig[11:]
                return orig[:-3] + ("LAS" if orig[-3:].isupper() else "las")
            text = re.sub(r'(?i)(?:Storage=LAZ(?=\d))|(?:\.laz\b)', sub_laz, text)
        return text, n_las, n_laz


class CopyWorker(QThread):
    progress  = Signal(int)
    file_done = Signal(str, bool, str)   # nome, ok, msg
    finished  = Signal(int, int)

    def __init__(self, entries, src_folder, dest_folder):
        super().__init__()
        self.entries     = entries        # lista de strings (caminhos do PRJ)
        self.src_folder  = src_folder
        self.dest_folder = dest_folder

    def run(self):
        ok = erros = 0
        for i, entry in enumerate(self.entries):
            name = os.path.basename(entry)
            candidates = [
                entry,
                os.path.join(self.src_folder, entry),
                os.path.join(self.src_folder, name),
            ]
            src_path = next((c for c in candidates if os.path.isfile(c)), None)
            dest_path = os.path.join(self.dest_folder, name)
            if src_path:
                try:
                    shutil.copy2(src_path, dest_path)
                    self.file_done.emit(name, True, "copiado")
                    ok += 1
                except Exception as e:
                    self.file_done.emit(name, False, str(e))
                    erros += 1
            else:
                self.file_done.emit(name, False, "arquivo não encontrado")
                erros += 1
            self.progress.emit(i + 1)
        self.finished.emit(ok, erros)


# Mapeamento de point format do LAS 1.4 (6-10) para formatos aceitos pelo LAS 1.2 (0-3).
# 6  -> 1  (básico + GPS time)
# 7  -> 3  (+ cor)
# 8  -> 3  (+ NIR — canal NIR é perdido, não existe em point format 3)
# 9  -> 1  (+ waveform — waveform é perdido, não suportado em 1.2)
# 10 -> 3  (cor + NIR + waveform — NIR e waveform são perdidos)
POINT_FORMAT_DOWNGRADE = {6: 1, 7: 3, 8: 3, 9: 1, 10: 3}


def _is_copc(las):
    """Detecta se um LasData carregado é um arquivo COPC (Cloud Optimized Point Cloud).

    COPC é uma variante hierárquica do LAZ 1.4 (VLR 'copc' + EVLR de hierarquia em
    octree). O laspy consegue LER esses arquivos normalmente, mas não sabe GRAVAR
    de volta os dois VLRs que carregam essa estrutura (CopcInfoVlr e
    CopcHierarchyVlr) — ambos lançam
    'NotImplementedError: Writing COPC is not supported' ao serem serializados.
    """
    try:
        for vlr in las.header.vlrs:
            if getattr(vlr, "user_id", "").strip().lower() == "copc":
                return True
        if las.header.evlrs:
            for vlr in las.header.evlrs:
                if getattr(vlr, "user_id", "").strip().lower() == "copc":
                    return True
    except Exception:
        pass
    return False


def _strip_copc(las):
    """Remove os VLRs/EVLRs de COPC (CopcInfoVlr e CopcHierarchyVlr) de um LasData
    recém-lido, para permitir gravá-lo normalmente.

    Depois do downgrade para 1.2 o arquivo deixa de ser COPC de qualquer forma —
    a organização hierárquica em octree só faz sentido dentro de um LAZ 1.4
    paginado no layout do COPC, então removê-la é seguro: os pontos em si (todas
    as dimensões, cor, tempo GPS etc.) são preservados normalmente, só se perde
    a indexação espacial/streaming por nível de detalhe.
    """
    def _sem_copc(vlrs):
        if not vlrs:
            return vlrs
        restantes = [v for v in vlrs if getattr(v, "user_id", "").strip().lower() != "copc"]
        return type(vlrs)(restantes)

    las.header.vlrs = _sem_copc(las.header.vlrs)
    las.header.evlrs = _sem_copc(las.header.evlrs)


def _set_raw_classification_byte(las, values):
    """Grava o BYTE INTEIRO de classificação de um LAS 1.2 (point format 0-3).

    No padrão LAS 1.2 esse byte é dividido em: bits 0-4 = classe (0-31),
    bit 5 = synthetic, bit 6 = key-point, bit 7 = withheld. O laspy, por isso,
    recusa valores > 31 em `las.classification`.

    Global Mapper e TerraScan, porém, usam o byte completo como classe (0-255)
    em arquivos 1.2. Escrevendo direto no campo cru ('raw_classification'),
    o valor de 8 bits é gravado exatamente como está.
    """
    values = np.asarray(values, dtype=np.uint8)
    arr = las.points.array
    names = arr.dtype.names or ()
    if "raw_classification" in names:
        arr["raw_classification"] = values
        return True
    # fallback para estruturas de ponto diferentes
    try:
        las.points["raw_classification"] = values
        return True
    except Exception:
        return False


class LazDowngradeWorker(QThread):
    progress  = Signal(int)
    file_done = Signal(str, bool, str)   # nome, ok, msg
    finished  = Signal(int, int)

    def __init__(self, files, dest_folder, add_suffix):
        super().__init__()
        self.files       = files
        self.dest_folder = dest_folder
        self.add_suffix  = add_suffix

    def run(self):
        ok = erros = 0
        for i, src in enumerate(self.files):
            try:
                las = laspy.read(str(src))
                versao = las.header.version

                if not (versao.major == 1 and versao.minor == 4):
                    self.file_done.emit(src.name, False,
                        f"ignorado — versão atual é {versao.major}.{versao.minor}, não 1.4")
                    erros += 1
                    self.progress.emit(i + 1)
                    continue

                # COPC não pode ser regravado pelo laspy tal como está; como o
                # arquivo perde a organização COPC ao virar LAS 1.2, removemos
                # esses VLRs. Os pontos não são afetados.
                era_copc = _is_copc(las)
                if era_copc:
                    _strip_copc(las)

                origem_pf  = las.header.point_format.id
                destino_pf = POINT_FORMAT_DOWNGRADE.get(origem_pf, 3)

                # Preserva o CRS (WKT no 1.4 -> GeoTIFF keys no 1.2)
                try:
                    crs = las.header.parse_crs()
                except Exception:
                    crs = None

                # ── Campos que estouram no LAS 1.2 ───────────────────────────
                # 1) Classificação: guarda o valor original de 8 bits (0-255).
                #    Para o laspy.convert não falhar, usa temporariamente o
                #    valor limitado a 31; o byte completo é regravado depois.
                classif_orig = np.asarray(las.classification).astype(np.uint8).copy()
                mask_class_alta = classif_orig > 31
                n_class_alta = int(np.count_nonzero(mask_class_alta))
                if n_class_alta:
                    las.classification = np.minimum(classif_orig, 31).astype(np.uint8)

                # 2) Return number / number of returns (4 bits -> 3 bits)
                n_ret_overflow = 0
                for campo in ("return_number", "number_of_returns"):
                    arr = np.asarray(las[campo])
                    mask = arr > 7
                    if mask.any():
                        n_ret_overflow += int(np.count_nonzero(mask))
                        las[campo] = np.minimum(arr, 7).astype(np.uint8)

                # 3) Scan angle: int16 (0.006°) -> scan_angle_rank int8 (graus)
                try:
                    graus = np.asarray(las.scan_angle, dtype=np.float64) * 0.006
                    scan_rank = np.clip(np.rint(graus), -90, 90).astype(np.int8)
                except Exception:
                    scan_rank = None

                # ── Conversão ────────────────────────────────────────────────
                convertido = laspy.convert(las, point_format_id=destino_pf, file_version="1.2")

                if scan_rank is not None:
                    try:
                        convertido.scan_angle_rank = scan_rank
                    except Exception:
                        pass

                # ── Mantém as classes originais (estilo Global Mapper) ───────
                # Nos pontos com classe > 31 grava o valor completo no byte de
                # classificação. Os demais pontos mantêm o byte já convertido
                # (classe + flags synthetic/key-point/withheld).
                classe_mantida = True
                if n_class_alta:
                    raw_atual = np.asarray(convertido.points.array["raw_classification"]).copy() \
                        if "raw_classification" in (convertido.points.array.dtype.names or ()) \
                        else None
                    if raw_atual is not None:
                        raw_atual[mask_class_alta] = classif_orig[mask_class_alta]
                        classe_mantida = _set_raw_classification_byte(convertido, raw_atual)
                    else:
                        classe_mantida = False

                if crs is not None:
                    try:
                        convertido.header.add_crs(crs)
                    except Exception:
                        pass

                nome_saida = src.stem + ("_v12" if self.add_suffix else "") + ".laz"
                dest_path = Path(self.dest_folder) / nome_saida

                try:
                    convertido.write(str(dest_path))
                except NotImplementedError as e:
                    self.file_done.emit(src.name, False,
                        f"ignorado — não foi possível gravar: {e}")
                    erros += 1
                    self.progress.emit(i + 1)
                    continue

                # ── Mensagem de log ──────────────────────────────────────────
                perdas = []
                if origem_pf in (8, 10):
                    perdas.append("NIR")
                if origem_pf in (9, 10):
                    perdas.append("waveform")
                if era_copc:
                    perdas.append("indexação COPC/octree")
                perda_txt = f"  (perdeu: {', '.join(perdas)})" if perdas else ""

                avisos = []
                if n_class_alta:
                    if classe_mantida:
                        avisos.append(
                            f"{n_class_alta} pts com classe >31 mantidos (byte completo, "
                            f"máx. {int(classif_orig.max())})")
                    else:
                        avisos.append(
                            f"{n_class_alta} pts com classe >31 NÃO puderam ser mantidos "
                            f"(limitados a 31)")
                if n_ret_overflow:
                    avisos.append(f"{n_ret_overflow} valores de retorno limitados a 7")
                aviso_txt = ("  |  " + "  |  ".join(avisos)) if avisos else ""

                self.file_done.emit(src.name, True,
                    f"1.4 → 1.2  |  formato {origem_pf} → {destino_pf}{perda_txt}{aviso_txt}")
                ok += 1
            except Exception as e:
                self.file_done.emit(src.name, False, str(e))
                erros += 1
            self.progress.emit(i + 1)
        self.finished.emit(ok, erros)


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS COMPARTILHADOS
# ══════════════════════════════════════════════════════════════════════════════

class DropList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, extensions=(".las", ".laz")):
        super().__init__()
        self.extensions = extensions
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setSpacing(1)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls(): e.acceptProposedAction()

    def dropEvent(self, e):
        paths = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in self.extensions:
                paths.append(p)
            elif p.is_dir():
                for f in p.rglob("*"):
                    if f.suffix.lower() in self.extensions:
                        paths.append(f)
        if paths:
            self.files_dropped.emit(paths)
        e.acceptProposedAction()


class StatCard(QFrame):
    def __init__(self, label, value="0", accent=False):
        super().__init__()
        self.setObjectName("statCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(3)

        color = C['accent'] if accent else C['text']
        self.val = QLabel(value)
        self.val.setFont(QFont("Segoe UI", 18, QFont.Bold))
        self.val.setStyleSheet(f"color: {color}; background: transparent;")
        self.val.setAlignment(Qt.AlignCenter)

        self.lbl = QLabel(label)
        self.lbl.setFont(QFont("Segoe UI", 8))
        self.lbl.setStyleSheet(f"color: {C['text3']}; background: transparent;")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)

        lay.addWidget(self.val)
        lay.addWidget(self.lbl)

    def set_value(self, v):
        self.val.setText(str(v))


def section_label(text):
    lbl = QLabel(text)
    lbl.setObjectName("sectionTitle")
    lbl.setFont(QFont("Segoe UI", 8, QFont.Bold))
    return lbl


def hint_label(text):
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    return lbl


def toggle_btn(text):
    b = QPushButton(text)
    b.setObjectName("toggleBtn")
    b.setCheckable(True)
    return b


def outline_btn(text, h=36):
    b = QPushButton(text)
    b.setObjectName("outlineBtn")
    b.setFixedHeight(h)
    return b


def danger_btn(text, h=36):
    b = QPushButton(text)
    b.setObjectName("dangerBtn")
    b.setFixedHeight(h)
    return b


def primary_btn(text, h=42, w=None):
    b = QPushButton(text)
    b.setObjectName("primaryBtn")
    b.setFixedHeight(h)
    if w:
        b.setMinimumWidth(w)
    return b


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 1 — RENOMEAR ARQUIVOS
# ══════════════════════════════════════════════════════════════════════════════

class RenameTab(QWidget):
    status_msg = Signal(str)

    def __init__(self):
        super().__init__()
        self._files = []
        self._mode  = ".laz"
        self._worker = None
        self._ok = self._err = 0
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = QHBoxLayout(); tb.setSpacing(8)

        tb.addWidget(section_label("CONVERTER PARA:"))
        self.b_laz = toggle_btn(".LAS → .LAZ"); self.b_laz.setChecked(True)
        self.b_las = toggle_btn(".LAZ → .LAS")
        self.b_laz.clicked.connect(lambda: self._set_mode(".laz"))
        self.b_las.clicked.connect(lambda: self._set_mode(".las"))
        tb.addWidget(self.b_laz); tb.addWidget(self.b_las)
        tb.addSpacing(16)

        sep = QFrame(); sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {C['border']}; max-width:1px;")
        tb.addWidget(sep); tb.addSpacing(8)

        self.btn_add = outline_btn("+ Adicionar")
        self.btn_dir = outline_btn("Pasta")
        self.btn_clr = danger_btn("Limpar")
        self.btn_add.clicked.connect(self._add_files)
        self.btn_dir.clicked.connect(self._add_folder)
        self.btn_clr.clicked.connect(self._clear)
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        tb.addStretch()
        lay.addLayout(tb)

        # ── Lista ─────────────────────────────────────────────────────────────
        lay.addWidget(hint_label("Arraste e solte arquivos .LAS ou .LAZ aqui"))
        self.list_w = DropList((".las", ".laz"))
        self.list_w.files_dropped.connect(self._on_drop)
        lay.addWidget(self.list_w, stretch=1)

        # ── Cards ─────────────────────────────────────────────────────────────
        cr = QHBoxLayout(); cr.setSpacing(10)
        self.c_total = StatCard("Total na fila")
        self.c_las   = StatCard("Arquivos .LAS")
        self.c_laz   = StatCard("Arquivos .LAZ")
        self.c_ok    = StatCard("Renomeados", accent=True)
        self.c_err   = StatCard("Erros")
        for c in (self.c_total, self.c_las, self.c_laz, self.c_ok, self.c_err):
            cr.addWidget(c)
        lay.addLayout(cr)

        # ── Progress + ações ──────────────────────────────────────────────────
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.hide()
        lay.addWidget(self.progress)

        br = QHBoxLayout(); br.setSpacing(8)
        self.btn_rem = outline_btn("Remover selecionados", 42)
        self.btn_rem.clicked.connect(self._remove_sel)
        self.btn_run = primary_btn("▶  Renomear arquivos", 42, 200)
        self.btn_run.clicked.connect(self._run)
        br.addWidget(self.btn_rem); br.addStretch(); br.addWidget(self.btn_run)
        lay.addLayout(br)

    # ── Lógica ────────────────────────────────────────────────────────────────

    def _set_mode(self, ext):
        self._mode = ext
        self.b_laz.setChecked(ext == ".laz")
        self.b_las.setChecked(ext == ".las")
        self._refresh()

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar arquivos", "", "Nuvem de pontos (*.las *.laz *.LAS *.LAZ)")
        self._add_paths([Path(p) for p in paths])

    def _add_folder(self):
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if f:
            self._add_paths([x for x in Path(f).rglob("*") if x.suffix.lower() in (".las", ".laz")])

    def _on_drop(self, paths): self._add_paths(paths)

    def _add_paths(self, paths):
        ex = {str(p) for p in self._files}
        added = 0
        for p in paths:
            if str(p) not in ex:
                self._files.append(p); ex.add(str(p)); added += 1
        if added:
            self._refresh()
            self.status_msg.emit(f"{added} arquivo(s) adicionado(s). Total: {len(self._files)}.")

    def _refresh(self):
        self.list_w.clear()
        las = laz = 0
        for p in self._files:
            ext = p.suffix.lower()
            if ext == ".las": las += 1
            else: laz += 1
            skip = ext == self._mode
            if skip:
                item = QListWidgetItem(f"  {p.name}  —  já é {self._mode[1:].upper()}")
                item.setForeground(QColor(C['text3']))
            else:
                item = QListWidgetItem(f"  {p.name}  →  {p.with_suffix(self._mode).name}")
            item.setData(Qt.UserRole, str(p))
            item.setToolTip(str(p))
            self.list_w.addItem(item)
        self.c_total.set_value(len(self._files))
        self.c_las.set_value(las); self.c_laz.set_value(laz)

    def _remove_sel(self):
        sel = {item.data(Qt.UserRole) for item in self.list_w.selectedItems()}
        self._files = [p for p in self._files if str(p) not in sel]
        self._refresh()
        self.status_msg.emit(f"{len(sel)} item(ns) removido(s).")

    def _clear(self):
        self._files.clear(); self.list_w.clear()
        for c in (self.c_total, self.c_las, self.c_laz, self.c_ok, self.c_err): c.set_value(0)
        self.status_msg.emit("Lista limpa.")

    def _run(self):
        targets = [p for p in self._files if p.suffix.lower() != self._mode]
        if not targets:
            QMessageBox.information(self, "Nada a fazer",
                f"Todos os arquivos já têm extensão {self._mode[1:].upper()}."); return
        if QMessageBox.question(self, "Confirmar",
            f"Renomear <b>{len(targets)}</b> arquivo(s) para <b>{self._mode[1:].upper()}</b>?<br>"
            "Esta ação modifica os arquivos no disco.",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return
        self.c_ok.set_value(0); self.c_err.set_value(0); self._ok = self._err = 0
        self.progress.setMaximum(len(targets)); self.progress.setValue(0); self.progress.show()
        self._busy(True)
        self._worker = RenameWorker(targets, self._mode)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.file_done.connect(self._on_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_done(self, original, resultado, sucesso):
        if sucesso:
            self._ok += 1; self.c_ok.set_value(self._ok)
            orig = Path(original)
            for i, p in enumerate(self._files):
                if p == orig: self._files[i] = Path(resultado); break
        else:
            self._err += 1; self.c_err.set_value(self._err)

    def _on_finished(self, ok, erros):
        self._busy(False); self.progress.hide(); self._refresh()
        if erros == 0:
            self.status_msg.emit(f"Concluído — {ok} arquivo(s) renomeado(s).")
            QMessageBox.information(self, "Concluído", f"<b>{ok}</b> arquivo(s) renomeado(s).")
        else:
            self.status_msg.emit(f"Concluído com erros — {ok} OK, {erros} erro(s).")
            QMessageBox.warning(self, "Concluído com erros",
                f"<b>{ok}</b> renomeado(s).<br><b>{erros}</b> com erro.")

    def _busy(self, busy):
        for w in (self.btn_run, self.btn_add, self.btn_dir,
                  self.btn_clr, self.b_laz, self.b_las): w.setEnabled(not busy)
        self.btn_run.setText("Processando…" if busy else "▶  Renomear arquivos")


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 2 — EDITAR PRJ (substituição de extensões no texto)
# ══════════════════════════════════════════════════════════════════════════════

class PrjEditTab(QWidget):
    status_msg = Signal(str)

    def __init__(self):
        super().__init__()
        self._files = []
        self._prj_mode = "las_to_laz"
        self._worker = None
        self._alter = self._err_cnt = self._n_las = self._n_laz = 0
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        # ── Opções ────────────────────────────────────────────────────────────
        opt = QFrame(); opt.setObjectName("section")
        ol = QHBoxLayout(opt); ol.setContentsMargins(16, 12, 16, 12); ol.setSpacing(10)

        ol.addWidget(section_label("SUBSTITUIÇÃO:"))
        self.bm1 = toggle_btn(".LAS → .LAZ"); self.bm1.setChecked(True)
        self.bm2 = toggle_btn(".LAZ → .LAS")
        self.bm3 = toggle_btn("Ambos")
        self.bm1.clicked.connect(lambda: self._set_mode("las_to_laz"))
        self.bm2.clicked.connect(lambda: self._set_mode("laz_to_las"))
        self.bm3.clicked.connect(lambda: self._set_mode("both"))
        for b in (self.bm1, self.bm2, self.bm3): ol.addWidget(b)

        sep = QFrame(); sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {C['border']}; max-width:1px; margin: 4px 8px;")
        ol.addWidget(sep)

        self.chk_bak = QCheckBox("Criar backup antes de salvar")
        self.chk_bak.setChecked(True)
        ol.addWidget(self.chk_bak)
        ol.addStretch()

        self.btn_add_p = outline_btn("+ PRJ")
        self.btn_dir_p = outline_btn("Pasta")
        self.btn_clr_p = danger_btn("Limpar")
        self.btn_add_p.clicked.connect(self._add_prj)
        self.btn_dir_p.clicked.connect(self._add_dir)
        self.btn_clr_p.clicked.connect(self._clear)
        for b in (self.btn_add_p, self.btn_dir_p, self.btn_clr_p): ol.addWidget(b)
        lay.addWidget(opt)

        # ── Splitter lista / preview ──────────────────────────────────────────
        spl = QSplitter(Qt.Horizontal)

        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0,0,0,0); ll.setSpacing(6)
        ll.addWidget(hint_label("Arquivos PRJ — arraste e solte"))
        self.prj_list = DropList((".prj",))
        self.prj_list.files_dropped.connect(self._on_drop)
        self.prj_list.currentItemChanged.connect(self._preview)
        ll.addWidget(self.prj_list)
        spl.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0,0,0,0); rl.setSpacing(6)
        rl.addWidget(hint_label("Pré-visualização do conteúdo"))
        self.preview_box = QTextEdit()
        self.preview_box.setObjectName("previewBox")
        self.preview_box.setReadOnly(True)
        self.preview_box.setFont(QFont("Consolas", 9))
        self.preview_box.setPlaceholderText("Selecione um PRJ para visualizar…")
        rl.addWidget(self.preview_box)
        spl.addWidget(right)
        spl.setSizes([320, 580])
        lay.addWidget(spl, stretch=1)

        # ── Cards ─────────────────────────────────────────────────────────────
        cr = QHBoxLayout(); cr.setSpacing(10)
        self.pc_total   = StatCard("PRJs na fila")
        self.pc_alter   = StatCard("Alterados", accent=True)
        self.pc_err     = StatCard("Erros")
        self.pc_las2laz = StatCard("Ref. .las→.laz")
        self.pc_laz2las = StatCard("Ref. .laz→.las")
        for c in (self.pc_total, self.pc_alter, self.pc_err,
                  self.pc_las2laz, self.pc_laz2las): cr.addWidget(c)
        lay.addLayout(cr)

        # ── Log ───────────────────────────────────────────────────────────────
        lay.addWidget(hint_label("Log de operações"))
        self.log_box = QTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 8))
        self.log_box.setFixedHeight(80)
        self.log_box.setStyleSheet(f"background: {C['logBg']}; border: 1px solid {C['border']}; border-radius: 6px;")
        self.log_box.setPlaceholderText("O resultado de cada PRJ aparecerá aqui…")
        lay.addWidget(self.log_box)

        # ── Progress + botão ──────────────────────────────────────────────────
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.hide()
        lay.addWidget(self.progress)

        br = QHBoxLayout(); br.setSpacing(8)
        self.btn_rem_p = outline_btn("Remover selecionados", 42)
        self.btn_rem_p.clicked.connect(self._remove_sel)
        self.btn_run_p = primary_btn("▶  Aplicar substituições", 42, 210)
        self.btn_run_p.clicked.connect(self._run)
        br.addWidget(self.btn_rem_p); br.addStretch(); br.addWidget(self.btn_run_p)
        lay.addLayout(br)

    # ── Lógica ────────────────────────────────────────────────────────────────

    def _set_mode(self, mode):
        self._prj_mode = mode
        self.bm1.setChecked(mode == "las_to_laz")
        self.bm2.setChecked(mode == "laz_to_las")
        self.bm3.setChecked(mode == "both")

    def _add_prj(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar PRJ", "", "TerraSolid/Spatix PRJ (*.prj *.PRJ)")
        self._add_paths([Path(p) for p in paths])

    def _add_dir(self):
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if f: self._add_paths(list(Path(f).rglob("*.prj")))

    def _on_drop(self, paths): self._add_paths(paths)

    def _add_paths(self, paths):
        ex = {str(p) for p in self._files}
        added = 0
        for p in paths:
            if str(p) not in ex:
                self._files.append(p); ex.add(str(p)); added += 1
        if added:
            self._refresh_list()
            self.status_msg.emit(f"{added} PRJ(s) adicionado(s). Total: {len(self._files)}.")

    def _refresh_list(self):
        self.prj_list.clear()
        for p in self._files:
            item = QListWidgetItem(f"  {p.name}")
            item.setData(Qt.UserRole, str(p))
            item.setToolTip(str(p))
            self.prj_list.addItem(item)
        self.pc_total.set_value(len(self._files))

    def _remove_sel(self):
        sel = {item.data(Qt.UserRole) for item in self.prj_list.selectedItems()}
        self._files = [p for p in self._files if str(p) not in sel]
        self._refresh_list(); self.preview_box.clear()
        self.status_msg.emit(f"{len(sel)} item(ns) removido(s).")

    def _clear(self):
        self._files.clear(); self.prj_list.clear(); self.preview_box.clear(); self.log_box.clear()
        for c in (self.pc_total, self.pc_alter, self.pc_err,
                  self.pc_las2laz, self.pc_laz2las): c.set_value(0)
        self.status_msg.emit("Lista limpa.")

    def _preview(self, current, _prev):
        if current is None: self.preview_box.clear(); return
        path = Path(current.data(Qt.UserRole))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.preview_box.setHtml(self._highlight(text))
        except Exception as e:
            self.preview_box.setPlainText(f"Erro: {e}")

    def _highlight(self, text):
        import html as _h
        safe = _h.escape(text)
        safe = re.sub(r'(?i)(Storage=)(LAS)(\d)',
            rf'\1<span style="background:#FEF3C7;color:#92400E;font-weight:bold">\2</span>\3', safe)
        safe = re.sub(r'(?i)(Storage=)(LAZ)(\d)',
            rf'\1<span style="background:#DBEAFE;color:#1E40AF;font-weight:bold">\2</span>\3', safe)
        safe = re.sub(r'(?i)(\.las)\b',
            r'<span style="background:#FEF3C7;color:#92400E;font-weight:bold">\1</span>', safe)
        safe = re.sub(r'(?i)(\.laz)\b',
            r'<span style="background:#DBEAFE;color:#1E40AF;font-weight:bold">\1</span>', safe)
        return f'<pre style="font-family:Consolas,monospace;font-size:9pt;white-space:pre-wrap">{safe}</pre>'

    def _run(self):
        if not self._files:
            QMessageBox.information(self, "Nada a fazer", "Adicione arquivos PRJ."); return
        modo_txt = {
            "las_to_laz": ".LAS → .LAZ",
            "laz_to_las": ".LAZ → .LAS",
            "both":       ".LAS ↔ .LAZ",
        }[self._prj_mode]
        bak_txt = "com backup" if self.chk_bak.isChecked() else "SEM backup"
        if QMessageBox.question(self, "Confirmar substituição",
            f"Aplicar substituição <b>{modo_txt}</b> em <b>{len(self._files)}</b> PRJ(s)?<br>"
            f"Modo: <b>{bak_txt}</b>.<br>Esta ação modifica arquivos no disco.",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return

        for c in (self.pc_alter, self.pc_err, self.pc_las2laz, self.pc_laz2las): c.set_value(0)
        self.log_box.clear()
        self._alter = self._err_cnt = self._n_las = self._n_laz = 0
        self.progress.setMaximum(len(self._files)); self.progress.setValue(0); self.progress.show()
        self._busy(True)
        self._worker = PrjEditWorker(self._files, self._prj_mode, self.chk_bak.isChecked())
        self._worker.progress.connect(self.progress.setValue)
        self._worker.file_done.connect(self._on_prj_done)
        self._worker.finished.connect(self._on_prj_finished)
        self._worker.start()

    def _on_prj_done(self, path, n_las, n_laz, ok, msg):
        name = Path(path).name
        if ok:
            if n_las > 0 or n_laz > 0:
                self._alter += 1; self.pc_alter.set_value(self._alter)
                self._n_las += n_las; self._n_laz += n_laz
                self.pc_las2laz.set_value(self._n_las); self.pc_laz2las.set_value(self._n_laz)
                color = C['green']; prefix = "✔"
            else:
                color = C['text3']; prefix = "—"
        else:
            self._err_cnt += 1; self.pc_err.set_value(self._err_cnt)
            color = C['danger']; prefix = "✖"
        self.log_box.append(f'<span style="color:{color}">{prefix}  {name}  —  {msg}</span>')

    def _on_prj_finished(self, ok, erros, n_las, n_laz):
        self._busy(False); self.progress.hide()
        cur = self.prj_list.currentItem()
        if cur: self._preview(cur, None)
        if erros == 0:
            self.status_msg.emit(f"Concluído — {ok} PRJ(s) alterado(s).")
            QMessageBox.information(self, "Concluído",
                f"<b>{ok}</b> PRJ(s) alterado(s).<br>"
                f".las→.laz: <b>{n_las}</b>  |  .laz→.las: <b>{n_laz}</b>")
        else:
            self.status_msg.emit(f"Concluído com erros — {ok} OK, {erros} erro(s).")
            QMessageBox.warning(self, "Concluído com erros",
                f"<b>{ok}</b> processado(s).<br><b>{erros}</b> com erro.")

    def _busy(self, busy):
        for w in (self.btn_run_p, self.btn_add_p, self.btn_dir_p,
                  self.btn_clr_p, self.bm1, self.bm2, self.bm3): w.setEnabled(not busy)
        self.btn_run_p.setText("Processando…" if busy else "▶  Aplicar substituições")


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 3 — COPIADOR PRJ  (lê arquivos do PRJ e copia para destino)
# ══════════════════════════════════════════════════════════════════════════════

class PrjCopyTab(QWidget):
    status_msg = Signal(str)

    def __init__(self):
        super().__init__()
        self._prj_path = None
        self._entries  = []        # lista de strings extraídas do PRJ
        self._worker   = None
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        # ── Passo 1: Pasta do PRJ ─────────────────────────────────────────────
        s1 = QFrame(); s1.setObjectName("section")
        l1 = QVBoxLayout(s1); l1.setContentsMargins(16, 12, 16, 12); l1.setSpacing(8)
        l1.addWidget(section_label("1. PASTA COM O ARQUIVO PRJ"))
        r1 = QHBoxLayout(); r1.setSpacing(8)
        self.prj_folder_edit = QLineEdit()
        self.prj_folder_edit.setReadOnly(True)
        self.prj_folder_edit.setPlaceholderText("Selecione a pasta onde estão os arquivos PRJ…")
        self.btn_pick_folder = outline_btn("Selecionar pasta", 34)
        self.btn_pick_folder.clicked.connect(self._pick_folder)
        r1.addWidget(self.prj_folder_edit, stretch=1)
        r1.addWidget(self.btn_pick_folder)
        l1.addLayout(r1)

        r1b = QHBoxLayout(); r1b.setSpacing(8)
        self.prj_combo = QComboBox()
        self.prj_combo.setFixedHeight(34)
        self.prj_combo.setPlaceholderText("Nenhum PRJ encontrado na pasta")
        self.btn_read_prj = primary_btn("Ler PRJ", 34, 120)
        self.btn_read_prj.clicked.connect(self._read_prj)
        r1b.addWidget(self.prj_combo, stretch=1)
        r1b.addWidget(self.btn_read_prj)
        l1.addLayout(r1b)
        lay.addWidget(s1)

        # ── Passo 2: Lista de folhas ──────────────────────────────────────────
        s2 = QFrame(); s2.setObjectName("section")
        l2 = QVBoxLayout(s2); l2.setContentsMargins(16, 12, 16, 12); l2.setSpacing(8)

        hl = QHBoxLayout()
        hl.addWidget(section_label("2. ARQUIVOS ENCONTRADOS NO PRJ"))
        hl.addStretch()
        self.count_lbl = QLabel("0 / 0 selecionados")
        self.count_lbl.setStyleSheet(f"color: {C['text3']}; font-size: 9px; background: transparent;")
        hl.addWidget(self.count_lbl)
        l2.addLayout(hl)

        tb2 = QHBoxLayout(); tb2.setSpacing(8)
        self.btn_sel_all = outline_btn("Selecionar todos", 30)
        self.btn_des_all = outline_btn("Desmarcar todos", 30)
        self.btn_sel_all.clicked.connect(lambda: (self.file_list.selectAll(), self._upd_count()))
        self.btn_des_all.clicked.connect(lambda: (self.file_list.clearSelection(), self._upd_count()))
        tb2.addWidget(self.btn_sel_all); tb2.addWidget(self.btn_des_all); tb2.addStretch()
        l2.addLayout(tb2)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setAlternatingRowColors(True)
        self.file_list.setFont(QFont("Consolas", 9))
        self.file_list.setMinimumHeight(160)
        self.file_list.itemSelectionChanged.connect(self._upd_count)
        l2.addWidget(self.file_list)
        lay.addWidget(s2, stretch=1)

        # ── Passo 3: Destino ──────────────────────────────────────────────────
        s3 = QFrame(); s3.setObjectName("section")
        l3 = QVBoxLayout(s3); l3.setContentsMargins(16, 12, 16, 12); l3.setSpacing(8)
        l3.addWidget(section_label("3. PASTA DE DESTINO"))
        r3 = QHBoxLayout(); r3.setSpacing(8)
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("Selecione onde os arquivos serão copiados…")
        self.btn_dest = outline_btn("Selecionar destino", 34)
        self.btn_dest.clicked.connect(self._pick_dest)
        r3.addWidget(self.dest_edit, stretch=1); r3.addWidget(self.btn_dest)
        l3.addLayout(r3)
        lay.addWidget(s3)

        # ── Progress + botão ──────────────────────────────────────────────────
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.hide()
        lay.addWidget(self.progress)

        br = QHBoxLayout(); br.setSpacing(8)
        self.copy_count_lbl = QLabel("")
        self.copy_count_lbl.setStyleSheet(f"color: {C['text3']}; font-size: 9px; background: transparent;")
        self.btn_copy = primary_btn("▶  Copiar arquivos selecionados", 42, 240)
        self.btn_copy.clicked.connect(self._copy)
        br.addWidget(self.copy_count_lbl); br.addStretch(); br.addWidget(self.btn_copy)
        lay.addLayout(br)

    # ── Lógica ────────────────────────────────────────────────────────────────

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecionar pasta do PRJ")
        if not folder: return
        self.prj_folder_edit.setText(folder)
        prjs = [f for f in os.listdir(folder) if f.lower().endswith(".prj")]
        self.prj_combo.clear()
        self.prj_combo.addItems(prjs)
        if prjs:
            self.status_msg.emit(f"{len(prjs)} PRJ(s) encontrado(s) na pasta.")
        else:
            self.status_msg.emit("Nenhum arquivo .PRJ encontrado na pasta selecionada.")

    def _read_prj(self):
        folder = self.prj_folder_edit.text()
        prj_name = self.prj_combo.currentText()
        if not folder or not prj_name:
            QMessageBox.warning(self, "Atenção", "Selecione a pasta e o arquivo PRJ primeiro.")
            return
        prj_path = os.path.join(folder, prj_name)
        self._prj_path = prj_path
        try:
            content = Path(prj_path).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            QMessageBox.critical(self, "Erro", f"Não foi possível ler o PRJ:\n{e}"); return

        matches = re.findall(r'[^\s\n\r\t"\'<>|*?]+\.la[sz]', content, re.IGNORECASE)
        seen = set(); unique = []
        for m in matches:
            key = os.path.basename(m).lower()
            if key not in seen:
                seen.add(key); unique.append(m.strip())

        self._entries = unique
        self.file_list.clear()
        for entry in unique:
            item = QListWidgetItem(f"  {entry}")
            item.setData(Qt.UserRole, entry)
            self.file_list.addItem(item)
        self.file_list.selectAll()
        self._upd_count()
        self.status_msg.emit(f"{len(unique)} arquivo(s) encontrado(s) no PRJ.")

    def _upd_count(self):
        total = self.file_list.count()
        sel   = len(self.file_list.selectedItems())
        self.count_lbl.setText(f"{sel} / {total} selecionados")
        self.copy_count_lbl.setText(f"{sel} arquivo(s) a copiar")

    def _pick_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecionar pasta de destino")
        if folder:
            self.dest_edit.setText(folder)
            self.status_msg.emit(f"Destino: {folder}")

    def _copy(self):
        sel_items = self.file_list.selectedItems()
        dest = self.dest_edit.text()
        src_folder = self.prj_folder_edit.text()
        if not sel_items:
            QMessageBox.warning(self, "Atenção", "Selecione ao menos um arquivo."); return
        if not dest:
            QMessageBox.warning(self, "Atenção", "Selecione a pasta de destino."); return
        if QMessageBox.question(self, "Confirmar cópia",
            f"Copiar <b>{len(sel_items)}</b> arquivo(s) para:<br><i>{dest}</i>?",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return

        entries = [item.data(Qt.UserRole) for item in sel_items]
        self.progress.setMaximum(len(entries)); self.progress.setValue(0); self.progress.show()
        self._busy(True)
        self._ok = self._err = 0
        self._not_found = []

        self._worker = CopyWorker(entries, src_folder, dest)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.file_done.connect(self._on_copy_done)
        self._worker.finished.connect(self._on_copy_finished)
        self._worker.start()

    def _on_copy_done(self, name, ok, msg):
        if ok:
            self._ok += 1
        else:
            self._err += 1
            self._not_found.append(f"{name}  ({msg})")
        self.status_msg.emit(f"Copiando… {self._ok + self._err}/{self.progress.maximum()}")

    def _on_copy_finished(self, ok, erros):
        self._busy(False); self.progress.hide()
        if erros == 0:
            self.status_msg.emit(f"Concluído — {ok} arquivo(s) copiado(s).")
            QMessageBox.information(self, "Concluído",
                f"<b>{ok}</b> arquivo(s) copiado(s) com sucesso.")
        else:
            detail = "\n".join(self._not_found)
            self.status_msg.emit(f"Concluído — {ok} OK, {erros} não encontrado(s)/erro(s).")
            QMessageBox.warning(self, "Concluído com avisos",
                f"<b>{ok}</b> copiado(s).<br>"
                f"<b>{erros}</b> arquivo(s) com problema:<br><pre>{detail}</pre>")

    def _busy(self, busy):
        for w in (self.btn_copy, self.btn_read_prj, self.btn_pick_folder,
                  self.btn_dest, self.btn_sel_all, self.btn_des_all): w.setEnabled(not busy)
        self.btn_copy.setText("Copiando…" if busy else "▶  Copiar arquivos selecionados")



# ══════════════════════════════════════════════════════════════════════════════
#  ABA 4 — NORMALIZAR .LAZ PARA .laz
# ══════════════════════════════════════════════════════════════════════════════

class LowercaseLazTab(QWidget):
    status_msg = Signal(str)

    def __init__(self):
        super().__init__()
        self._folder = None
        self._files = []
        self._worker = None
        self._ok = self._err = 0
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        section = QFrame()
        section.setObjectName("section")
        sl = QVBoxLayout(section)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(10)

        sl.addWidget(section_label("PASTA COM ARQUIVOS .LAZ"))

        row = QHBoxLayout()
        row.setSpacing(8)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText(
            "Selecione a pasta. Apenas arquivos terminados exatamente em .LAZ serão alterados."
        )
        self.btn_folder = outline_btn("Selecionar pasta", 36)
        self.btn_folder.clicked.connect(self._pick_folder)
        row.addWidget(self.folder_edit, stretch=1)
        row.addWidget(self.btn_folder)
        sl.addLayout(row)

        self.chk_subfolders = QCheckBox("Incluir subpastas")
        self.chk_subfolders.setChecked(False)
        self.chk_subfolders.stateChanged.connect(self._scan)
        sl.addWidget(self.chk_subfolders)

        lay.addWidget(section)

        lay.addWidget(hint_label(
            "Pré-visualização — somente .LAZ maiúsculo será renomeado para .laz minúsculo"
        ))
        self.file_list = QListWidget()
        self.file_list.setAlternatingRowColors(True)
        self.file_list.setFont(QFont("Consolas", 9))
        lay.addWidget(self.file_list, stretch=1)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.c_found = StatCard("Arquivos .LAZ encontrados")
        self.c_ok = StatCard("Renomeados", accent=True)
        self.c_err = StatCard("Erros")
        cards.addWidget(self.c_found)
        cards.addWidget(self.c_ok)
        cards.addWidget(self.c_err)
        lay.addLayout(cards)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        lay.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.btn_refresh = outline_btn("Atualizar lista", 42)
        self.btn_refresh.clicked.connect(self._scan)
        self.btn_run = primary_btn("▶  Trocar .LAZ por .laz", 42, 220)
        self.btn_run.clicked.connect(self._run)
        bottom.addWidget(self.btn_refresh)
        bottom.addStretch()
        bottom.addWidget(self.btn_run)
        lay.addLayout(bottom)

    def _pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Selecionar pasta com arquivos .LAZ"
        )
        if not folder:
            return
        self._folder = Path(folder)
        self.folder_edit.setText(folder)
        self._scan()

    def _scan(self):
        self._files = []
        self.file_list.clear()

        if not self._folder or not self._folder.exists():
            self.c_found.set_value(0)
            return

        iterator = self._folder.rglob("*") if self.chk_subfolders.isChecked() else self._folder.iterdir()

        # IMPORTANTE: comparação exata. .laz minúsculo é ignorado.
        self._files = sorted(
            [p for p in iterator if p.is_file() and p.suffix == ".LAZ"],
            key=lambda p: str(p).lower()
        )

        for p in self._files:
            dst_name = p.name[:-4] + ".laz"
            item = QListWidgetItem(f"  {p.name}  →  {dst_name}")
            item.setToolTip(str(p))
            self.file_list.addItem(item)

        self.c_found.set_value(len(self._files))
        self.c_ok.set_value(0)
        self.c_err.set_value(0)
        self.status_msg.emit(
            f"{len(self._files)} arquivo(s) com extensão .LAZ maiúscula encontrado(s)."
        )

    def _run(self):
        if not self._folder:
            QMessageBox.information(
                self, "Selecione uma pasta",
                "Selecione primeiro a pasta que contém os arquivos .LAZ."
            )
            return

        self._scan()

        if not self._files:
            QMessageBox.information(
                self, "Nada a fazer",
                "Não há arquivos com extensão .LAZ maiúscula nesta pasta."
            )
            return

        if QMessageBox.question(
            self,
            "Confirmar",
            f"Trocar a extensão de <b>{len(self._files)}</b> arquivo(s) "
            f"de <b>.LAZ</b> para <b>.laz</b>?<br><br>"
            "O nome do arquivo será mantido. Apenas as letras da extensão "
            "serão convertidas para minúsculas.",
            QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
            return

        self._ok = self._err = 0
        self.c_ok.set_value(0)
        self.c_err.set_value(0)
        self.progress.setMaximum(len(self._files))
        self.progress.setValue(0)
        self.progress.show()
        self._busy(True)

        self._worker = LowercaseLazWorker(self._files)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.file_done.connect(self._on_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_done(self, original, resultado, sucesso):
        if sucesso:
            self._ok += 1
            self.c_ok.set_value(self._ok)
        else:
            self._err += 1
            self.c_err.set_value(self._err)

    def _on_finished(self, ok, erros):
        self._busy(False)
        self.progress.hide()
        self._scan()
        self.c_ok.set_value(ok)
        self.c_err.set_value(erros)

        if erros == 0:
            self.status_msg.emit(
                f"Concluído — {ok} arquivo(s) alterado(s) de .LAZ para .laz."
            )
            QMessageBox.information(
                self, "Concluído",
                f"<b>{ok}</b> arquivo(s) alterado(s) de <b>.LAZ</b> para <b>.laz</b>."
            )
        else:
            self.status_msg.emit(
                f"Concluído com erros — {ok} OK, {erros} erro(s)."
            )
            QMessageBox.warning(
                self, "Concluído com erros",
                f"<b>{ok}</b> arquivo(s) alterado(s).<br>"
                f"<b>{erros}</b> arquivo(s) apresentaram erro."
            )

    def _busy(self, busy):
        self.btn_folder.setEnabled(not busy)
        self.btn_refresh.setEnabled(not busy)
        self.btn_run.setEnabled(not busy)
        self.chk_subfolders.setEnabled(not busy)
        self.btn_run.setText(
            "Processando…" if busy else "▶  Trocar .LAZ por .laz"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 5 — CONVERTER LAZ 1.4 → 1.2
# ══════════════════════════════════════════════════════════════════════════════

class ConvertVersionTab(QWidget):
    status_msg = Signal(str)

    def __init__(self):
        super().__init__()
        self._files = []
        self._worker = None
        self._ok = self._err = 0
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        if not LASPY_AVAILABLE:
            aviso = QLabel(
                "A biblioteca <b>laspy</b> não está instalada.<br>"
                "Instale com:  <code>pip install laspy[lazrs]</code>  e reinicie o programa."
            )
            aviso.setWordWrap(True)
            aviso.setStyleSheet(
                f"background: {C['dangerBg']}; color: {C['danger']}; "
                f"border: 1px solid {C['dangerBrd']}; border-radius: 8px; padding: 12px;"
            )
            lay.addWidget(aviso)

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = QHBoxLayout(); tb.setSpacing(8)
        tb.addWidget(section_label("ENTRADA (LAZ/LAS 1.4):"))
        self.btn_add = outline_btn("+ Adicionar")
        self.btn_dir = outline_btn("Pasta")
        self.btn_clr = danger_btn("Limpar")
        self.btn_add.clicked.connect(self._add_files)
        self.btn_dir.clicked.connect(self._add_folder)
        self.btn_clr.clicked.connect(self._clear)
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        tb.addStretch()
        lay.addLayout(tb)

        # ── Lista ─────────────────────────────────────────────────────────────
        lay.addWidget(hint_label("Arraste e solte arquivos .LAZ ou .LAS (versão 1.4) aqui"))
        self.list_w = DropList((".las", ".laz"))
        self.list_w.files_dropped.connect(self._on_drop)
        lay.addWidget(self.list_w, stretch=1)

        # ── Opções de destino ────────────────────────────────────────────────
        opt = QFrame(); opt.setObjectName("section")
        ol = QHBoxLayout(opt); ol.setContentsMargins(16, 12, 16, 12); ol.setSpacing(10)
        ol.addWidget(section_label("DESTINO:"))
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setPlaceholderText("Pasta onde os arquivos .laz (versão 1.2) serão salvos…")
        self.btn_dest = outline_btn("Selecionar pasta", 34)
        self.btn_dest.clicked.connect(self._pick_dest)
        ol.addWidget(self.dest_edit, stretch=1)
        ol.addWidget(self.btn_dest)
        self.chk_suffix = QCheckBox("Adicionar sufixo _v12 ao nome do arquivo")
        self.chk_suffix.setChecked(True)
        ol.addWidget(self.chk_suffix)
        lay.addWidget(opt)

        # ── Cards ─────────────────────────────────────────────────────────────
        cr = QHBoxLayout(); cr.setSpacing(10)
        self.c_total = StatCard("Total na fila")
        self.c_ok    = StatCard("Convertidos", accent=True)
        self.c_err   = StatCard("Erros")
        for c in (self.c_total, self.c_ok, self.c_err):
            cr.addWidget(c)
        lay.addLayout(cr)

        # ── Log ───────────────────────────────────────────────────────────────
        lay.addWidget(hint_label("Log de operações"))
        self.log_box = QTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setFont(QFont("Consolas", 8))
        self.log_box.setFixedHeight(90)
        self.log_box.setStyleSheet(f"background: {C['logBg']}; border: 1px solid {C['border']}; border-radius: 6px;")
        self.log_box.setPlaceholderText("O resultado de cada arquivo aparecerá aqui…")
        lay.addWidget(self.log_box)

        # ── Progress + ações ──────────────────────────────────────────────────
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.hide()
        lay.addWidget(self.progress)

        br = QHBoxLayout(); br.setSpacing(8)
        self.btn_rem = outline_btn("Remover selecionados", 42)
        self.btn_rem.clicked.connect(self._remove_sel)
        self.btn_run = primary_btn("▶  Converter para 1.2", 42, 200)
        self.btn_run.clicked.connect(self._run)
        if not LASPY_AVAILABLE:
            self.btn_run.setEnabled(False)
        br.addWidget(self.btn_rem); br.addStretch(); br.addWidget(self.btn_run)
        lay.addLayout(br)

    # ── Lógica ────────────────────────────────────────────────────────────────

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar arquivos", "", "Nuvem de pontos (*.las *.laz *.LAS *.LAZ)")
        self._add_paths([Path(p) for p in paths])

    def _add_folder(self):
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if f:
            self._add_paths([x for x in Path(f).rglob("*") if x.suffix.lower() in (".las", ".laz")])

    def _on_drop(self, paths): self._add_paths(paths)

    def _add_paths(self, paths):
        ex = {str(p) for p in self._files}
        added = 0
        for p in paths:
            if str(p) not in ex:
                self._files.append(p); ex.add(str(p)); added += 1
        if added:
            self._refresh()
            self.status_msg.emit(f"{added} arquivo(s) adicionado(s). Total: {len(self._files)}.")

    def _refresh(self):
        self.list_w.clear()
        for p in self._files:
            item = QListWidgetItem(f"  {p.name}")
            item.setData(Qt.UserRole, str(p))
            item.setToolTip(str(p))
            self.list_w.addItem(item)
        self.c_total.set_value(len(self._files))

    def _remove_sel(self):
        sel = {item.data(Qt.UserRole) for item in self.list_w.selectedItems()}
        self._files = [p for p in self._files if str(p) not in sel]
        self._refresh()
        self.status_msg.emit(f"{len(sel)} item(ns) removido(s).")

    def _clear(self):
        self._files.clear(); self.list_w.clear(); self.log_box.clear()
        for c in (self.c_total, self.c_ok, self.c_err): c.set_value(0)
        self.status_msg.emit("Lista limpa.")

    def _pick_dest(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecionar pasta de destino")
        if folder:
            self.dest_edit.setText(folder)
            self.status_msg.emit(f"Destino: {folder}")

    def _run(self):
        if not LASPY_AVAILABLE:
            QMessageBox.critical(self, "Dependência ausente",
                "Instale a biblioteca laspy para usar esta aba:\npip install laspy[lazrs]")
            return
        if not self._files:
            QMessageBox.information(self, "Nada a fazer", "Adicione arquivos LAZ/LAS 1.4."); return
        dest = self.dest_edit.text()
        if not dest:
            QMessageBox.warning(self, "Atenção", "Selecione a pasta de destino."); return

        if QMessageBox.question(self, "Confirmar conversão",
            f"Converter <b>{len(self._files)}</b> arquivo(s) de LAZ/LAS <b>1.4</b> para <b>1.2</b>?<br>"
            "Arquivos com point format 6-10 terão o formato de ponto ajustado "
            "(6→1, 7→3, 8→3, 9→1, 10→3); os canais NIR e waveform, quando existirem, "
            "serão perdidos por não existirem na versão 1.2. Arquivos COPC também "
            "são aceitos: a indexação em octree é removida, mas todos os pontos "
            "são preservados.<br><br>"
            "<b>Classificação:</b> os valores de classe (inclusive acima de 31) são "
            "mantidos, gravando o byte completo de classificação, como o Global Mapper "
            "e o TerraScan fazem.<br>"
            "Os arquivos de origem não são modificados.",
            QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes: return

        self.c_ok.set_value(0); self.c_err.set_value(0); self._ok = self._err = 0
        self.log_box.clear()
        self.progress.setMaximum(len(self._files)); self.progress.setValue(0); self.progress.show()
        self._busy(True)
        self._worker = LazDowngradeWorker(self._files, dest, self.chk_suffix.isChecked())
        self._worker.progress.connect(self.progress.setValue)
        self._worker.file_done.connect(self._on_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_done(self, name, ok, msg):
        if ok:
            self._ok += 1; self.c_ok.set_value(self._ok)
            color, prefix = C['green'], "✔"
        else:
            self._err += 1; self.c_err.set_value(self._err)
            color, prefix = C['danger'], "✖"
        self.log_box.append(f'<span style="color:{color}">{prefix}  {name}  —  {msg}</span>')

    def _on_finished(self, ok, erros):
        self._busy(False); self.progress.hide()
        if erros == 0:
            self.status_msg.emit(f"Concluído — {ok} arquivo(s) convertido(s) para 1.2.")
            QMessageBox.information(self, "Concluído", f"<b>{ok}</b> arquivo(s) convertido(s) para LAZ 1.2.")
        else:
            self.status_msg.emit(f"Concluído com erros — {ok} OK, {erros} erro(s).")
            QMessageBox.warning(self, "Concluído com erros",
                f"<b>{ok}</b> convertido(s).<br><b>{erros}</b> com erro (veja o log).")

    def _busy(self, busy):
        for w in (self.btn_run, self.btn_add, self.btn_dir, self.btn_clr,
                  self.btn_dest, self.chk_suffix): w.setEnabled(not busy)
        self.btn_run.setText("Convertendo…" if busy else "▶  Converter para 1.2")


# ══════════════════════════════════════════════════════════════════════════════
#  JANELA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LAS / LAZ Tools  —  TerraSolid / Spatix")
        self.setMinimumSize(1000, 700)
        self._build()
        self.setStyleSheet(STYLE)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Cabeçalho
        header = QFrame(); header.setObjectName("header"); header.setFixedHeight(66)
        hl = QHBoxLayout(header); hl.setContentsMargins(28, 0, 28, 0)
        tc = QVBoxLayout(); tc.setSpacing(2)
        t = QLabel("LAS / LAZ Tools"); t.setObjectName("headerTitle")
        t.setFont(QFont("Segoe UI", 15, QFont.Bold))
        s = QLabel("Renomeação de arquivos  ·  Edição de projetos PRJ  ·  Copiador de folhas  ·  .LAZ→.laz  ·  Conversão de versão")
        s.setObjectName("headerSub")
        tc.addWidget(t); tc.addWidget(s)
        hl.addLayout(tc); hl.addStretch()
        root.addWidget(header)

        # Abas
        self.tabs = QTabWidget(); self.tabs.setObjectName("mainTabs")
        self.tab1 = RenameTab()
        self.tab2 = PrjEditTab()
        self.tab3 = PrjCopyTab()
        self.tab4 = LowercaseLazTab()
        self.tab5 = ConvertVersionTab()
        self.tabs.addTab(self.tab1, "  Renomear Arquivos  ")
        self.tabs.addTab(self.tab2, "  Editar PRJ  ")
        self.tabs.addTab(self.tab3, "  Copiador PRJ  ")
        self.tabs.addTab(self.tab4, "  .LAZ → .laz  ")
        self.tabs.addTab(self.tab5, "  Converter LAZ 1.4→1.2  ")
        root.addWidget(self.tabs, stretch=1)

        self.sb = QStatusBar(); self.setStatusBar(self.sb)
        self.sb.showMessage("Pronto.")
        for t in (self.tab1, self.tab2, self.tab3, self.tab4, self.tab5):
            t.status_msg.connect(self.sb.showMessage)


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window,        QColor(C['bg']))
    pal.setColor(QPalette.WindowText,    QColor(C['text']))
    pal.setColor(QPalette.Base,          QColor(C['surface']))
    pal.setColor(QPalette.AlternateBase, QColor(C['altRow']))
    pal.setColor(QPalette.Button,        QColor(C['surface']))
    pal.setColor(QPalette.ButtonText,    QColor(C['text']))
    app.setPalette(pal)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()