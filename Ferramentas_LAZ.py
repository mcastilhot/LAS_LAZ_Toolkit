#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LAS / LAZ Toolkit  (TerraScan / TerraModeler / Spatix)  -- v2
==============================================================

Aplicativo único (PySide6) que reúne:

    1. Metadados          - nome da folha e nº de pontos (lê só o cabeçalho)
    2. Editar PRJ         - troca .las/.laz dentro de projetos PRJ (com backup)
    3. Copiador PRJ       - copia os arquivos listados em um PRJ
    4. .LAZ -> .laz       - corrige extensão maiúscula
    5. Converter Versão   - conversão bidirecional LAS/LAZ 1.4 ↔ 1.2
    6. Conversor LAS → LAZ- conversão e compressão inteligente em streaming com relatório CSV
    7. Exportar Classes   - reclassificação e filtro de classes (macros TerraScan)
    8. Pontos-Chave (LAS) - equivalente à macro FnScanClassifyModelKey
    9. GeoTIFF (Lattice)  - equivalente ao "Export Lattice Model" 
                            (requer: pip install pyvista matplotlib)

Melhorias da v2:
    - Cancelamento real do pool de processos.
    - Leitura em chunks (filtra classe/bbox sem carregar a folha inteira).
    - Afinamento de pontos-chave vetorizado.
    - Código de reunião de vizinhos consolidado.
    - Leitura LAZ com backend explícito em todo o programa.
    - Renomear não sobrescreve destino; PRJ "Ambos" faz troca única.
    - Saída de pontos-chave opcionalmente em .laz; limpeza de QThread.

Dependências: veja requirements.txt
    - Abas 1-6 precisam apenas de PySide6, numpy e laspy (+ lazrs p/ .LAZ).
    - Abas 7-9 precisam também de scipy, rasterio, shapely, scikit-image
      e geopandas. Se faltarem, essas abas mostram um aviso e o resto
      do programa continua funcionando.
"""

from __future__ import annotations

import csv
import html as _html
import importlib.util
import multiprocessing
import os
import re
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

# --- Windows + PyInstaller: prioriza as DLLs empacotadas no executável ---------
# Evita conflito com outras cópias de Qt/GDAL/VC++ que existam no PATH do
# computador (QGIS, Anaconda, OSGeo4W, softwares de CAD...). Precisa vir ANTES
# do primeiro import do PySide6.
if sys.platform == "win32" and getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _dlls = [_base, os.path.join(_base, "PySide6")]
    for _root, _dirs, _ in os.walk(_base):
        for _d in _dirs:
            if _d.endswith(".libs") or _d in ("PySide6", "rasterio", "pyproj", "shapely", "scipy"):
                _dlls.append(os.path.join(_root, _d))
    _dlls = list(dict.fromkeys(_dlls))
    os.environ["PATH"] = os.pathsep.join(_dlls) + os.pathsep + os.environ.get("PATH", "")
    for _d in _dlls:
        if os.path.isdir(_d):
            try:
                os.add_dll_directory(_d)
            except Exception:
                pass

    for _proj in [
        os.path.join(_base, "pyproj", "proj_dir", "share", "proj"),
        os.path.join(_base, "share", "proj"),
        os.path.join(_base, "proj_data"),
    ]:
        if os.path.isdir(_proj):
            os.environ.setdefault("PROJ_DATA", _proj)
            os.environ.setdefault("PROJ_LIB", _proj)
            break

    for _gdal in [
        os.path.join(_base, "rasterio", "gdal_data"),
        os.path.join(_base, "share", "gdal"),
        os.path.join(_base, "gdal_data"),
    ]:
        if os.path.isdir(_gdal):
            os.environ.setdefault("GDAL_DATA", _gdal)
            break

from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal, QUrl, QSettings
from PySide6.QtGui import QColor, QFontDatabase, QTextCharFormat, QTextCursor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QListWidgetItem,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# --------------------------------------------------------------------------
# Dependências opcionais
# --------------------------------------------------------------------------
try:
    import laspy
    try:
        from laspy.compression import LazBackend
    except Exception:
        LazBackend = getattr(laspy, "LazBackend", None)
    LASPY_AVAILABLE = True
except ImportError:
    laspy = None  # type: ignore
    LazBackend = None
    LASPY_AVAILABLE = False

try:
    import scipy
    from scipy.spatial import Delaunay, cKDTree
    import rasterio
    import rasterio.io
    import rasterio.windows
    from rasterio.transform import from_origin
    from rasterio.merge import merge as rasterio_merge
    from shapely.geometry import box
    from shapely.strtree import STRtree
    GEO_AVAILABLE = True
    GEO_IMPORT_ERROR = ""
except Exception as _exc:  # pragma: no cover
    GEO_AVAILABLE = False
    GEO_IMPORT_ERROR = str(_exc)


# ==========================================================================
#  LEITURA DE LAS/LAZ (compartilhado)
# ==========================================================================

def _has_laz_backend() -> bool:
    for mod in ("lazrs", "laszip"):
        try:
            __import__(mod)
            return True
        except Exception:
            pass
    return False


def _laz_backends() -> list:
    return [b for b in (laspy.LazBackend.Lazrs, laspy.LazBackend.Laszip) if b.is_available()]


def _open_reader(path):
    """laspy.open com backend LAZ explícito (leitura consistente em todo o programa)."""
    path = Path(path)
    if path.suffix.lower() == ".laz":
        backends = _laz_backends()
        if not backends:
            raise RuntimeError(
                "Nenhum backend de descompressão LAZ instalado. "
                "Rode: pip install laspy[lazrs]  (ou laspy[laszip])"
            )
        return laspy.open(str(path), laz_backend=backends)
    return laspy.open(str(path))


def _abrir_las(path: Path) -> "laspy.LasData":
    """Abre um LAS/LAZ e retorna o LasData completo (todos os atributos)."""
    with _open_reader(path) as f:
        return f.read()


def _ler_bbox_header(path: Path) -> tuple[float, float, float, float]:
    """Lê só o header (rápido) e devolve (xmin, ymin, xmax, ymax)."""
    with _open_reader(path) as f:
        h = f.header
        return (float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]), float(h.maxs[1]))


def _ler_xyz_filtrado(path, classes, bbox=None, chunk: int = 1_000_000):
    """
    Lê um LAS/LAZ em CHUNKS e devolve só o XYZ das classes pedidas
    (opcionalmente dentro de `bbox` = (xmin, ymin, xmax, ymax)).

    Nunca materializa a nuvem inteira com todos os atributos: o pico de memória
    é ~um chunk + os pontos já filtrados. Se o bbox do header não intersecta
    `bbox`, o arquivo nem é lido (só o cabeçalho).

    Retorna (xyz[N,3], bbox_do_header).
    """
    alvo = np.asarray(sorted(set(int(c) for c in classes)))
    with _open_reader(path) as f:
        h = f.header
        bb_h = (float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]), float(h.maxs[1]))
        if bbox is not None and (
            bb_h[2] < bbox[0] or bb_h[0] > bbox[2] or bb_h[3] < bbox[1] or bb_h[1] > bbox[3]
        ):
            return np.empty((0, 3), dtype=np.float64), bb_h

        partes = []
        for pts in f.chunk_iterator(chunk):
            m = np.isin(np.asarray(pts.classification), alvo)
            if not m.any():
                continue
            x = np.asarray(pts.x)
            y = np.asarray(pts.y)
            if bbox is not None:
                m &= (x >= bbox[0]) & (x <= bbox[2]) & (y >= bbox[1]) & (y <= bbox[3])
                if not m.any():
                    continue
            partes.append(np.column_stack([x[m], y[m], np.asarray(pts.z)[m]]))

    xyz = np.concatenate(partes, axis=0) if partes else np.empty((0, 3), dtype=np.float64)
    return xyz, bb_h


# ==========================================================================
#  LÓGICA DAS ABAS 1-6 (rodam em thread; comunicam via emit(tipo, *args))
# ==========================================================================

# ── 1. Metadados ──────────────────────────────────────────────────────────
def work_metadata(files, emit):
    laz_ok = _has_laz_backend()
    total = len(files)
    for idx, path in enumerate(files, start=1):
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".laz" and not laz_ok:
                emit("warn", f"Sem backend para .LAZ (instale 'lazrs' ou 'laszip'). Pulando: {path}")
            else:
                with _open_reader(path) as reader:
                    pontos = int(getattr(reader.header, "point_count", 0))
                folha = os.path.splitext(os.path.basename(path))[0]
                emit("row", folha, path, pontos)
        except Exception as ex:
            emit("error", f"Erro ao processar: {path}\n{ex}\n{traceback.format_exc()}")
        emit("progress", idx, total)
    emit("done")



# ── 5. .LAZ -> .laz ───────────────────────────────────────────────────────
def work_lowercase_laz(files, emit):
    ok = erros = 0
    for i, src in enumerate(files):
        dst = src.with_name(src.name[:-4] + ".laz")
        try:
            # No Windows, mudar só maiúsculas/minúsculas exige etapa intermediária
            if src.name != dst.name and src.name.lower() == dst.name.lower():
                temp = src.with_name(src.name + ".__tmp_case__")
                src.rename(temp)
                temp.rename(dst)
            else:
                src.rename(dst)
            emit("file", str(src), str(dst), True)
            ok += 1
        except Exception as e:
            emit("file", str(src), str(e), False)
            erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


# ── 3. Editar PRJ ─────────────────────────────────────────────────────────
def prj_replace(text, mode):
    """
    Troca las<->laz no PRJ preservando maiúsculas/minúsculas.
    No modo "both" a troca é feita em UMA passada (swap) -- antes, a segunda
    passada desfazia a primeira e tudo acabava como .las.
    """
    n_las = n_laz = 0
    de = {"las_to_laz": "las", "laz_to_las": "laz", "both": "la[sz]"}[mode]

    def sub(m):
        nonlocal n_las, n_laz
        tag = m.group("tag") or m.group("tag2")
        if tag.lower() == "las":
            n_las += 1
            novo = "laz"
        else:
            n_laz += 1
            novo = "las"
        return novo.upper() if tag.isupper() else novo.lower()

    pat = rf"(?i)(?:(?<=Storage=)(?P<tag>{de})(?=\d)|(?<=\.)(?P<tag2>{de})\b)"
    text = re.sub(pat, sub, text)
    return text, n_las, n_laz


def work_prj_edit(files, mode, backup, emit):
    files_ok = files_err = total_las = total_laz = 0
    for i, prj in enumerate(files):
        try:
            text = prj.read_text(encoding="utf-8", errors="replace")
            new_text, n_las, n_laz = prj_replace(text, mode)
            if new_text == text:
                emit("file", str(prj), 0, 0, True, "sem alterações")
            else:
                if backup:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    shutil.copy2(prj, prj.with_suffix(f".bak_{ts}"))
                prj.write_text(new_text, encoding="utf-8")
                total_las += n_las
                total_laz += n_laz
                emit("file", str(prj), n_las, n_laz, True,
                     f"{n_las} .las→.laz  |  {n_laz} .laz→.las")
                files_ok += 1
        except Exception as e:
            emit("file", str(prj), 0, 0, False, str(e))
            files_err += 1
        emit("progress", i + 1)
    emit("done", files_ok, files_err, total_las, total_laz)


# ── 4. Copiador PRJ ───────────────────────────────────────────────────────
def work_copy(entries, src_folder, dest_folder, emit):
    ok = erros = 0
    for i, entry in enumerate(entries):
        name = os.path.basename(entry)
        candidates = [entry, os.path.join(src_folder, entry), os.path.join(src_folder, name)]
        src_path = next((c for c in candidates if os.path.isfile(c)), None)
        dest_path = os.path.join(dest_folder, name)
        if src_path:
            try:
                shutil.copy2(src_path, dest_path)
                emit("file", name, True, "copiado")
                ok += 1
            except Exception as e:
                emit("file", name, False, str(e))
                erros += 1
        else:
            emit("file", name, False, "arquivo não encontrado")
            erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


# ── 6. Conversão LAS/LAZ 1.4 -> 1.2 ───────────────────────────────────────
# 6->1, 7->3, 8->3 (perde NIR), 9->1 (perde waveform), 10->3 (perde NIR e waveform)
POINT_FORMAT_DOWNGRADE = {6: 1, 7: 3, 8: 3, 9: 1, 10: 3}
POINT_FORMAT_UPGRADE = {0: 6, 1: 6, 2: 7, 3: 7, 4: 9, 5: 10}


def _eh_copc(vlr):
    uid = str(getattr(vlr, "user_id", "")).strip().lower()
    desc = str(getattr(vlr, "description", "")).strip().lower()
    record_id = getattr(vlr, "record_id", None)
    return uid == "copc" or "copc" in uid or "copc" in desc or (uid == "entwine" and record_id == 1)


def _is_copc(las):
    try:
        for vlr in getattr(las.header, "vlrs", []):
            if _eh_copc(vlr):
                return True
        for vlr in getattr(las.header, "evlrs", []) or []:
            if _eh_copc(vlr):
                return True
    except Exception:
        pass
    return False


def _strip_copc(las):
    def _sem_copc(vlrs):
        if not vlrs:
            return vlrs
        restantes = [v for v in vlrs if not _eh_copc(v)]
        return type(vlrs)(restantes)
    las.header.vlrs = _sem_copc(las.header.vlrs)
    if hasattr(las.header, "evlrs") and las.header.evlrs:
        las.header.evlrs = _sem_copc(las.header.evlrs)


def _get_raw_classes(points) -> np.ndarray:
    rec = getattr(points, "points", points)
    arr = getattr(rec, "array", None)
    if arr is not None and "raw_classification" in (arr.dtype.names or ()):
        return np.asarray(arr["raw_classification"], dtype=np.uint8)
    return np.asarray(points.classification, dtype=np.uint8)


def _set_raw_classes(points, values: np.ndarray) -> None:
    values = np.asarray(values, dtype=np.uint8)
    rec = getattr(points, "points", points)
    arr = getattr(rec, "array", None)
    if arr is not None and "raw_classification" in (arr.dtype.names or ()):
        arr["raw_classification"] = values
    else:
        try:
            points.classification = values
        except Exception:
            if np.any(values > 31):
                points.classification = np.minimum(values, 31)
            else:
                points.classification = values


def _set_raw_classification_byte(las, values):
    """Grava o byte INTEIRO de classificação do LAS 1.2 (como Global Mapper/TerraScan),
    permitindo classes de 0 a 255 em vez do limite de 31 (5 bits) do padrão."""
    values = np.asarray(values, dtype=np.uint8)
    rec = getattr(las, "points", las)
    arr = getattr(rec, "array", None)
    if arr is not None and "raw_classification" in (arr.dtype.names or ()):
        arr["raw_classification"] = values
        return True
    try:
        rec["raw_classification"] = values
        return True
    except Exception:
        return False



def _write_converted_las(convertido, src: Path, dest_folder: str, suffix_tag: str,
                         add_suffix: bool, overwrite_original: bool) -> Optional[str]:
    """Grava o LAS/LAZ convertido; se overwrite_original=True, usa arquivo temporário e substitui atomicamente."""
    if overwrite_original:
        dest_path = src
        tmp_path = src.with_name(f".{src.stem}_tmp_conv{src.suffix}")
        target_write = tmp_path
    else:
        nome_saida = src.stem + (suffix_tag if add_suffix else "") + ".laz"
        dest_path = Path(dest_folder) / nome_saida
        try:
            if dest_path.resolve() == src.resolve():
                return (
                    f"ignorado — destino coincide com origem "
                    f"(ative o sufixo {suffix_tag}, marque Sobrescrever original ou escolha outra pasta)"
                )
        except Exception:
            pass
        target_write = dest_path

    try:
        laz_bk = None
        if str(target_write).lower().endswith(".laz") and LazBackend is not None:
            av = LazBackend.detect_available()
            laszip_bk = getattr(LazBackend, "Laszip", None)
            if laszip_bk in av:
                laz_bk = laszip_bk
        if laz_bk is not None:
            convertido.write(str(target_write), laz_backend=laz_bk)
        else:
            convertido.write(str(target_write))
    except NotImplementedError as e:
        if overwrite_original and target_write.exists():
            target_write.unlink(missing_ok=True)
        return f"ignorado — não foi possível gravar: {e}"
    except Exception:
        if overwrite_original and target_write.exists():
            target_write.unlink(missing_ok=True)
        raise

    if overwrite_original:
        os.replace(str(tmp_path), str(dest_path))

    return None


def work_convert_version(files, dest_folder, mode, add_suffix, overwrite_original, emit):
    """
    Converte arquivos LAS/LAZ entre versões:
      - mode == '1.4->1.2': Downgrade para LAS 1.2 (compatível com softwares legados/TerraScan)
      - mode == '1.2->1.4': Upgrade para LAS 1.4 (modernização, 256 classes nativas, precisão 0.006°)
    """
    ok = erros = 0
    is_downgrade = (mode == "1.4->1.2")

    for i, src in enumerate(files):
        try:
            las = laspy.read(str(src), laz_backend=_laz_backends() or None) \
                if src.suffix.lower() == ".laz" else laspy.read(str(src))
            versao = las.header.version

            if is_downgrade:
                if not (versao.major == 1 and versao.minor == 4):
                    emit("file", src.name, False,
                         f"ignorado — versão atual é {versao.major}.{versao.minor}, esperado 1.4")
                    erros += 1
                    emit("progress", i + 1)
                    continue

                era_copc = _is_copc(las)
                if era_copc:
                    _strip_copc(las)

                origem_pf = las.header.point_format.id
                destino_pf = POINT_FORMAT_DOWNGRADE.get(origem_pf, origem_pf if origem_pf in (0, 1, 2, 3) else 3)

                try:
                    crs = las.header.parse_crs()
                except Exception:
                    crs = None

                # Classificação: guarda original (8 bits); limita temporariamente a 31
                # só para o laspy.convert não falhar.
                classif_orig = _get_raw_classes(las).copy()
                mask_alta = classif_orig > 31
                n_class_alta = int(np.count_nonzero(mask_alta))
                if n_class_alta:
                    las.classification = np.minimum(classif_orig, 31).astype(np.uint8)

                # Return number / number of returns (4 bits → 3 bits)
                n_ret_overflow = 0
                for campo in ("return_number", "number_of_returns"):
                    try:
                        arr = np.asarray(las[campo])
                        m = arr > 7
                        if m.any():
                            n_ret_overflow += int(np.count_nonzero(m))
                            las[campo] = np.minimum(arr, 7).astype(np.uint8)
                    except Exception:
                        pass

                # Scan angle int16 (0.006°) → scan_angle_rank int8 (graus)
                try:
                    graus = np.asarray(las.scan_angle, dtype=np.float64) * 0.006
                    scan_rank = np.clip(np.rint(graus), -90, 90).astype(np.int8)
                except Exception:
                    scan_rank = None

                convertido = laspy.convert(las, point_format_id=destino_pf, file_version="1.2")

                if scan_rank is not None:
                    try:
                        convertido.scan_angle_rank = scan_rank
                    except Exception:
                        pass

                # Mantém as classes originais gravando o byte completo
                classe_mantida = True
                if n_class_alta:
                    classe_mantida = _set_raw_classification_byte(convertido, classif_orig)

                if crs is not None:
                    try:
                        convertido.header.add_crs(crs)
                    except Exception:
                        pass

                if era_copc:
                    _strip_copc(convertido)

                erro_msg = _write_converted_las(convertido, src, dest_folder, "_v12", add_suffix, overwrite_original)
                if erro_msg:
                    emit("file", src.name, False, erro_msg)
                    erros += 1
                    emit("progress", i + 1)
                    continue

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
                        avisos.append(f"{n_class_alta} pts com classe >31 mantidos "
                                      f"(byte completo, máx. {int(classif_orig.max())})")
                    else:
                        avisos.append(f"{n_class_alta} pts com classe >31 NÃO puderam ser mantidos (limitados a 31)")
                if n_ret_overflow:
                    avisos.append(f"{n_ret_overflow} valores de retorno limitados a 7")
                aviso_txt = ("  |  " + "  |  ".join(avisos)) if avisos else ""

                emit("file", src.name, True,
                     f"1.4 → 1.2  |  formato {origem_pf} → {destino_pf}{perda_txt}{aviso_txt}")
                ok += 1

            else:
                # Upgrade 1.2 / 1.3 -> 1.4
                if versao.major == 1 and versao.minor == 4:
                    emit("file", src.name, False,
                         f"ignorado — versão atual já é 1.4")
                    erros += 1
                    emit("progress", i + 1)
                    continue

                era_copc = _is_copc(las)
                if era_copc:
                    _strip_copc(las)

                origem_pf = las.header.point_format.id
                destino_pf = POINT_FORMAT_UPGRADE.get(origem_pf, origem_pf if origem_pf in (6, 7, 8, 9, 10) else 6)

                try:
                    crs = las.header.parse_crs()
                except Exception:
                    crs = None

                # Classificação: salva classes originais (byte completo, 0-255)
                classif_orig = _get_raw_classes(las).copy()

                # Scan angle rank (int8, graus) → scan angle (int16, passos de 0.006°)
                try:
                    if hasattr(las, "scan_angle_rank"):
                        scan_deg = np.asarray(las.scan_angle_rank, dtype=np.float64)
                        scan_14 = np.clip(np.round(scan_deg / 0.006), -30000, 30000).astype(np.int16)
                    else:
                        scan_14 = None
                except Exception:
                    scan_14 = None

                convertido = laspy.convert(las, point_format_id=destino_pf, file_version="1.4")

                # No LAS 1.4, classification é campo uint8 nativo com 256 classes
                try:
                    convertido.classification = classif_orig
                except Exception:
                    pass

                if scan_14 is not None:
                    try:
                        convertido.scan_angle = scan_14
                    except Exception:
                        pass

                if crs is not None:
                    try:
                        convertido.header.add_crs(crs)
                    except Exception:
                        pass

                if era_copc:
                    _strip_copc(convertido)

                erro_msg = _write_converted_las(convertido, src, dest_folder, "_v14", add_suffix, overwrite_original)
                if erro_msg:
                    emit("file", src.name, False, erro_msg)
                    erros += 1
                    emit("progress", i + 1)
                    continue

                max_c = int(classif_orig.max()) if len(classif_orig) else 0
                info_c = f"  |  classe máx: {max_c}" if max_c > 31 else ""
                emit("file", src.name, True,
                     f"{versao.major}.{versao.minor} → 1.4  |  formato {origem_pf} → {destino_pf}{info_c}")
                ok += 1

        except Exception as e:
            emit("file", src.name, False, str(e))
            erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


def work_downgrade(files, dest_folder, add_suffix, emit):
    return work_convert_version(files, dest_folder, "1.4->1.2", add_suffix, False, emit)

# ==========================================================================
#  EXPORTAR E RECLASSIFICAR CLASSES (macros TerraScan FnScanClassifyClass / FnScanOutput)
# ==========================================================================

def _clean_header_copc(header: laspy.LasHeader) -> laspy.LasHeader:
    """
    Remove VLRs/EVLRs de octree exclusivos do COPC para permitir que o laspy
    grave o arquivo como um LAS/LAZ convencional legível em qualquer software.
    Preserva CRS, VLRs de projeção, escalas e offsets.
    """
    novo = laspy.LasHeader(
        point_format=header.point_format,
        version=header.version,
    )
    novo.scales = header.scales
    novo.offsets = header.offsets
    novo.generating_software = "LAS Toolkit (Python)"

    def _eh_copc(vlr):
        uid = str(getattr(vlr, "user_id", "")).strip().lower()
        desc = str(getattr(vlr, "description", "")).strip().lower()
        record_id = getattr(vlr, "record_id", None)
        return uid == "copc" or "copc" in uid or "copc" in desc or (uid == "entwine" and record_id == 1)

    for vlr in getattr(header, "vlrs", []):
        if not _eh_copc(vlr):
            try:
                novo.vlrs.append(vlr)
            except Exception:
                pass

    if getattr(header, "evlrs", None):
        for evlr in header.evlrs:
            if not _eh_copc(evlr):
                try:
                    novo.evlrs.append(evlr)
                except Exception:
                    pass

    return novo


def parse_class_ranges(texto: str) -> list[int]:
    """Interpreta números e intervalos separados por vírgula (ex.: '0-6,8,11,13-17,63,105-107')."""
    res = set()
    for parte in texto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "-" in parte:
            tokens = parte.split("-")
            if len(tokens) == 2 and tokens[0].strip().isdigit() and tokens[1].strip().isdigit():
                ini, fim = int(tokens[0]), int(tokens[1])
                res.update(range(min(ini, fim), max(ini, fim) + 1))
        elif parte.isdigit():
            res.add(int(parte))
    return sorted(res)


def parse_terrascan_macro(caminho: Path) -> tuple[list[tuple[int, int]], str]:
    """Lê um arquivo .mac do TerraScan e extrai regras FnScanClassifyClass e FnScanOutput."""
    conteudo = caminho.read_text(encoding="utf-8", errors="replace")
    reclass = [
        (int(de), int(para))
        for de, para in re.findall(r"FnScanClassifyClass\s*\(\s*(\d+)\s*,\s*(\d+)", conteudo)
    ]
    m = re.search(r'FnScanOutput\s*\(\s*"[^"]*"\s*,\s*"([^"]*)"', conteudo)
    classes_export = m.group(1).strip() if m else ""
    return reclass, classes_export


def work_export_classes(
    arquivos: list[Path],
    pasta_destino: str,
    reclass_rules: list[tuple[int, int]],
    classes_export: list[int],
    formato_saida: str,
    sufixo: str,
    emit,
) -> None:
    dest_dir = Path(pasta_destino)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Tabela de lookup para reclassificação (0..255)
    map_class = np.arange(256, dtype=np.uint8)
    for de, para in reclass_rules:
        if 0 <= de <= 255 and 0 <= para <= 255:
            map_class[de] = para

    classes_filtro = set(classes_export) if classes_export else None

    ok = 0
    erros = 0

    for i, src in enumerate(arquivos):
        nome = src.name
        try:
            if formato_saida == "las":
                ext = ".las"
            elif formato_saida == "laz":
                ext = ".laz"
            else:
                ext = src.suffix.lower()

            nome_saida = f"{src.stem}{sufixo}{ext}"
            dest_path = dest_dir / nome_saida

            if dest_path.resolve() == src.resolve():
                emit("file", nome, False, "ignorado — destino coincide com origem (adicione um sufixo ou use outra pasta)")
                erros += 1
                emit("progress", i + 1)
                continue

            with _open_reader(src) as reader:
                header = _clean_header_copc(reader.header)
                laz_backends = _laz_backends() or None

                is_laz = dest_path.suffix.lower() == ".laz"
                writer_kw = {"laz_backend": laz_backends} if is_laz else {}

                pts_lidos_arq = 0
                pts_gravados_arq = 0

                with laspy.open(str(dest_path), mode="w", header=header, **writer_kw) as writer:
                    for chunk in reader.chunk_iterator(1_000_000):
                        n_chunk = len(chunk)
                        pts_lidos_arq += n_chunk

                        # 1. Aplica reclassificação
                        c_orig = _get_raw_classes(chunk)
                        c_novo = map_class[c_orig]
                        _set_raw_classes(chunk, c_novo)

                        # 2. Aplica filtro de exportação
                        if classes_filtro is not None:
                            mask = np.isin(c_novo, list(classes_filtro))
                            if mask.any():
                                chunk_sel = chunk[mask]
                                writer.write_points(chunk_sel)
                                pts_gravados_arq += int(np.count_nonzero(mask))
                        else:
                            writer.write_points(chunk)
                            pts_gravados_arq += n_chunk

            ok += 1
            emit(
                "file",
                nome,
                True,
                f"gravado ({pts_gravados_arq:,} de {pts_lidos_arq:,} pontos)",
            )

        except Exception as exc:
            erros += 1
            emit("file", nome, False, f"erro: {exc}")

        emit("progress", i + 1)

    emit("done", ok, erros)


# ==========================================================================
#  LÓGICA DAS ABAS 7-9 (processamento geoespacial)
# ==========================================================================

# ── 7. Pontos-Chave (LAS), equivalente à macro FnScanClassifyModelKey ──────

@dataclass
class ProcessParams:
    laz_path: str
    output_dir: str
    ground_classes: list[int] = field(default_factory=lambda: [2])
    resolution: float = 1.0
    thin_tolerance: float = 0.05          # tolerância vertical do afinamento (m)
    thin_cell_size: float = 0.5           # tamanho de célula usado no afinamento (m)
    export_las: bool = True               # LAS com pontos-chave (igual ao FnScanOutput)
    model_key_class: int = 8              # classe ASPRS "Model Key-point"
    export_geotiff: bool = False
    export_ascii: bool = False
    saida_laz: bool = False               # grava .laz em vez de .las
    epsg: Optional[int] = None


@dataclass
class ProcessResult:
    laz_path: str
    ok: bool
    message: str = ""
    n_pontos_solo: int = 0
    n_pontos_afinados: int = 0
    n_col: int = 0
    n_lin: int = 0
    tempo_s: float = 0.0
    las_path: str = ""
    geotiff_path: str = ""
    ascii_path: str = ""


def _thin_model_key_points_indices(xyz: np.ndarray, cell_size: float, tolerance: float) -> np.ndarray:
    """
    Aproxima o 'Model Key Points' do TerraScan e retorna os ÍNDICES (relativos
    a `xyz`) dos pontos mantidos, para remapear de volta ao registro LAS
    completo (intensidade, GPS time, RGB...). Versão VETORIZADA.

    Em cada célula da grade mantém:
      - o ponto mais baixo; e
      - todo ponto cujo resíduo em relação à média da célula excede a tolerância.

    Não é bit-a-bit igual ao algoritmo proprietário do TerraScan, mas produz
    uma seleção equivalente dentro da tolerância informada.
    """
    n = xyz.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.int64)

    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    ix = np.floor((x - x.min()) / cell_size).astype(np.int64)
    iy = np.floor((y - y.min()) / cell_size).astype(np.int64)
    cell_id = iy * (int(ix.max()) + 1) + ix

    ordem = np.argsort(cell_id, kind="stable")
    cid = cell_id[ordem]
    z_ord = z[ordem]

    inicio = np.flatnonzero(np.concatenate(([True], cid[1:] != cid[:-1])))
    contagem = np.diff(np.concatenate((inicio, [n])))
    grupo = np.repeat(np.arange(inicio.size), contagem)

    z_min = np.minimum.reduceat(z_ord, inicio)
    z_med = np.add.reduceat(z_ord, inicio) / contagem

    # primeiro ponto que atinge o mínimo em cada célula
    pos_min = np.flatnonzero(z_ord == z_min[grupo])
    _, primeiro = np.unique(grupo[pos_min], return_index=True)
    manter = np.zeros(n, dtype=bool)
    manter[pos_min[primeiro]] = True

    # pontos acima da tolerância vertical
    manter |= np.abs(z_ord - z_med[grupo]) > tolerance

    return np.sort(ordem[manter])


def process_block(params: ProcessParams) -> ProcessResult:
    """Processa uma única folha LAZ: le -> filtra solo -> afina -> LAS (+ grid opcional)."""
    t0 = time.time()
    laz_path = Path(params.laz_path)
    nome_base = laz_path.stem

    try:
        las = _abrir_las(laz_path)

        classificacao = np.asarray(las.classification)
        mascara_solo = np.isin(classificacao, params.ground_classes)
        n_pontos_solo = int(mascara_solo.sum())

        if n_pontos_solo < 3:
            return ProcessResult(
                laz_path=str(laz_path),
                ok=False,
                message=f"Menos de 3 pontos nas classes {sorted(params.ground_classes)}, pulando.",
            )

        indices_solo = np.flatnonzero(mascara_solo)
        xyz_solo = np.column_stack(
            [
                np.asarray(las.x)[indices_solo],
                np.asarray(las.y)[indices_solo],
                np.asarray(las.z)[indices_solo],
            ]
        )

        indices_afinado_local = _thin_model_key_points_indices(
            xyz_solo, cell_size=params.thin_cell_size, tolerance=params.thin_tolerance
        )
        n_pontos_afinados = indices_afinado_local.shape[0]

        if n_pontos_afinados < 3:
            return ProcessResult(
                laz_path=str(laz_path),
                ok=False,
                message="Pontos insuficientes após afinamento.",
                n_pontos_solo=n_pontos_solo,
            )

        indices_finais = indices_solo[indices_afinado_local]
        xyz_afinado = xyz_solo[indices_afinado_local]

        out_dir = Path(params.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        las_path_saida = ""
        geotiff_path = ""
        ascii_path = ""
        n_col = 0
        n_lin = 0

        if params.export_las:
            ext_saida = "laz" if params.saida_laz else "las"
            las_path_saida = str(out_dir / f"{nome_base}.{ext_saida}")

            # O arquivo de entrada pode ser COPC. O laspy consegue ler COPC,
            # mas não consegue ESCREVER COPC. Portanto, a saída é
            # deliberadamente um LAS/LAZ convencional.
            #
            # Não copiamos os VLRs/EVLRs estruturais do COPC para o novo
            # cabeçalho. Os atributos dos pontos (X/Y/Z, intensidade, RGB,
            # GPS time etc.) continuam sendo preservados através de
            # `las.points[indices_finais].copy()`.

            header_saida = laspy.LasHeader(
                point_format=las.header.point_format,
                version=las.header.version,
            )
            header_saida.scales = las.header.scales
            header_saida.offsets = las.header.offsets
            header_saida.generating_software = "LAS Toolkit (Python)"

            # Copia VLRs de usuário que não sejam relacionados ao COPC.
            for vlr in las.header.vlrs:
                user_id = str(getattr(vlr, "user_id", "")).strip().lower()
                description = str(getattr(vlr, "description", "")).strip().lower()

                eh_copc = (
                    user_id == "copc"
                    or "copc" in user_id
                    or "copc" in description
                )

                if not eh_copc:
                    try:
                        header_saida.vlrs.append(vlr)
                    except Exception:
                        # Um VLR incompatível não deve impedir a exportação.
                        pass

            las_saida = laspy.LasData(header_saida)

            # Preserva o registro completo dos pontos selecionados.
            las_saida.points = las.points[indices_finais].copy()

            # Reclassifica os pontos selecionados como Model Key-point.
            las_saida.classification = np.full(
                len(indices_finais),
                params.model_key_class,
                dtype=las_saida.classification.dtype,
            )

            las_saida.write(las_path_saida, do_compress=params.saida_laz)

        if params.export_geotiff or params.export_ascii:
            res = params.resolution
            x_min = np.floor(xyz_afinado[:, 0].min() / res) * res
            x_max = np.ceil(xyz_afinado[:, 0].max() / res) * res
            y_min = np.floor(xyz_afinado[:, 1].min() / res) * res
            y_max = np.ceil(xyz_afinado[:, 1].max() / res) * res

            n_col = max(int(round((x_max - x_min) / res)), 1)
            n_lin = max(int(round((y_max - y_min) / res)), 1)

            cols_centros = x_min + (np.arange(n_col) + 0.5) * res
            lins_centros = y_max - (np.arange(n_lin) + 0.5) * res
            grid_x, grid_y = np.meshgrid(cols_centros, lins_centros)

            tri = Delaunay(xyz_afinado[:, :2])
            interp_simplex = tri.find_simplex(np.column_stack([grid_x.ravel(), grid_y.ravel()]))

            grid_z = np.full(grid_x.size, np.nan, dtype=np.float32)
            dentro = interp_simplex >= 0

            if dentro.any():
                pontos_validos = np.column_stack([grid_x.ravel()[dentro], grid_y.ravel()[dentro]])
                simplices = tri.simplices[interp_simplex[dentro]]
                transform = tri.transform[interp_simplex[dentro]]
                b0 = pontos_validos - transform[:, 2]
                bary = np.einsum("ijk,ik->ij", transform[:, :2], b0)
                bary_completo = np.column_stack([bary, 1 - bary.sum(axis=1)])
                z_simplex = xyz_afinado[simplices, 2]
                grid_z[dentro] = np.sum(bary_completo * z_simplex, axis=1).astype(np.float32)

            grid_z = grid_z.reshape((n_lin, n_col))

            if (~dentro).any():
                arvore = cKDTree(xyz_afinado[:, :2])
                faltantes = np.column_stack([grid_x.ravel()[~dentro], grid_y.ravel()[~dentro]])
                _, idx_prox = arvore.query(faltantes, k=1)
                grid_z_flat = grid_z.ravel()
                grid_z_flat[~dentro] = xyz_afinado[idx_prox, 2].astype(np.float32)
                grid_z = grid_z_flat.reshape((n_lin, n_col))

            epsg = params.epsg

            if params.export_geotiff:
                geotiff_path = str(out_dir / f"{nome_base}.tif")
                transform_raster = from_origin(x_min, y_max, res, res)
                perfil = {
                    "driver": "GTiff",
                    "height": n_lin,
                    "width": n_col,
                    "count": 1,
                    "dtype": "float32",
                    "crs": f"EPSG:{epsg}" if epsg else None,
                    "transform": transform_raster,
                    "nodata": -9999.0,
                    "compress": "lzw",
                }
                grid_saida = np.nan_to_num(grid_z, nan=-9999.0)
                with rasterio.open(geotiff_path, "w", **perfil) as dst:
                    dst.write(grid_saida, 1)

            if params.export_ascii:
                ascii_path = str(out_dir / f"{nome_base}.asc")
                grid_saida_asc = np.nan_to_num(grid_z, nan=-9999.0)
                with open(ascii_path, "w") as fasc:
                    fasc.write(f"ncols {n_col}\n")
                    fasc.write(f"nrows {n_lin}\n")
                    fasc.write(f"xllcorner {x_min}\n")
                    fasc.write(f"yllcorner {y_min}\n")
                    fasc.write(f"cellsize {res}\n")
                    fasc.write("NODATA_value -9999\n")
                    np.savetxt(fasc, grid_saida_asc, fmt="%.3f")

        return ProcessResult(
            laz_path=str(laz_path),
            ok=True,
            message="OK",
            n_pontos_solo=n_pontos_solo,
            n_pontos_afinados=n_pontos_afinados,
            n_col=n_col,
            n_lin=n_lin,
            tempo_s=time.time() - t0,
            las_path=las_path_saida,
            geotiff_path=geotiff_path,
            ascii_path=ascii_path,
        )

    except Exception as exc:  # noqa: BLE001
        return ProcessResult(
            laz_path=str(laz_path),
            ok=False,
            message=f"Erro: {exc}\n{traceback.format_exc(limit=2)}",
        )


# ── 8. GeoTIFF (Lattice Model) ────────────────────────────────────────────

@dataclass
class LatticeParams:
    laz_path: str
    vizinhos_paths: list[str]
    output_dir: str
    classes: list[int]
    expand_by: float = 1.0
    grid_spacing: float = 1.0
    max_triangle: float = 200.0
    model_buffer: float = 100.0
    outside_z: float = -99999.0
    criar_tfw: bool = True
    extensao: str = "tif"
    epsg: Optional[int] = None


@dataclass
class LatticeResult:
    laz_path: str
    ok: bool
    message: str = ""
    n_pontos_tin: int = 0
    n_vizinhos_usados: int = 0
    n_col: int = 0
    n_lin: int = 0
    tempo_s: float = 0.0
    tif_path: str = ""
    tfw_path: str = ""
    # bbox nominal da folha (sem o "expand_by"), usado para recortar exatamente
    # no limite ao montar o mosaico geral, evitando sobreposição entre folhas
    x_min_nom: float = 0.0
    y_min_nom: float = 0.0
    x_max_nom: float = 0.0
    y_max_nom: float = 0.0


def _construir_indice_vizinhos(arquivos: list[Path], model_buffer: float) -> dict[str, list[str]]:
    """
    Lê só os headers (rápido) e monta, para cada folha, a lista de folhas
    vizinhas cujo bbox intersecta o bbox da folha expandido pelo Model buffer.
    Usa STRtree (shapely) para não fazer busca O(n²) em pastas grandes.
    """
    bboxes: dict[str, tuple[float, float, float, float]] = {}
    for arq in arquivos:
        try:
            bboxes[str(arq)] = _ler_bbox_header(arq)
        except Exception:
            continue

    caminhos = list(bboxes.keys())
    geometrias = [box(*bboxes[c]) for c in caminhos]
    arvore = STRtree(geometrias)

    vizinhos_por_arquivo: dict[str, list[str]] = {}
    for chave in caminhos:
        x_min, y_min, x_max, y_max = bboxes[chave]
        area_busca = box(
            x_min - model_buffer, y_min - model_buffer, x_max + model_buffer, y_max + model_buffer
        )
        indices_candidatos = arvore.query(area_busca)
        vizinhos = [
            caminhos[i]
            for i in indices_candidatos
            if caminhos[i] != chave and geometrias[i].intersects(area_busca)
        ]
        vizinhos_por_arquivo[chave] = vizinhos

    return vizinhos_por_arquivo


def process_lattice_block(params: LatticeParams) -> LatticeResult:
    """Processa uma folha: filtra classes -> TIN (com pontos de vizinhos no buffer) -> GeoTIFF."""
    t0 = time.time()
    laz_path = Path(params.laz_path)
    nome_base = laz_path.stem
    classes_alvo = set(params.classes)

    try:
        # Rotina compartilhada: leitura em chunks, só as classes pedidas, e só
        # os vizinhos que realmente intersectam o buffer.
        xyz_total, (x_min_nom, y_min_nom, x_max_nom, y_max_nom), n_vizinhos_usados = (
            _reunir_pontos_buffer(
                laz_path, params.vizinhos_paths, classes_alvo, params.model_buffer
            )
        )

        if xyz_total.shape[0] < 3:
            return LatticeResult(
                laz_path=str(laz_path),
                ok=False,
                message=(
                    f"Menos de 3 pontos nas classes {sorted(classes_alvo)} "
                    "(verifique a lista de classes)."
                ),
            )

        # --- extensão de saída: bbox nominal (própria folha) + Expand by, alinhado ao Grid spacing ---
        res = params.grid_spacing
        x_min_saida = np.floor((x_min_nom - params.expand_by) / res) * res
        x_max_saida = np.ceil((x_max_nom + params.expand_by) / res) * res
        y_min_saida = np.floor((y_min_nom - params.expand_by) / res) * res
        y_max_saida = np.ceil((y_max_nom + params.expand_by) / res) * res

        n_col = max(int(round((x_max_saida - x_min_saida) / res)), 1)
        n_lin = max(int(round((y_max_saida - y_min_saida) / res)), 1)

        cols_centros = x_min_saida + (np.arange(n_col) + 0.5) * res
        lins_centros = y_max_saida - (np.arange(n_lin) + 0.5) * res
        grid_x, grid_y = np.meshgrid(cols_centros, lins_centros)
        pontos_grid = np.column_stack([grid_x.ravel(), grid_y.ravel()])

        tri = Delaunay(xyz_total[:, :2])
        simplex_idx = tri.find_simplex(pontos_grid)

        grid_z = np.full(pontos_grid.shape[0], params.outside_z, dtype=np.float32)
        dentro = simplex_idx >= 0

        if dentro.any():
            simplices = tri.simplices[simplex_idx[dentro]]

            # comprimento da maior aresta de cada triângulo (filtro "Max triangle")
            v0 = xyz_total[simplices[:, 0], :2]
            v1 = xyz_total[simplices[:, 1], :2]
            v2 = xyz_total[simplices[:, 2], :2]
            maior_aresta = np.maximum(
                np.maximum(np.linalg.norm(v0 - v1, axis=1), np.linalg.norm(v1 - v2, axis=1)),
                np.linalg.norm(v2 - v0, axis=1),
            )
            valido_triangulo = maior_aresta <= params.max_triangle

            transform = tri.transform[simplex_idx[dentro]]
            pts_dentro = pontos_grid[dentro]
            b0 = pts_dentro - transform[:, 2]
            bary = np.einsum("ijk,ik->ij", transform[:, :2], b0)
            bary_completo = np.column_stack([bary, 1 - bary.sum(axis=1)])
            z_simplex = xyz_total[simplices, 2]
            z_interp = np.sum(bary_completo * z_simplex, axis=1).astype(np.float32)

            indices_dentro_global = np.flatnonzero(dentro)
            indices_validos = indices_dentro_global[valido_triangulo]
            grid_z[indices_validos] = z_interp[valido_triangulo]
            # pontos em triângulos maiores que max_triangle ficam com outside_z
            # (sem preenchimento por vizinho mais próximo -- é assim que o
            # Export Lattice Model do TerraModeler se comporta: vira vazio)

        grid_z = grid_z.reshape((n_lin, n_col))

        out_dir = Path(params.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        extensao = params.extensao.lstrip(".")
        tif_path = str(out_dir / f"{nome_base}.{extensao}")

        transform_raster = from_origin(x_min_saida, y_max_saida, res, res)
        perfil = {
            "driver": "GTiff",
            "height": n_lin,
            "width": n_col,
            "count": 1,
            "dtype": "float32",
            "crs": f"EPSG:{params.epsg}" if params.epsg else None,
            "transform": transform_raster,
            "nodata": params.outside_z,
        }
        with rasterio.open(tif_path, "w", **perfil) as dst:
            dst.write(grid_z, 1)

        tfw_path = ""
        if params.criar_tfw:
            tfw_path = str(out_dir / f"{nome_base}.tfw")
            with open(tfw_path, "w") as ftfw:
                ftfw.write(
                    f"{res}\n0.0\n0.0\n{-res}\n"
                    f"{x_min_saida + res / 2}\n{y_max_saida - res / 2}\n"
                )

        return LatticeResult(
            laz_path=str(laz_path),
            ok=True,
            message="OK",
            n_pontos_tin=int(xyz_total.shape[0]),
            n_vizinhos_usados=n_vizinhos_usados,
            n_col=n_col,
            n_lin=n_lin,
            tempo_s=time.time() - t0,
            tif_path=tif_path,
            tfw_path=tfw_path,
            x_min_nom=x_min_nom,
            y_min_nom=y_min_nom,
            x_max_nom=x_max_nom,
            y_max_nom=y_max_nom,
        )

    except Exception as exc:  # noqa: BLE001
        return LatticeResult(
            laz_path=str(laz_path),
            ok=False,
            message=f"Erro: {exc}\n{traceback.format_exc(limit=2)}",
        )


def _gerar_mosaico_geral(
    tifs_com_bbox: list[tuple[str, tuple[float, float, float, float]]],
    output_path: str,
    outside_z: float,
    criar_tfw: bool,
) -> tuple[int, int]:
    """
    Junta os GeoTIFFs individuais em um único GeoTIFF "geral".

    Cada folha é recortada exatamente no seu limite NOMINAL (sem o "expand_by")
    antes da junção -- ou seja, a faixa que cada folha exportou a mais (o
    'Expand by'/'Model buffer') é descartada no mosaico. Isso evita a emenda
    visível nas bordas: sem esse recorte, folhas vizinhas se sobrepõem numa
    faixa onde cada uma foi triangulada de forma independente, o que gera
    pequenas diferenças de cota bem na sobreposição. Recortando no limite
    nominal, cada pixel do mosaico vem de uma única folha.
    """
    datasets_recortados = []
    try:
        for tif_path, (x_min_nom, y_min_nom, x_max_nom, y_max_nom) in tifs_com_bbox:
            with rasterio.open(tif_path) as src:
                janela = rasterio.windows.from_bounds(
                    x_min_nom, y_min_nom, x_max_nom, y_max_nom, transform=src.transform
                ).round_offsets().round_lengths()
                # protege contra bbox nominal levemente fora do raster (arredondamentos)
                janela = janela.intersection(
                    rasterio.windows.Window(0, 0, src.width, src.height)
                )
                if janela.width <= 0 or janela.height <= 0:
                    continue

                dados_recorte = src.read(window=janela)
                transform_recorte = src.window_transform(janela)

                perfil_recorte = src.profile.copy()
                perfil_recorte.update(
                    {
                        "height": int(janela.height),
                        "width": int(janela.width),
                        "transform": transform_recorte,
                    }
                )

                memfile = rasterio.io.MemoryFile()
                ds_recorte = memfile.open(**perfil_recorte)
                ds_recorte.write(dados_recorte)
                datasets_recortados.append((memfile, ds_recorte))

        if not datasets_recortados:
            raise RuntimeError("Nenhuma folha válida para montar o mosaico.")

        mosaico, transform_mosaico = rasterio_merge(
            [ds for _, ds in datasets_recortados], nodata=outside_z
        )

        perfil = datasets_recortados[0][1].profile.copy()
        perfil.update(
            {
                "height": mosaico.shape[1],
                "width": mosaico.shape[2],
                "transform": transform_mosaico,
                "nodata": outside_z,
                "compress": "lzw",
            }
        )
        with rasterio.open(output_path, "w", **perfil) as dst:
            dst.write(mosaico)

        if criar_tfw:
            res_x = transform_mosaico.a
            res_y = transform_mosaico.e  # negativo
            x_origem = transform_mosaico.c
            y_origem = transform_mosaico.f
            tfw_path = str(Path(output_path).with_suffix(".tfw"))
            with open(tfw_path, "w") as ftfw:
                ftfw.write(
                    f"{res_x}\n0.0\n0.0\n{res_y}\n"
                    f"{x_origem + res_x / 2}\n{y_origem + res_y / 2}\n"
                )

        return mosaico.shape[2], mosaico.shape[1]
    finally:
        for memfile, ds_recorte in datasets_recortados:
            ds_recorte.close()
            memfile.close()


# ── 9. Curvas de Nível (Contours) ─────────────────────────────────────────

@dataclass
class ContourParams:
    laz_path: str
    vizinhos_paths: list[str]
    output_dir: str
    prefixo: str
    classes: list[int]
    model_buffer: float
    resolucao_grid: float
    draw_basic: bool
    draw_major: bool
    draw_minor: bool
    basic_interval: float
    major_interval: float
    minor_interval: float
    smoothing: bool
    thin: bool
    thin_deviation: float
    use_min_area: bool
    hill_area: float
    dep_area: float
    label_decimals: int
    formato_saida: str
    ajustar_bordas: bool
    epsg: Optional[int] = None


@dataclass
class ContourResult:
    laz_path: str
    ok: bool
    message: str = ""
    n_pontos_tin: int = 0
    n_vizinhos_usados: int = 0
    n_linhas: int = 0
    tempo_s: float = 0.0
    curvas_path: str = ""


def carregar_configuracoes_set(caminho: str) -> dict:
    """
    Lê um arquivo .set de configuração de curvas do TerraModeler (formato
    'chave=valor') e devolve um dicionário bruto (strings).
    """
    config: dict[str, str] = {}
    with open(caminho, "r", encoding="latin-1") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("[") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            config[chave.strip()] = valor.strip()
    return config


def _reunir_pontos_buffer(
    laz_path: Path, vizinhos_paths: list[str], classes: set[int], buffer: float
) -> tuple[np.ndarray, tuple[float, float, float, float], int]:
    """
    Pontos da própria folha + pontos de folhas vizinhas dentro do buffer,
    para a triangulação não ter descontinuidade perto da borda.
    Leitura em chunks e filtrada por classe/bbox (memória baixa).
    """
    xyz_alvo, bbox_nominal = _ler_xyz_filtrado(laz_path, classes)
    x_min_nom, y_min_nom, x_max_nom, y_max_nom = bbox_nominal
    bbox_busca = (x_min_nom - buffer, y_min_nom - buffer, x_max_nom + buffer, y_max_nom + buffer)

    pontos = [xyz_alvo]
    n_vizinhos_usados = 0
    for caminho_vizinho in vizinhos_paths:
        try:
            xyz_v, _ = _ler_xyz_filtrado(caminho_vizinho, classes, bbox=bbox_busca)
        except Exception:
            continue
        if xyz_v.shape[0] > 0:
            pontos.append(xyz_v)
            n_vizinhos_usados += 1

    xyz_total = np.concatenate(pontos, axis=0) if len(pontos) > 1 else pontos[0]
    return xyz_total, bbox_nominal, n_vizinhos_usados


def _suavizar_chaikin(coords: np.ndarray, iteracoes: int = 2) -> np.ndarray:
    """
    Suavização por corte de cantos (Chaikin) -- aproxima o efeito do
    'Smoothing' do TerraModeler (algoritmo exato não documentado).
    """
    pts = coords
    if len(pts) < 3:
        return pts
    fechado = np.allclose(pts[0], pts[-1])
    for _ in range(iteracoes):
        n = len(pts)
        limite = n - 1
        novos = []
        for i in range(limite):
            p0, p1 = pts[i], pts[i + 1]
            novos.append(p0 + 0.25 * (p1 - p0))
            novos.append(p0 + 0.75 * (p1 - p0))
        if fechado:
            novos.append(novos[0])
        else:
            novos = [pts[0]] + novos + [pts[-1]]
        pts = np.array(novos)
    return pts


def _suavizar_preservando_pontas(coords: np.ndarray, iteracoes: int = 2) -> np.ndarray:
    pts = _suavizar_chaikin(coords, iteracoes=iteracoes)
    if len(pts) >= 2 and not np.allclose(coords[0], coords[-1]):
        pts[0] = coords[0]
        pts[-1] = coords[-1]
    return pts


def _ajustar_bordas_folha(geom, bbox_saida: tuple[float, float, float, float], tolerancia: float):
    x_min, y_min, x_max, y_max = bbox_saida
    coords = np.asarray(geom.coords, dtype=np.float64).copy()
    if coords.shape[0] < 2:
        return geom

    for i in (0, coords.shape[0] - 1):
        x, y = coords[i]
        distancias = [
            (abs(x - x_min), 0, x_min),
            (abs(x - x_max), 0, x_max),
            (abs(y - y_min), 1, y_min),
            (abs(y - y_max), 1, y_max),
        ]
        distancia, eixo, valor = min(distancias, key=lambda item: item[0])
        if distancia <= tolerancia:
            coords[i, eixo] = valor

    return LineString(coords)


def _gerar_niveis(z_min: float, z_max: float, intervalo: float) -> np.ndarray:
    if intervalo <= 0:
        return np.array([])
    primeiro = np.ceil(z_min / intervalo) * intervalo
    if primeiro > z_max:
        return np.array([])
    return np.arange(primeiro, z_max, intervalo)


def _alinhar_extensao_grid(valor_min: float, valor_max: float, res: float) -> tuple[float, float]:
    """Alinha a extensão aos múltiplos da resolução para folhas vizinhas coincidirem."""
    return np.floor(valor_min / res) * res, np.ceil(valor_max / res) * res


def _salvar_curvas(registros_linhas: list[dict], params: ContourParams, nome_base: str) -> str:
    if not registros_linhas:
        return ""

    out_dir = Path(params.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gdf_linhas = gpd.GeoDataFrame(
        registros_linhas,
        crs=f"EPSG:{params.epsg}" if params.epsg else None,
    )
    if params.formato_saida == "GPKG":
        curvas_path = str(out_dir / f"{params.prefixo}_{nome_base}_curvas.gpkg")
        gdf_linhas.to_file(curvas_path, layer="curvas", driver="GPKG")
        return curvas_path

    curvas_path = str(out_dir / f"{params.prefixo}_{nome_base}_curvas.shp")
    gdf_linhas.to_file(curvas_path)
    return curvas_path


def _extrair_curvas_xyz(
    xyz_total: np.ndarray,
    bbox_saida: tuple[float, float, float, float],
    params: ContourParams,
    nome_base: str,
    n_vizinhos_usados: int = 0,
    margem_extra: Optional[float] = None,
) -> ContourResult:
    """Extrai curvas de um conjunto XYZ único e grava em SHP/GPKG."""
    t0 = time.time()
    x_min_nom, y_min_nom, x_max_nom, y_max_nom = bbox_saida

    try:
        if xyz_total.shape[0] < 3:
            return ContourResult(
                laz_path=params.laz_path,
                ok=False,
                message=(
                    f"Menos de 3 pontos nas classes {sorted(set(params.classes))} "
                    "(verifique a lista de classes)."
                ),
            )

        tri = Delaunay(xyz_total[:, :2])

        # --- raster fino com respiro ao redor da folha ---
        # O TIN já usa vizinhos dentro do Model buffer; rasterizar só a extensão
        # nominal cortava os contornos exatamente na borda e podia gerar pontas
        # diferentes entre folhas. Calculamos no envelope expandido e só depois
        # recortamos na folha nominal, preservando continuidade.
        res = params.resolucao_grid
        margem_curvas = margem_extra if margem_extra is not None else min(max(params.model_buffer, res * 2.0), res * 8.0)
        x_min_g, x_max_g = _alinhar_extensao_grid(x_min_nom - margem_curvas, x_max_nom + margem_curvas, res)
        y_min_g, y_max_g = _alinhar_extensao_grid(y_min_nom - margem_curvas, y_max_nom + margem_curvas, res)

        n_col = max(int(round((x_max_g - x_min_g) / res)), 1)
        n_lin = max(int(round((y_max_g - y_min_g) / res)), 1)

        cols_centros = x_min_g + (np.arange(n_col) + 0.5) * res
        lins_centros = y_max_g - (np.arange(n_lin) + 0.5) * res
        grid_x, grid_y = np.meshgrid(cols_centros, lins_centros)
        pontos_grid = np.column_stack([grid_x.ravel(), grid_y.ravel()])

        simplex_idx = tri.find_simplex(pontos_grid)
        VAZIO = -1.0e6
        grid_z = np.full(pontos_grid.shape[0], VAZIO, dtype=np.float64)
        dentro = simplex_idx >= 0
        if dentro.any():
            simplices = tri.simplices[simplex_idx[dentro]]
            transform = tri.transform[simplex_idx[dentro]]
            b0 = pontos_grid[dentro] - transform[:, 2]
            bary = np.einsum("ijk,ik->ij", transform[:, :2], b0)
            bary_completo = np.column_stack([bary, 1 - bary.sum(axis=1)])
            z_simplex = xyz_total[simplices, 2]
            grid_z[dentro] = np.sum(bary_completo * z_simplex, axis=1)
        grid_z = grid_z.reshape((n_lin, n_col))

        if not dentro.any():
            return ContourResult(
                laz_path=params.laz_path, ok=False, message="Nenhum ponto dentro da triangulação."
            )

        bbox_nominal_poly = box(x_min_nom, y_min_nom, x_max_nom, y_max_nom)
        bbox_nominal_mask = (
            (grid_x >= x_min_nom) & (grid_x <= x_max_nom)
            & (grid_y >= y_min_nom) & (grid_y <= y_max_nom)
            & (grid_z > VAZIO / 2)
        )
        z_validos = grid_z[bbox_nominal_mask]
        if z_validos.size == 0:
            z_validos = grid_z[grid_z > VAZIO / 2]
        z_min, z_max = float(z_validos.min()), float(z_validos.max())

        # --- monta a lista de níveis (major tem prioridade sobre basic, que tem
        #     prioridade sobre minor, para não desenhar o mesmo nível duas vezes) ---
        niveis: list[tuple[float, str]] = []
        usados: set[float] = set()

        if params.draw_major and params.major_interval > 0:
            for z in _gerar_niveis(z_min, z_max, params.major_interval):
                niveis.append((float(z), "major"))
                usados.add(round(float(z), 6))
        if params.draw_basic and params.basic_interval > 0:
            for z in _gerar_niveis(z_min, z_max, params.basic_interval):
                if round(float(z), 6) not in usados:
                    niveis.append((float(z), "basic"))
                    usados.add(round(float(z), 6))
        if params.draw_minor and params.minor_interval > 0:
            for z in _gerar_niveis(z_min, z_max, params.minor_interval):
                if round(float(z), 6) not in usados:
                    niveis.append((float(z), "minor"))
                    usados.add(round(float(z), 6))

        registros_linhas = []

        for z0, tipo in niveis:
            grid_contorno = np.where(grid_z > VAZIO / 2, grid_z, np.nan)
            contornos = skimage_measure.find_contours(grid_contorno, level=z0)
            for c in contornos:
                if len(c) < 2:
                    continue
                coords_mundo = np.column_stack(
                    [
                        x_min_g + (c[:, 1] + 0.5) * res,
                        y_max_g - (c[:, 0] + 0.5) * res,
                    ]
                )

                if params.use_min_area and np.allclose(coords_mundo[0], coords_mundo[-1]):
                    area_anel = Polygon(coords_mundo).area
                    limite_area = max(params.hill_area, params.dep_area)
                    if area_anel < limite_area:
                        continue

                linha = LineString(coords_mundo)

                if params.thin and params.thin_deviation > 0:
                    linha = linha.simplify(params.thin_deviation, preserve_topology=False)

                linha_recortada = linha.intersection(bbox_nominal_poly)
                if linha_recortada.is_empty:
                    continue

                geometrias = (
                    list(linha_recortada.geoms)
                    if linha_recortada.geom_type.startswith("Multi")
                    else [linha_recortada]
                )
                for geom in geometrias:
                    if geom.geom_type == "LineString" and len(geom.coords) >= 2:
                        if params.ajustar_bordas:
                            geom = _ajustar_bordas_folha(
                                geom,
                                bbox_saida,
                                tolerancia=max(res * 0.75, 0.001),
                            )
                        if params.smoothing:
                            coords_suaves = _suavizar_preservando_pontas(
                                np.asarray(geom.coords, dtype=np.float64),
                                iteracoes=2,
                            )
                            geom = LineString(coords_suaves)
                        registros_linhas.append(
                            {
                                "geometry": geom,
                                "cota": round(z0, params.label_decimals),
                                "tipo": tipo,
                            }
                        )

        curvas_path = _salvar_curvas(registros_linhas, params, nome_base)

        return ContourResult(
            laz_path=params.laz_path,
            ok=True,
            message="OK",
            n_pontos_tin=int(xyz_total.shape[0]),
            n_vizinhos_usados=n_vizinhos_usados,
            n_linhas=len(registros_linhas),
            tempo_s=time.time() - t0,
            curvas_path=curvas_path,
        )

    except Exception as exc:  # noqa: BLE001
        return ContourResult(
            laz_path=params.laz_path,
            ok=False,
            message=f"Erro: {exc}\n{traceback.format_exc(limit=2)}",
        )


def process_contour_block(params: ContourParams) -> ContourResult:
    """Processa uma folha: TIN (com vizinhos) -> raster fino -> contornos."""
    try:
        laz_path = Path(params.laz_path)
        classes_alvo = set(params.classes)
        xyz_total, bbox_nominal, n_vizinhos_usados = _reunir_pontos_buffer(
            laz_path, params.vizinhos_paths, classes_alvo, params.model_buffer
        )
        return _extrair_curvas_xyz(
            xyz_total,
            bbox_nominal,
            params,
            laz_path.stem,
            n_vizinhos_usados=n_vizinhos_usados,
        )
    except Exception as exc:  # noqa: BLE001
        return ContourResult(
            laz_path=params.laz_path,
            ok=False,
            message=f"Erro: {exc}\n{traceback.format_exc(limit=2)}",
        )


def process_contour_dataset(arquivos: list[Path], params: ContourParams) -> ContourResult:
    """Processa todas as folhas como um único bloco para gerar curvas contínuas."""
    classes_alvo = set(params.classes)
    pontos = []
    bboxes = []

    for caminho in arquivos:
        xyz, bb = _ler_xyz_filtrado(caminho, classes_alvo)  # chunks: sem carregar tudo
        bboxes.append(bb)
        if xyz.shape[0] > 0:
            pontos.append(xyz)

    xyz_total = np.concatenate(pontos, axis=0) if pontos else np.empty((0, 3), dtype=np.float64)
    bbox_total = (
        min(b[0] for b in bboxes), min(b[1] for b in bboxes),
        max(b[2] for b in bboxes), max(b[3] for b in bboxes),
    )
    return _extrair_curvas_xyz(
        xyz_total,
        bbox_total,
        params,
        "continuo",
        n_vizinhos_usados=0,
        margem_extra=params.resolucao_grid * 2.0,
    )


# ── Execução em lote com ProcessPoolExecutor (abas 7-9) ───────────────────

def run_pool(func, lista, max_workers, cancel, emit, on_result=None):
    """
    Executa `func` sobre `lista` em processos paralelos.
    O cancelamento é REAL: o loop acorda a cada 0,25 s, e ao cancelar descarta
    os futures pendentes e encerra os processos que estão rodando.
    """
    total = len(lista)
    done = 0
    executor = ProcessPoolExecutor(max_workers=max_workers)
    try:
        pendentes = {executor.submit(func, p) for p in lista}
        while pendentes and not cancel.is_set():
            concluidos, pendentes = wait(pendentes, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in concluidos:
                resultado = future.result()
                done += 1
                if on_result:
                    on_result(resultado)
                emit("result", resultado)
                emit("progress", done, total)
    finally:
        cancelado = cancel.is_set()
        if cancelado:
            for proc in list(getattr(executor, "_processes", {}).values()):
                try:
                    proc.terminate()
                except Exception:
                    pass
            emit("log", f"Cancelado: {done}/{total} folha(s) concluída(s). "
                        "Saídas parciais das folhas interrompidas podem existir.")
        executor.shutdown(wait=not cancelado, cancel_futures=True)
    return done


def work_keypoints(lista, max_workers, cancel, emit):
    run_pool(process_block, lista, max_workers, cancel, emit)


def work_lattice(arquivos, kw, gerar_mosaico, nome_mosaico, max_workers, cancel, emit):
    emit("log", f"Lendo cabeçalhos de {len(arquivos)} folha(s) para montar "
                "índice espacial de vizinhos...")
    vizinhos = _construir_indice_vizinhos(arquivos, kw["model_buffer"])
    lista = [
        LatticeParams(laz_path=str(a), vizinhos_paths=vizinhos.get(str(a), []), **kw)
        for a in arquivos
    ]

    tifs: list[tuple[str, tuple[float, float, float, float]]] = []

    def _coleta(r: LatticeResult):
        if r.ok and r.tif_path:
            tifs.append((r.tif_path, (r.x_min_nom, r.y_min_nom, r.x_max_nom, r.y_max_nom)))

    run_pool(process_lattice_block, lista, max_workers, cancel, emit, on_result=_coleta)

    if gerar_mosaico and not cancel.is_set():
        if tifs:
            emit("log", f"Gerando GeoTIFF geral (mosaico) com {len(tifs)} folha(s)...")
            caminho = str(Path(kw["output_dir"]) / nome_mosaico)
            try:
                n_col, n_lin = _gerar_mosaico_geral(tifs, caminho, kw["outside_z"], kw["criar_tfw"])
                emit("log", f"[OK] Mosaico geral -> {caminho} ({n_col} x {n_lin} px)")
            except Exception as exc:  # noqa: BLE001
                emit("log", f"[FALHA] Erro ao gerar mosaico geral: {exc}")
        else:
            emit("log", "Nenhum GeoTIFF individual foi gerado com sucesso; mosaico geral não criado.")


def work_contours(arquivos, model_buffer, kw, saida_continua, max_workers, cancel, emit):
    if saida_continua:
        emit("log", "Modo contínuo ativo: montando uma única triangulação para todas as folhas...")
        params = ContourParams(laz_path="Lote contínuo", vizinhos_paths=[],
                               model_buffer=model_buffer, **kw)
        resultado = process_contour_dataset(arquivos, params)
        emit("result", resultado)
        emit("progress", 1, 1)
        return

    emit("log", f"Lendo cabeçalhos de {len(arquivos)} folha(s) para montar "
                "índice espacial de vizinhos...")
    vizinhos = _construir_indice_vizinhos(arquivos, model_buffer)
    lista = [
        ContourParams(laz_path=str(a), vizinhos_paths=vizinhos.get(str(a), []),
                      model_buffer=model_buffer, **kw)
        for a in arquivos
    ]
    run_pool(process_contour_block, lista, max_workers, cancel, emit)


# ==========================================================================
#  INFRAESTRUTURA DE INTERFACE
# ==========================================================================

STYLE = """
QLabel#hint { color: #8B95A1; font-size: 9pt; }
QLabel#sec  { color: #4B5563; font-weight: bold; font-size: 9pt; }
QFrame#card { background: #FFFFFF; border: 1px solid #DDE1E7; border-radius: 4px; }
QLabel#statVal { font-size: 18pt; font-weight: bold; }
QLabel#statAcc { font-size: 18pt; font-weight: bold; color: #2A5FAC; }
QLabel#statLbl { color: #8B95A1; font-size: 8pt; }
QPushButton#primary { background: #2A5FAC; color: white; font-weight: bold;
                      padding: 8px 20px; border: none; border-radius: 3px; }
QPushButton#primary:hover { background: #1E4A8A; }
QPushButton#primary:disabled { background: #8FB3DC; }
QPushButton#danger { color: #B91C1C; }
QLabel#warn { background: #FEF2F2; color: #B91C1C; padding: 10px; }
"""


def mkbtn(text, slot=None, kind=None) -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    if slot:
        b.clicked.connect(slot)
    return b


def mklabel(text, name=None) -> QLabel:
    lb = QLabel(text)
    if name:
        lb.setObjectName(name)
    return lb


def spin_d(lo, hi, val, dec=2, step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    if step:
        s.setSingleStep(step)
    s.setValue(val)
    return s


def spin_i(lo, hi, val) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def parse_classes(texto: str) -> Optional[list[int]]:
    texto = texto.strip()
    if not texto:
        return None
    try:
        return [int(c.strip()) for c in texto.split(",") if c.strip() != ""]
    except ValueError:
        return None


def browse_row(edit: QLineEdit, titulo: str) -> QHBoxLayout:
    def _pick():
        pasta = QFileDialog.getExistingDirectory(edit, titulo)
        if pasta:
            edit.setText(pasta)
    row = QHBoxLayout()
    row.addWidget(edit)
    row.addWidget(mkbtn("Selecionar...", _pick))
    return row


def info(w, t, m):
    QMessageBox.information(w, t, m)


def warn(w, t, m):
    QMessageBox.warning(w, t, m)


def ask(w, t, m) -> bool:
    return QMessageBox.question(w, t, m) == QMessageBox.StandardButton.Yes


def card(title: Optional[str] = None) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    if title:
        lay.addWidget(mklabel(title, "sec"))
    return f, lay


def make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    t.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return t


def table_add(t: QTableWidget, values: list, center_from: int = 1) -> None:
    r = t.rowCount()
    t.insertRow(r)
    for c, v in enumerate(values):
        it = QTableWidgetItem(str(v))
        if c >= center_from:
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setItem(r, c, it)


class LogView(QTextEdit):
    COLORS = {"": "#1A1D23", "ok": "#1A6B3C", "err": "#B91C1C", "muted": "#8B95A1"}

    def __init__(self, min_h: int = 90) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setMinimumHeight(min_h)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

    def add(self, text: str, tag: str = "") -> None:
        esc = _html.escape(text).replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        self.append(f'<span style="color:{self.COLORS.get(tag, self.COLORS[""])}">{esc}</span>')


class Stats(QWidget):
    def __init__(self, items) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.labels: dict[str, QLabel] = {}
        for key, caption, accent in items:
            f = QFrame()
            f.setObjectName("card")
            v = QVBoxLayout(f)
            v.setContentsMargins(10, 6, 10, 6)
            val = mklabel("0", "statAcc" if accent else "statVal")
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap = mklabel(caption, "statLbl")
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            v.addWidget(val)
            v.addWidget(cap)
            lay.addWidget(f, 1)
            self.labels[key] = val

    def set(self, key: str, val) -> None:
        self.labels[key].setText(str(val))

    def reset(self, *keys: str) -> None:
        for k in (keys or self.labels.keys()):
            self.labels[k].setText("0")


class FileList(QListWidget):
    """Lista de arquivos com arrastar-e-soltar; guarda uma lista de Paths."""
    dropped = Signal(int)

    def __init__(self, exts, fmt=None, mono: bool = False, recursive: bool = True) -> None:
        super().__init__()
        self.exts = tuple(e.lower() for e in exts)
        self.fmt = fmt or (lambda p: p.name)
        self.paths: list[Path] = []
        self.recursive = recursive
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        if mono:
            self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

    # --- drag & drop ---
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        n = self.add_paths(paths)
        e.acceptProposedAction()
        self.dropped.emit(n)

    # --- dados ---
    def add_paths(self, paths, recursive: Optional[bool] = None) -> int:
        rec = self.recursive if recursive is None else recursive
        existentes = {str(p) for p in self.paths}
        add = 0
        for p in paths:
            p = Path(p)
            cand: list[Path] = []
            if p.is_dir():
                it = p.rglob("*") if rec else p.iterdir()
                cand = sorted(f for f in it if f.is_file() and f.suffix.lower() in self.exts)
            elif p.is_file() and p.suffix.lower() in self.exts:
                cand = [p]
            for f in cand:
                if str(f) not in existentes:
                    self.paths.append(f)
                    existentes.add(str(f))
                    add += 1
        if add:
            self.refresh()
        return add

    def refresh(self) -> None:
        QListWidget.clear(self)
        for p in self.paths:
            self.addItem(self.fmt(p))

    def selected_paths(self) -> list[Path]:
        rows = sorted(self.row(i) for i in self.selectedItems())
        return [self.paths[r] for r in rows]

    def remove_selected(self) -> int:
        sel = {str(p) for p in self.selected_paths()}
        self.paths = [p for p in self.paths if str(p) not in sel]
        self.refresh()
        return len(sel)

    def reset_list(self) -> None:
        # NÃO chamar 'reset': conflita com QAbstractItemView.reset() do Qt (recursão)
        self.paths = []
        self.refresh()


# --------------------------------------------------------------------------
#  Execução em thread
# --------------------------------------------------------------------------

class _Worker(QObject):
    evento = Signal(str, object)

    def __init__(self, target, args) -> None:
        super().__init__()
        self.target = target
        self.args = args

    def _emit(self, kind, *a) -> None:
        self.evento.emit(kind, a)

    def run(self) -> None:
        try:
            self.target(*self.args, self._emit)
        except Exception:  # noqa: BLE001
            self.evento.emit("fatal", traceback.format_exc())
        finally:
            self.evento.emit("__end__", ())


class Runner(QObject):
    """Executa `target(*args, emit)` em QThread e entrega os eventos na thread da UI."""

    def __init__(self, handler, parent=None) -> None:
        super().__init__(parent)
        self.handler = handler
        self._thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        self.running = False

    def start(self, target, *args) -> None:
        self.running = True
        self._thread = QThread(self)
        self._worker = _Worker(target, args)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.evento.connect(self._on_evento)
        self._thread.start()

    def _on_evento(self, kind: str, a) -> None:
        if kind == "__end__":
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait()
            if self._worker is not None:
                self._worker.deleteLater()
            if self._thread is not None:
                self._thread.deleteLater()
            self._worker = self._thread = None
            self.running = False
            self.handler("finished", ())
            return
        self.handler(kind, a)


class ToolTab(QWidget):
    """Base das abas: runner + despacho de eventos."""

    def __init__(self, status) -> None:
        super().__init__()
        self.status = status
        self.runner = Runner(self._dispatch, self)
        self.pb = QProgressBar()
        self.pb.setVisible(False)

    def _start_pb(self, maximum: int) -> None:
        self.pb.setMaximum(max(maximum, 1))
        self.pb.setValue(0)
        self.pb.setVisible(True)

    def _dispatch(self, kind: str, a) -> None:
        if kind == "fatal":
            self._busy(False)
            self.pb.setVisible(False)
            QMessageBox.critical(self, "Erro", f"Falha inesperada:\n\n{a}")
        elif kind == "finished":
            self._finished()
        else:
            self._on_event(kind, *a)

    def _on_event(self, kind: str, *a) -> None: ...
    def _finished(self) -> None: ...
    def _busy(self, busy: bool) -> None: ...


def set_enabled(widgets, enabled: bool) -> None:
    for w in widgets:
        w.setEnabled(enabled)


class MissingDepsTab(QWidget):
    def __init__(self, titulo: str, mensagem: str) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lb = mklabel(f"{titulo}\n\n{mensagem}", "warn")
        lb.setWordWrap(True)
        lay.addWidget(lb)
        lay.addStretch(1)


# ==========================================================================
#  ABA 1 — METADADOS (nome da folha e nº de pontos)
# ==========================================================================

class MetadataTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self._results: list[dict] = []

        root = QVBoxLayout(self)

        row = QHBoxLayout()
        self.btn_add_dir = mkbtn("Adicionar PASTA", self.on_add_dir)
        self.btn_add_files = mkbtn("Adicionar ARQUIVOS", self.on_add_files)
        self.btn_clear = mkbtn("Limpar Lista", self.on_clear_list, "danger")
        self.chk_rec = QCheckBox("Recursivo")
        self.chk_rec.setChecked(True)
        for b in (self.btn_add_dir, self.btn_add_files, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.chk_rec)
        root.addLayout(row)

        root.addWidget(mklabel("Entradas (.las/.laz) — também é possível arrastar e soltar:", "hint"))
        self.flist = FileList((".las", ".laz"), fmt=lambda p: str(p))
        self.flist.dropped.connect(lambda n: self.status(f"{n} arquivo(s) adicionado(s)."))
        root.addWidget(self.flist, 1)

        run = QHBoxLayout()
        self.btn_run = mkbtn("Iniciar Leitura", self.on_run, "primary")
        self.btn_export = mkbtn("Exportar CSV/TXT", self.on_export)
        self.btn_copy = mkbtn("Copiar Resultado", self.on_copy_results)
        self.btn_copy_sel = mkbtn("Copiar Selecionados", self.on_copy_selected)
        run.addWidget(self.btn_run)
        run.addWidget(self.pb, 1)
        run.addWidget(self.btn_export)
        run.addWidget(self.btn_copy)
        run.addWidget(self.btn_copy_sel)
        root.addLayout(run)
        self._result_buttons(False)

        root.addWidget(mklabel("Resultados:", "hint"))
        self.table = make_table(["Folha", "Pontos"])
        root.addWidget(self.table, 2)

        logrow = QHBoxLayout()
        logrow.addWidget(mklabel("Log:", "hint"))
        logrow.addStretch(1)
        logrow.addWidget(mkbtn("Copiar LOG", self.on_copy_log))
        logrow.addWidget(mkbtn("Copiar Seleção LOG", self.on_copy_log_sel))
        logrow.addWidget(mkbtn("Salvar LOG", self.on_save_log))
        logrow.addWidget(mkbtn("Limpar LOG", self.on_clear_log))
        root.addLayout(logrow)
        self.log = LogView(100)
        root.addWidget(self.log, 1)
        self.log.add("Programa iniciado.", "muted")

    def _result_buttons(self, enabled: bool) -> None:
        set_enabled([self.btn_export, self.btn_copy, self.btn_copy_sel], enabled)

    # --- entrada ---
    def on_add_dir(self) -> None:
        pasta = QFileDialog.getExistingDirectory(self, "Escolher pasta com .las/.laz")
        if not pasta:
            return
        n = self.flist.add_paths([Path(pasta)], recursive=self.chk_rec.isChecked())
        if n == 0:
            self.log.add("Aviso: Nenhum .las/.laz novo encontrado nessa pasta.", "err")
        else:
            self.log.add(f"Adicionados {n} arquivo(s) da pasta.")

    def on_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar arquivos LAS/LAZ", "", "Arquivos LAS/LAZ (*.las *.laz *.LAS *.LAZ)")
        if files:
            n = self.flist.add_paths([Path(f) for f in files])
            self.log.add(f"Adicionados {n} arquivo(s) selecionado(s).")

    def on_clear_list(self) -> None:
        self.flist.reset_list()
        self.table.setRowCount(0)
        self.pb.setVisible(False)
        self._results.clear()
        self._result_buttons(False)
        self.log.add("Lista limpa.", "muted")

    # --- execução ---
    def on_run(self) -> None:
        if not LASPY_AVAILABLE:
            warn(self, "Dependência ausente", "Instale a biblioteca laspy:\npip install laspy[lazrs]")
            return
        files = [str(p) for p in self.flist.paths]
        if not files:
            warn(self, "Atenção", "Adicione pelo menos um arquivo ou pasta antes de iniciar.")
            return
        self.table.setRowCount(0)
        self._results.clear()
        self._result_buttons(False)
        self._start_pb(len(files))
        self._busy(True)
        self.log.add("")
        self.log.add("Iniciando leitura dos metadados...")
        self.runner.start(work_metadata, files)

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_add_dir, self.btn_add_files, self.btn_clear,
                     self.btn_run, self.chk_rec], not busy)
        tem = bool(self._results)
        self.btn_export.setEnabled(not busy and tem)
        self.btn_copy.setEnabled(tem)
        self.btn_copy_sel.setEnabled(tem)

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            pct = int(a[0] / a[1] * 100)
            self.pb.setMaximum(100)
            self.pb.setValue(pct)
            self.pb.setFormat(f"{pct}% - Processado {a[0]}/{a[1]}")
        elif kind == "row":
            folha, path, pontos = a
            table_add(self.table, [folha, pontos])
            self._results.append({"Folha": folha, "Pontos": pontos})
            self.log.add(f"OK: {path} | Pontos: {pontos}", "ok")
            self._result_buttons(True)
        elif kind == "warn":
            self.log.add(f"Aviso: {a[0]}", "err")
        elif kind == "error":
            self.log.add(f"ERRO: {a[0]}", "err")
        elif kind == "done":
            self._busy(False)
            self.log.add("Concluído.")

    # --- resultados -> texto ---
    def _results_text(self) -> str:
        if not self._results:
            return ""
        return "\n".join(["Folha\tPontos"] + [f"{r['Folha']}\t{r['Pontos']}" for r in self._results])

    def _selected_text(self) -> str:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            return ""
        linhas = ["Folha\tPontos"]
        for r in rows:
            a, b = self.table.item(r, 0), self.table.item(r, 1)
            linhas.append(f"{a.text() if a else ''}\t{b.text() if b else ''}")
        return "\n".join(linhas)

    def on_copy_results(self) -> None:
        txt = self._results_text()
        if not txt.strip():
            info(self, "Sem resultados", "Não há resultados para copiar.")
            return
        QApplication.clipboard().setText(txt)
        self.log.add("OK: Todos os resultados foram copiados.", "ok")
        info(self, "Resultado copiado",
             "Todos os resultados foram copiados.\nVocê pode colar no Excel, LibreOffice ou Bloco de Notas.")

    def on_copy_selected(self) -> None:
        txt = self._selected_text()
        if not txt.strip():
            info(self, "Nada selecionado", "Selecione uma ou mais linhas da tabela antes de copiar.")
            return
        QApplication.clipboard().setText(txt)
        self.log.add("OK: Resultados selecionados foram copiados.", "ok")
        info(self, "Selecionados copiados", "As linhas selecionadas foram copiadas.")

    def on_export(self) -> None:
        if not self._results:
            info(self, "Sem dados", "Não há resultados para exportar.")
            return
        path, sel = QFileDialog.getSaveFileName(
            self, "Salvar resultados", "", "CSV (*.csv);;TXT (*.txt);;Todos (*)")
        if not path:
            return
        base, ext = os.path.splitext(path)
        flt = (sel or "").lower()
        if "csv" in flt and ext.lower() != ".csv":
            path = base + ".csv"
        elif "txt" in flt and ext.lower() != ".txt":
            path = base + ".txt"
        elif ext == "":
            path = base + ".csv"
        sep = ";" if path.lower().endswith(".csv") else "\t"
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=sep)
                w.writerow(["Folha", "Pontos"])
                for r in self._results:
                    w.writerow([r["Folha"], r["Pontos"]])
            self.log.add(f"OK: Arquivo salvo em: {path}", "ok")
            info(self, "Exportado", f"Arquivo salvo em:\n{path}")
        except Exception as ex:  # noqa: BLE001
            self.log.add(f"ERRO: Falha ao salvar '{path}'\n{ex}", "err")
            QMessageBox.critical(self, "Erro ao salvar", f"Falha ao salvar o arquivo:\n{ex}")

    # --- LOG ---
    def on_copy_log(self) -> None:
        txt = self.log.toPlainText()
        if not txt.strip():
            info(self, "LOG vazio", "Não há informações no LOG para copiar.")
            return
        QApplication.clipboard().setText(txt)
        info(self, "LOG copiado", "Todo o conteúdo do LOG foi copiado.")

    def on_copy_log_sel(self) -> None:
        txt = self.log.textCursor().selectedText().replace("\u2029", "\n")
        if not txt.strip():
            info(self, "Nada selecionado", "Selecione uma parte do LOG antes de usar este botão.")
            return
        QApplication.clipboard().setText(txt)
        info(self, "Seleção copiada", "A parte selecionada do LOG foi copiada.")

    def on_save_log(self) -> None:
        txt = self.log.toPlainText()
        if not txt.strip():
            info(self, "LOG vazio", "Não há informações no LOG para salvar.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Salvar LOG", "", "Arquivo de texto (*.txt);;Todos os arquivos (*)")
        if not path:
            return
        base, ext = os.path.splitext(path)
        if ext == "":
            path = base + ".txt"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)
            self.log.add(f"OK: LOG salvo em: {path}", "ok")
            info(self, "LOG salvo", f"LOG salvo em:\n{path}")
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, "Erro ao salvar LOG", f"Falha ao salvar o LOG:\n{ex}")

    def on_clear_log(self) -> None:
        self.log.clear()
        self.log.add("LOG limpo.", "muted")



# ==========================================================================
#  ABA 3 — EDITAR PRJ
# ==========================================================================

class PrjEditTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self.alter = self.err = self.n_las = self.n_laz = 0
        lay = QVBoxLayout(self)

        opt, ol = card()
        row = QHBoxLayout()
        row.addWidget(mklabel("SUBSTITUIÇÃO:", "sec"))
        self.rb_l2z = QRadioButton(".LAS → .LAZ")
        self.rb_z2l = QRadioButton(".LAZ → .LAS")
        self.rb_both = QRadioButton("Ambos")
        self.rb_l2z.setChecked(True)
        for rb in (self.rb_l2z, self.rb_z2l, self.rb_both):
            row.addWidget(rb)
        self.chk_bak = QCheckBox("Criar backup antes de salvar")
        self.chk_bak.setChecked(True)
        row.addSpacing(16)
        row.addWidget(self.chk_bak)
        row.addStretch(1)
        self.btn_add = mkbtn("+ PRJ", self._add_prj)
        self.btn_dir = mkbtn("Pasta", self._add_dir)
        self.btn_clr = mkbtn("Limpar", self._clear, "danger")
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            row.addWidget(b)
        ol.addLayout(row)
        lay.addWidget(opt)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(mklabel("Arquivos PRJ — arraste e solte", "hint"))
        self.flist = FileList((".prj",))
        self.flist.dropped.connect(self._after_add)
        self.flist.itemSelectionChanged.connect(self._preview)
        ll.addWidget(self.flist)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(mklabel("Pré-visualização do conteúdo", "hint"))
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        rl.addWidget(self.preview)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)

        self.stats = Stats([("total", "PRJs na fila", False), ("alter", "Alterados", True),
                            ("err", "Erros", False), ("l2z", "Ref. .las→.laz", False),
                            ("z2l", "Ref. .laz→.las", False)])
        lay.addWidget(self.stats)

        lay.addWidget(mklabel("Log de operações", "hint"))
        self.log = LogView(70)
        lay.addWidget(self.log)
        lay.addWidget(self.pb)

        bottom = QHBoxLayout()
        self.btn_rem = mkbtn("Remover selecionados", self._remove_sel)
        self.btn_run = mkbtn("▶  Aplicar substituições", self._run, "primary")
        bottom.addWidget(self.btn_rem)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run)
        lay.addLayout(bottom)

    @property
    def mode(self) -> str:
        if self.rb_z2l.isChecked():
            return "laz_to_las"
        if self.rb_both.isChecked():
            return "both"
        return "las_to_laz"

    def _add_prj(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar PRJ", "", "TerraSolid/Spatix PRJ (*.prj *.PRJ)")
        self._add([Path(p) for p in ps])

    def _add_dir(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if f:
            self._add([Path(f)])

    def _add(self, paths) -> None:
        self._after_add(self.flist.add_paths(paths))

    def _after_add(self, n: int) -> None:
        if n:
            self.stats.set("total", len(self.flist.paths))
            self.status(f"{n} PRJ(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self) -> None:
        n = self.flist.remove_selected()
        self.stats.set("total", len(self.flist.paths))
        self._set_preview("")
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self) -> None:
        self.flist.reset_list()
        self._set_preview("")
        self.log.clear()
        self.stats.reset()
        self.status("Lista limpa.")

    def _set_preview(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.preview.setPlainText(text)
        sels = []
        if text:
            fmt_las = QTextCharFormat()
            fmt_las.setBackground(QColor("#FEF3C7"))
            fmt_las.setForeground(QColor("#92400E"))
            fmt_laz = QTextCharFormat()
            fmt_laz.setBackground(QColor("#DBEAFE"))
            fmt_laz.setForeground(QColor("#1E40AF"))
            for pat, fmt in ((r'(?i)Storage=(LAS)(?=\d)', fmt_las), (r'(?i)Storage=(LAZ)(?=\d)', fmt_laz),
                             (r'(?i)(\.las)\b', fmt_las), (r'(?i)(\.laz)\b', fmt_laz)):
                for m in re.finditer(pat, text):
                    sel = QTextEdit.ExtraSelection()
                    cur = QTextCursor(self.preview.document())
                    cur.setPosition(m.start(1))
                    cur.setPosition(m.end(1), QTextCursor.MoveMode.KeepAnchor)
                    sel.cursor = cur
                    sel.format = fmt
                    sels.append(sel)
        self.preview.setExtraSelections(sels)

    def _preview(self) -> None:
        sel = self.flist.selected_paths()
        if not sel:
            return
        try:
            self._set_preview(sel[0].read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            self._set_preview(f"Erro: {e}")

    def _run(self) -> None:
        if not self.flist.paths:
            info(self, "Nada a fazer", "Adicione arquivos PRJ.")
            return
        modo_txt = {"las_to_laz": ".LAS → .LAZ", "laz_to_las": ".LAZ → .LAS",
                    "both": ".LAS ↔ .LAZ"}[self.mode]
        bak_txt = "com backup" if self.chk_bak.isChecked() else "SEM backup"
        if not ask(self, "Confirmar substituição",
                   f"Aplicar substituição {modo_txt} em {len(self.flist.paths)} PRJ(s)?\n"
                   f"Modo: {bak_txt}.\nEsta ação modifica arquivos no disco."):
            return
        self.stats.reset("alter", "err", "l2z", "z2l")
        self.log.clear()
        self.alter = self.err = self.n_las = self.n_laz = 0
        self._start_pb(len(self.flist.paths))
        self._busy(True)
        self.runner.start(work_prj_edit, list(self.flist.paths), self.mode, self.chk_bak.isChecked())

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
        elif kind == "file":
            path, n_las, n_laz, ok, msg = a
            name = Path(path).name
            if ok:
                if n_las > 0 or n_laz > 0:
                    self.alter += 1
                    self.stats.set("alter", self.alter)
                    self.n_las += n_las
                    self.n_laz += n_laz
                    self.stats.set("l2z", self.n_las)
                    self.stats.set("z2l", self.n_laz)
                    tag, prefix = "ok", "✔"
                else:
                    tag, prefix = "muted", "—"
            else:
                self.err += 1
                self.stats.set("err", self.err)
                tag, prefix = "err", "✖"
            self.log.add(f"{prefix}  {name}  —  {msg}", tag)
        elif kind == "done":
            ok, erros, n_las, n_laz = a
            self._busy(False)
            self.pb.setVisible(False)
            self._preview()
            if erros == 0:
                self.status(f"Concluído — {ok} PRJ(s) alterado(s).")
                info(self, "Concluído",
                     f"{ok} PRJ(s) alterado(s).\n.las→.laz: {n_las}  |  .laz→.las: {n_laz}")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                warn(self, "Concluído com erros", f"{ok} processado(s).\n{erros} com erro.")

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_rem], not busy)
        self.btn_run.setText("Processando…" if busy else "▶  Aplicar substituições")


# ==========================================================================
#  ABA 4 — COPIADOR PRJ
# ==========================================================================

class PrjCopyTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self.entries: list[str] = []
        self.ok = self.err = 0
        self.problemas: list[str] = []
        lay = QVBoxLayout(self)

        s1, l1 = card("1. PASTA COM O ARQUIVO PRJ")
        r = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setReadOnly(True)
        self.btn_pick = mkbtn("Selecionar pasta", self._pick_folder)
        r.addWidget(self.folder)
        r.addWidget(self.btn_pick)
        l1.addLayout(r)
        r2 = QHBoxLayout()
        self.combo = QComboBox()
        self.btn_read = mkbtn("Ler PRJ", self._read_prj, "primary")
        r2.addWidget(self.combo, 1)
        r2.addWidget(self.btn_read)
        l1.addLayout(r2)
        lay.addWidget(s1)

        s2, l2 = card("2. ARQUIVOS ENCONTRADOS NO PRJ")
        r3 = QHBoxLayout()
        self.btn_sel = mkbtn("Selecionar todos", self._sel_all)
        self.btn_des = mkbtn("Desmarcar todos", self._des_all)
        self.count_lbl = mklabel("0 / 0 selecionados", "hint")
        r3.addWidget(self.btn_sel)
        r3.addWidget(self.btn_des)
        r3.addStretch(1)
        r3.addWidget(self.count_lbl)
        l2.addLayout(r3)
        self.lw = QListWidget()
        self.lw.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.lw.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.lw.itemSelectionChanged.connect(self._upd_count)
        l2.addWidget(self.lw, 1)
        lay.addWidget(s2, 1)

        s3, l3 = card("3. PASTA DE DESTINO")
        r4 = QHBoxLayout()
        self.dest = QLineEdit()
        self.dest.setReadOnly(True)
        self.btn_dest = mkbtn("Selecionar destino", self._pick_dest)
        r4.addWidget(self.dest)
        r4.addWidget(self.btn_dest)
        l3.addLayout(r4)
        lay.addWidget(s3)

        lay.addWidget(self.pb)
        bottom = QHBoxLayout()
        self.copy_lbl = mklabel("", "hint")
        self.btn_copy = mkbtn("▶  Copiar arquivos selecionados", self._copy, "primary")
        bottom.addWidget(self.copy_lbl)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_copy)
        lay.addLayout(bottom)

    def _sel_all(self) -> None:
        self.lw.selectAll()
        self._upd_count()

    def _des_all(self) -> None:
        self.lw.clearSelection()
        self._upd_count()

    def _upd_count(self) -> None:
        sel = len(self.lw.selectedItems())
        self.count_lbl.setText(f"{sel} / {self.lw.count()} selecionados")
        self.copy_lbl.setText(f"{sel} arquivo(s) a copiar")

    def _pick_folder(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta do PRJ")
        if not f:
            return
        self.folder.setText(f)
        prjs = sorted(x for x in os.listdir(f) if x.lower().endswith(".prj"))
        self.combo.clear()
        self.combo.addItems(prjs)
        self.status(f"{len(prjs)} PRJ(s) encontrado(s) na pasta." if prjs
                    else "Nenhum arquivo .PRJ encontrado na pasta selecionada.")

    def _read_prj(self) -> None:
        folder, name = self.folder.text(), self.combo.currentText()
        if not folder or not name:
            warn(self, "Atenção", "Selecione a pasta e o arquivo PRJ primeiro.")
            return
        try:
            content = Path(folder, name).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "Erro", f"Não foi possível ler o PRJ:\n{e}")
            return
        matches = re.findall(r'[^\s\n\r\t"\'<>|*?]+\.la[sz]', content, re.IGNORECASE)
        seen: set[str] = set()
        unique: list[str] = []
        for m in matches:
            key = os.path.basename(m).lower()
            if key not in seen:
                seen.add(key)
                unique.append(m.strip())
        self.entries = unique
        self.lw.clear()
        self.lw.addItems(unique)
        self._sel_all()
        self.status(f"{len(unique)} arquivo(s) encontrado(s) no PRJ.")

    def _pick_dest(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta de destino")
        if f:
            self.dest.setText(f)
            self.status(f"Destino: {f}")

    def _copy(self) -> None:
        rows = sorted(self.lw.row(i) for i in self.lw.selectedItems())
        if not rows:
            warn(self, "Atenção", "Selecione ao menos um arquivo.")
            return
        overwrite = self.chk_overwrite.isChecked()
        if not overwrite and not self.dest.text():
            warn(self, "Atenção", "Selecione a pasta de destino ou marque 'Sobrescrever original'.")
            return
        if not ask(self, "Confirmar cópia", f"Copiar {len(rows)} arquivo(s) para:\n{self.dest.text()}?"):
            return
        entries = [self.entries[i] for i in rows]
        self.ok = self.err = 0
        self.problemas = []
        self._start_pb(len(entries))
        self._busy(True)
        self.runner.start(work_copy, entries, self.folder.text(), self.dest.text())

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
            self.status(f"Copiando… {self.ok + self.err}/{self.pb.maximum()}")
        elif kind == "file":
            name, ok, msg = a
            if ok:
                self.ok += 1
            else:
                self.err += 1
                self.problemas.append(f"{name}  ({msg})")
        elif kind == "done":
            ok, erros = a
            self._busy(False)
            self.pb.setVisible(False)
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) copiado(s).")
                info(self, "Concluído", f"{ok} arquivo(s) copiado(s) com sucesso.")
            else:
                self.status(f"Concluído — {ok} OK, {erros} não encontrado(s)/erro(s).")
                warn(self, "Concluído com avisos",
                     f"{ok} copiado(s).\n{erros} arquivo(s) com problema:\n\n" + "\n".join(self.problemas))

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_copy, self.btn_read, self.btn_pick, self.btn_dest,
                     self.btn_sel, self.btn_des], not busy)
        self.btn_copy.setText("Copiando…" if busy else "▶  Copiar arquivos selecionados")


# ==========================================================================
#  ABA 5 — .LAZ -> .laz
# ==========================================================================

class LowercaseLazTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self.folder: Optional[Path] = None
        self.files: list[Path] = []
        self.ok = self.err = 0
        lay = QVBoxLayout(self)

        s, sl = card("PASTA COM ARQUIVOS .LAZ")
        r = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.btn_folder = mkbtn("Selecionar pasta", self._pick_folder)
        r.addWidget(self.folder_edit)
        r.addWidget(self.btn_folder)
        sl.addLayout(r)
        self.chk = QCheckBox("Incluir subpastas")
        self.chk.stateChanged.connect(lambda _=None: self._scan())
        sl.addWidget(self.chk)
        lay.addWidget(s)

        lay.addWidget(mklabel(
            "Pré-visualização — somente .LAZ maiúsculo será renomeado para .laz minúsculo", "hint"))
        self.lb = FileList((".laz",), fmt=lambda p: f"{p.name}  →  {p.name[:-4]}.laz", mono=True)
        lay.addWidget(self.lb, 1)

        self.stats = Stats([("found", "Arquivos .LAZ encontrados", False),
                            ("ok", "Renomeados", True), ("err", "Erros", False)])
        lay.addWidget(self.stats)
        lay.addWidget(self.pb)

        bottom = QHBoxLayout()
        self.btn_refresh = mkbtn("Atualizar lista", self._scan)
        self.btn_run = mkbtn("▶  Trocar .LAZ por .laz", self._run, "primary")
        bottom.addWidget(self.btn_refresh)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run)
        lay.addLayout(bottom)

    def _pick_folder(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta com arquivos .LAZ")
        if f:
            self.folder = Path(f)
            self.folder_edit.setText(f)
            self._scan()

    def _scan(self) -> None:
        self.files = []
        self.lb.paths = []
        self.lb.refresh()
        if not self.folder or not self.folder.exists():
            self.stats.set("found", 0)
            return
        it = self.folder.rglob("*") if self.chk.isChecked() else self.folder.iterdir()
        # comparação exata: .laz minúsculo é ignorado
        self.files = sorted([p for p in it if p.is_file() and p.suffix == ".LAZ"],
                            key=lambda p: str(p).lower())
        self.lb.paths = list(self.files)
        self.lb.refresh()
        self.stats.set("found", len(self.files))
        self.stats.set("ok", 0)
        self.stats.set("err", 0)
        self.status(f"{len(self.files)} arquivo(s) com extensão .LAZ maiúscula encontrado(s).")

    def _run(self) -> None:
        if not self.folder:
            info(self, "Selecione uma pasta", "Selecione primeiro a pasta que contém os arquivos .LAZ.")
            return
        self._scan()
        if not self.files:
            info(self, "Nada a fazer", "Não há arquivos com extensão .LAZ maiúscula nesta pasta.")
            return
        if not ask(self, "Confirmar",
                   f"Trocar a extensão de {len(self.files)} arquivo(s) de .LAZ para .laz?\n\n"
                   "O nome do arquivo será mantido. Apenas as letras da extensão serão "
                   "convertidas para minúsculas."):
            return
        self.ok = self.err = 0
        self.stats.reset("ok", "err")
        self._start_pb(len(self.files))
        self._busy(True)
        self.runner.start(work_lowercase_laz, list(self.files))

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
        elif kind == "file":
            if a[2]:
                self.ok += 1
                self.stats.set("ok", self.ok)
            else:
                self.err += 1
                self.stats.set("err", self.err)
        elif kind == "done":
            ok, erros = a
            self._busy(False)
            self.pb.setVisible(False)
            self._scan()
            self.stats.set("ok", ok)
            self.stats.set("err", erros)
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) alterado(s) de .LAZ para .laz.")
                info(self, "Concluído", f"{ok} arquivo(s) alterado(s) de .LAZ para .laz.")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                warn(self, "Concluído com erros",
                     f"{ok} arquivo(s) alterado(s).\n{erros} arquivo(s) apresentaram erro.")

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_folder, self.btn_refresh, self.btn_run, self.chk], not busy)
        self.btn_run.setText("Processando…" if busy else "▶  Trocar .LAZ por .laz")


# ==========================================================================
#  ABA 6 — CONVERTER LAS/LAZ 1.4 -> 1.2
# ==========================================================================

class ConvertVersionTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self.ok = self.err = 0
        lay = QVBoxLayout(self)

        if not LASPY_AVAILABLE:
            lb = mklabel("A biblioteca laspy não está instalada.\n"
                         "Instale com:  pip install laspy[lazrs]  e reinicie o programa.", "warn")
            lay.addWidget(lb)

        # Seleção de direção de conversão
        dir_box, dir_lay = card()
        dir_row = QHBoxLayout()
        dir_row.addWidget(mklabel("DIREÇÃO DA CONVERSÃO:", "sec"))
        self.rb_downgrade = QRadioButton("LAS 1.4 → 1.2 (Compatibilidade / TerraScan)")
        self.rb_upgrade = QRadioButton("LAS 1.2 → 1.4 (Modernização / Padrão Atual)")
        self.rb_downgrade.setChecked(True)
        self.rb_downgrade.toggled.connect(self._on_mode_changed)
        dir_row.addWidget(self.rb_downgrade)
        dir_row.addWidget(self.rb_upgrade)
        dir_row.addStretch(1)
        dir_lay.addLayout(dir_row)
        lay.addWidget(dir_box)

        tb = QHBoxLayout()
        self.lbl_entrada = mklabel("ENTRADA (LAZ/LAS 1.4):", "sec")
        tb.addWidget(self.lbl_entrada)
        self.btn_add = mkbtn("+ Adicionar", self._add_files)
        self.btn_dir = mkbtn("Pasta", self._add_folder)
        self.btn_clr = mkbtn("Limpar", self._clear, "danger")
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        self.chk_sub = QCheckBox("Incluir subpastas")
        self.chk_sub.setChecked(False)
        self.chk_sub.toggled.connect(lambda v: setattr(self.flist, "recursive", v))
        tb.addWidget(self.chk_sub)
        tb.addStretch(1)
        lay.addLayout(tb)

        self.lbl_hint = mklabel("Arraste e solte arquivos .LAZ ou .LAS (versão 1.4) aqui", "hint")
        lay.addWidget(self.lbl_hint)
        self.flist = FileList((".las", ".laz"), recursive=False)
        self.flist.dropped.connect(self._after_add)
        lay.addWidget(self.flist, 1)

        opt, ol = card()
        r = QHBoxLayout()
        r.addWidget(mklabel("DESTINO:", "sec"))
        self.dest = QLineEdit()
        self.dest.setReadOnly(True)
        self.btn_dest = mkbtn("Selecionar pasta", self._pick_dest)
        self.chk = QCheckBox("Adicionar sufixo _v12 ao nome do arquivo")
        self.chk.setChecked(True)
        self.chk_overwrite = QCheckBox("Sobrescrever original (na pasta de origem)")
        self.chk_overwrite.setChecked(False)
        self.chk_overwrite.toggled.connect(self._on_overwrite_toggled)
        r.addWidget(self.dest, 1)
        r.addWidget(self.btn_dest)
        r.addWidget(self.chk)
        ol.addLayout(r)
        r2 = QHBoxLayout()
        r2.addWidget(self.chk_overwrite)
        r2.addStretch(1)
        ol.addLayout(r2)
        lay.addWidget(opt)

        self.stats = Stats([("total", "Total na fila", False), ("ok", "Convertidos", True),
                            ("err", "Erros", False)])
        lay.addWidget(self.stats)

        lay.addWidget(mklabel("Log de operações", "hint"))
        self.log = LogView(90)
        lay.addWidget(self.log)
        lay.addWidget(self.pb)

        bottom = QHBoxLayout()
        self.btn_rem = mkbtn("Remover selecionados", self._remove_sel)
        self.btn_run = mkbtn("▶  Converter para 1.2", self._run, "primary")
        if not LASPY_AVAILABLE:
            self.btn_run.setEnabled(False)
        bottom.addWidget(self.btn_rem)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run)
        lay.addLayout(bottom)

    def _on_overwrite_toggled(self, checked: bool) -> None:
        self.dest.setEnabled(not checked)
        self.btn_dest.setEnabled(not checked)
        self.chk.setEnabled(not checked)
        if checked:
            self.dest.setPlaceholderText("Será salvo na pasta de origem sobrescrevendo o arquivo original")
        else:
            self.dest.setPlaceholderText("")

    def _on_mode_changed(self) -> None:
        if self.rb_downgrade.isChecked():
            self.lbl_entrada.setText("ENTRADA (LAZ/LAS 1.4):")
            self.lbl_hint.setText("Arraste e solte arquivos .LAZ ou .LAS (versão 1.4) aqui")
            self.chk.setText("Adicionar sufixo _v12 ao nome do arquivo")
            self.btn_run.setText("▶  Converter para 1.2")
        else:
            self.lbl_entrada.setText("ENTRADA (LAZ/LAS 1.2):")
            self.lbl_hint.setText("Arraste e solte arquivos .LAZ ou .LAS (versão 1.2 / 1.3) aqui")
            self.chk.setText("Adicionar sufixo _v14 ao nome do arquivo")
            self.btn_run.setText("▶  Converter para 1.4")

    def _add_files(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar arquivos", "", "Nuvem de pontos (*.las *.laz *.LAS *.LAZ)")
        self._add([Path(p) for p in ps])

    def _add_folder(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if f:
            self._add([Path(f)])

    def _add(self, paths) -> None:
        self._after_add(self.flist.add_paths(paths))

    def _after_add(self, n: int) -> None:
        if n:
            self.stats.set("total", len(self.flist.paths))
            self.status(f"{n} arquivo(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self) -> None:
        n = self.flist.remove_selected()
        self.stats.set("total", len(self.flist.paths))
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self) -> None:
        self.flist.reset_list()
        self.log.clear()
        self.stats.reset()
        self.status("Lista limpa.")

    def _pick_dest(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta de destino")
        if f:
            self.dest.setText(f)
            self.status(f"Destino: {f}")

    def _run(self) -> None:
        if not self.flist.paths:
            info(self, "Nada a fazer", "Adicione arquivos LAZ/LAS para conversão.")
            return
        if not self.dest.text():
            warn(self, "Atenção", "Selecione a pasta de destino.")
            return

        is_downgrade = self.rb_downgrade.isChecked()
        mode = "1.4->1.2" if is_downgrade else "1.2->1.4"
        n_arqs = len(self.flist.paths)

        if is_downgrade:
            msg = (
                f"Converter {n_arqs} arquivo(s) de LAZ/LAS 1.4 para 1.2?\n\n"
                "• Arquivos com point format 6-10 terão o formato ajustado "
                "(6→1, 7→3, 8→3, 9→1, 10→3); NIR e waveform, quando existirem, serão perdidos.\n"
                "• Arquivos COPC são aceitos: a indexação em octree é removida, mas todos os "
                "pontos são preservados.\n"
                "• Classificação: os valores de classe (inclusive acima de 31) são mantidos, "
                "gravando o byte completo (como Global Mapper e TerraScan fazem).\n"
                "• Ângulo de varredura: convertido para graus inteiros (-90° a +90°).\n\n"
                + ("ATENÇÃO: Os arquivos originais SERÃO SOBRESCRITOS diretamente na pasta de origem!" if overwrite else "Os arquivos de origem não são modificados.")
            )
        else:
            msg = (
                f"Converter {n_arqs} arquivo(s) de LAZ/LAS 1.2/1.3 para 1.4?\n\n"
                "• Formatos serão atualizados para 1.4 (0/1→6, 2/3→7, 4→9, 5→10).\n"
                "• Formatos 6 e 7 suportam nativamente até 15 retornos por pulso e 256 classes (8 bits).\n"
                "• Ângulo de varredura: expandido para precisão de 0.006° (int16).\n"
                "• Classificações originais (inclusive acima de 31) são integralmente mantidas.\n"
                "• Coordenadas, escalas, offsets e Projeção/CRS são preservados.\n\n"
                + ("ATENÇÃO: Os arquivos originais SERÃO SOBRESCRITOS diretamente na pasta de origem!" if overwrite else "Os arquivos de origem não são modificados.")
            )

        if not ask(self, "Confirmar conversão", msg):
            return

        self.ok = self.err = 0
        self.stats.reset("ok", "err")
        self.log.clear()
        self._start_pb(len(self.flist.paths))
        self._busy(True)
        self.runner.start(
            work_convert_version,
            list(self.flist.paths),
            self.dest.text(),
            mode,
            self.chk.isChecked(),
            overwrite,
        )

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
        elif kind == "file":
            name, ok, msg = a
            if ok:
                self.ok += 1
                self.stats.set("ok", self.ok)
                tag, prefix = "ok", "✔"
            else:
                self.err += 1
                self.stats.set("err", self.err)
                tag, prefix = "err", "✖"
            self.log.add(f"{prefix}  {name}  —  {msg}", tag)
        elif kind == "done":
            ok, erros = a
            self._busy(False)
            self.pb.setVisible(False)
            target_v = "1.2" if self.rb_downgrade.isChecked() else "1.4"
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) convertido(s) para {target_v}.")
                info(self, "Concluído", f"{ok} arquivo(s) convertido(s) para LAZ {target_v}.")
            else:
                self.status(f"Concluído com avisos — {ok} sucesso(s), {erros} problema(s).")
                warn(self, "Atenção",
                     f"{ok} arquivo(s) convertido(s) para LAZ {target_v}.\n"
                     f"{erros} arquivo(s) falharam ou foram ignorados. Veja o log.")



# ==========================================================================
#  ABA — EXPORTAR CLASSES (TerraScan Macro FnScanClassifyClass / FnScanOutput)
# ==========================================================================


# ==========================================================================
#  ABA — EXPORTAR CLASSES (TerraScan Macro FnScanClassifyClass / FnScanOutput)
# ==========================================================================

class ExportClassesTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self.ok = self.err = 0
        lay = QVBoxLayout(self)

        if not LASPY_AVAILABLE:
            lb = mklabel("A biblioteca laspy não está instalada.\\n"
                         "Instale com:  pip install laspy[lazrs]  e reinicie o programa.", "warn")
            lay.addWidget(lb)

        tb = QHBoxLayout()
        tb.addWidget(mklabel("ENTRADA (LAS / LAZ):", "sec"))
        self.btn_add = mkbtn("+ Adicionar", self._add_files)
        self.btn_dir = mkbtn("Pasta", self._add_folder)
        self.btn_clr = mkbtn("Limpar", self._clear, "danger")
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        self.chk_sub = QCheckBox("Incluir subpastas")
        self.chk_sub.setChecked(False)
        self.chk_sub.toggled.connect(lambda v: setattr(self.flist, "recursive", v))
        tb.addWidget(self.chk_sub)
        tb.addStretch(1)
        lay.addLayout(tb)

        lay.addWidget(mklabel("Arraste e solte arquivos .LAS ou .LAZ aqui", "hint"))
        self.flist = FileList((".las", ".laz"), recursive=False)
        self.flist.dropped.connect(self._after_add)
        lay.addWidget(self.flist, 1)

        # Configurações de Macro / Reclassificação / Filtro
        grp = QGroupBox("Configurações da Macro (Reclassificação e Filtro de Exportação)")
        gl = QVBoxLayout(grp)

        linha_macro = QHBoxLayout()
        linha_macro.addWidget(mkbtn("📂 Carregar Macro TerraScan (.mac)...", self._load_macro))
        linha_macro.addWidget(mkbtn("Preset MDT", self._preset_mdt))
        linha_macro.addWidget(mkbtn("Preset MDS", self._preset_mds))
        linha_macro.addStretch(1)
        gl.addLayout(linha_macro)

        meio = QHBoxLayout()

        # Tabela De -> Para
        esq_box = QVBoxLayout()
        esq_box.addWidget(mklabel("Regras de Reclassificação (De → Para):", "sec"))
        self.tab_reclass = QTableWidget(0, 2)
        self.tab_reclass.setHorizontalHeaderLabels(["Classe Origem (De)", "Classe Destino (Para)"])
        self.tab_reclass.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tab_reclass.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tab_reclass.setFixedHeight(120)
        esq_box.addWidget(self.tab_reclass)

        btn_reclass_row = QHBoxLayout()
        btn_reclass_row.addWidget(mkbtn("+ Adicionar Regra", self._add_reclass_row))
        btn_reclass_row.addWidget(mkbtn("- Remover Regra", self._remove_reclass_row))
        btn_reclass_row.addWidget(mkbtn("Limpar Regras", self._clear_reclass_rows))
        btn_reclass_row.addStretch(1)
        esq_box.addLayout(btn_reclass_row)
        meio.addLayout(esq_box, 1)

        # Classes a exportar
        dir_box = QVBoxLayout()
        dir_box.addWidget(mklabel("Classes para Exportar (Filtro final):", "sec"))
        self.edit_classes = QLineEdit()
        self.edit_classes.setPlaceholderText("Ex.: 2  ou  0-6,8,11,13-17,63,105-107 (vazio = todas)")
        dir_box.addWidget(self.edit_classes)

        dica = mklabel(
            "Digite as classes finais separadas por vírgula ou faixas com hífen.\\n"
            "Se deixar em branco, exportará todas as classes existentes.\\n"
            "Classes acima de 31 (ex: 63, 105, 141) são gravadas integralmente.",
            "hint",
        )
        dica.setWordWrap(True)
        dir_box.addWidget(dica)
        dir_box.addStretch(1)
        meio.addLayout(dir_box, 1)

        gl.addLayout(meio)
        lay.addWidget(grp)

        # Destino e formato
        opt, ol = card()
        r = QHBoxLayout()
        r.addWidget(mklabel("DESTINO:", "sec"))
        self.dest = QLineEdit()
        self.dest.setReadOnly(True)
        self.btn_dest = mkbtn("Selecionar pasta", self._pick_dest)
        r.addWidget(self.dest, 1)
        r.addWidget(self.btn_dest)

        r.addWidget(mklabel("Formato:", "sec"))
        self.combo_fmt = QComboBox()
        self.combo_fmt.addItems(["Manter original", ".las", ".laz"])
        r.addWidget(self.combo_fmt)

        r.addWidget(mklabel("Sufixo no nome:", "sec"))
        self.edit_sufixo = QLineEdit("_mdt")
        self.edit_sufixo.setFixedWidth(90)
        r.addWidget(self.edit_sufixo)
        ol.addLayout(r)
        lay.addWidget(opt)

        self.stats = Stats([("total", "Total na fila", False), ("ok", "Exportados", True),
                            ("err", "Erros", False)])
        lay.addWidget(self.stats)

        lay.addWidget(mklabel("Log de operações", "hint"))
        self.log = LogView(90)
        lay.addWidget(self.log)
        lay.addWidget(self.pb)

        bottom = QHBoxLayout()
        self.btn_rem = mkbtn("Remover selecionados", self._remove_sel)
        self.btn_run = mkbtn("▶  Exportar Classes", self._run, "primary")
        if not LASPY_AVAILABLE:
            self.btn_run.setEnabled(False)
        bottom.addWidget(self.btn_rem)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run)
        lay.addLayout(bottom)

        # Preset MDT por padrão
        self._preset_mdt()

    def _add_files(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar arquivos", "", "Nuvem de pontos (*.las *.laz *.LAS *.LAZ)")
        self._add([Path(p) for p in ps])

    def _add_folder(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if f:
            self._add([Path(f)])

    def _add(self, paths) -> None:
        self._after_add(self.flist.add_paths(paths))

    def _after_add(self, n: int) -> None:
        if n:
            self.stats.set("total", len(self.flist.paths))
            self.status(f"{n} arquivo(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self) -> None:
        n = self.flist.remove_selected()
        self.stats.set("total", len(self.flist.paths))
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self) -> None:
        self.flist.reset_list()
        self.log.clear()
        self.stats.reset()
        self.status("Lista limpa.")

    def _pick_dest(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Selecionar pasta de destino")
        if f:
            self.dest.setText(f)
            self.status(f"Destino: {f}")

    def _add_reclass_row(self, de: str = "", para: str = "") -> None:
        r = self.tab_reclass.rowCount()
        self.tab_reclass.insertRow(r)
        it_de = QTableWidgetItem(str(de))
        it_para = QTableWidgetItem(str(para))
        it_de.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it_para.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tab_reclass.setItem(r, 0, it_de)
        self.tab_reclass.setItem(r, 1, it_para)

    def _remove_reclass_row(self) -> None:
        rows = sorted([i.row() for i in self.tab_reclass.selectedItems()], reverse=True)
        for r in rows:
            self.tab_reclass.removeRow(r)

    def _clear_reclass_rows(self) -> None:
        self.tab_reclass.setRowCount(0)

    def _get_reclass_rules(self) -> list[tuple[int, int]]:
        rules = []
        for r in range(self.tab_reclass.rowCount()):
            it_de = self.tab_reclass.item(r, 0)
            it_para = self.tab_reclass.item(r, 1)
            if it_de and it_para:
                try:
                    c_de = int(it_de.text().strip())
                    c_para = int(it_para.text().strip())
                    rules.append((c_de, c_para))
                except ValueError:
                    pass
        return rules

    def _preset_mdt(self) -> None:
        self._clear_reclass_rows()
        self._add_reclass_row("8", "2")
        self._add_reclass_row("63", "2")
        self.edit_classes.setText("2")
        self.edit_sufixo.setText("_mdt")
        self.log.add("Preset MDT carregado: reclassifica 8,63→2 e exporta classe 2.", "muted")

    def _preset_mds(self) -> None:
        self._clear_reclass_rows()
        self._add_reclass_row("8", "2")
        self._add_reclass_row("63", "2")
        self._add_reclass_row("22", "1")
        self._add_reclass_row("0", "1")
        self.edit_classes.setText("0-6,8,11,13-17,63,105-107,121-122,141-144")
        self.edit_sufixo.setText("_mds")
        self.log.add("Preset MDS carregado: reclassifica 8,63→2, 22,0→1 e exporta classes do MDS.", "muted")

    def _load_macro(self) -> None:
        cam, _ = QFileDialog.getOpenFileName(
            self, "Carregar Macro TerraScan", "", "TerraScan Macro (*.mac);;Texto (*.txt);;Todos (*.*)"
        )
        if not cam:
            return
        try:
            reclass, classes_export = parse_terrascan_macro(Path(cam))
            self._clear_reclass_rows()
            for de, para in reclass:
                self._add_reclass_row(str(de), str(para))
            self.edit_classes.setText(classes_export)
            self.log.add(
                f"Macro carregada de '{Path(cam).name}': {len(reclass)} regra(s) de reclassificação, "
                f"classes exportadas: '{classes_export or 'todas'}'.",
                "ok",
            )
        except Exception as exc:
            warn(self, "Erro", f"Falha ao ler macro: {exc}")

    def _run(self) -> None:
        if not self.flist.paths:
            info(self, "Nada a fazer", "Adicione arquivos LAS ou LAZ.")
            return
        if not self.dest.text():
            warn(self, "Atenção", "Selecione a pasta de destino.")
            return

        reclass_rules = self._get_reclass_rules()
        texto_classes = self.edit_classes.text().strip()
        classes_export = parse_class_ranges(texto_classes) if texto_classes else []

        fmt_map = {"Manter original": "original", ".las": "las", ".laz": "laz"}
        formato = fmt_map.get(self.combo_fmt.currentText(), "original")
        sufixo = self.edit_sufixo.text().strip()

        msg = (
            f"Processar {len(self.flist.paths)} arquivo(s)?\\n\\n"
            f"- Regras de reclassificação: {len(reclass_rules)} regra(s)\\n"
            f"- Classes a exportar: {texto_classes if texto_classes else 'Todas as classes'}\\n"
            f"- Pasta de destino: {self.dest.text()}\\n"
            f"- Formato: {self.combo_fmt.currentText()}\\n\\n"
            "Os arquivos originais não serão alterados."
        )
        if not ask(self, "Confirmar exportação", msg):
            return

        self.ok = self.err = 0
        self.stats.reset("ok", "err")
        self.log.clear()
        self._start_pb(len(self.flist.paths))
        self._busy(True)
        self.runner.start(
            work_export_classes,
            list(self.flist.paths),
            self.dest.text(),
            reclass_rules,
            classes_export,
            formato,
            sufixo,
        )

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
        elif kind == "file":
            name, ok, msg = a
            if ok:
                self.ok += 1
                self.stats.set("ok", self.ok)
                tag, prefix = "ok", "✔"
            else:
                self.err += 1
                self.stats.set("err", self.err)
                tag, prefix = "err", "✖"
            self.log.add(f"{prefix}  {name}  —  {msg}", tag)
        elif kind == "done":
            ok, erros = a
            self._busy(False)
            self.pb.setVisible(False)
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) processado(s).")
                info(self, "Concluído", f"{ok} arquivo(s) exportado(s) com sucesso.")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                warn(self, "Concluído com erros", f"{ok} exportado(s).\\n{erros} com erro (veja o log).")

    def _busy(self, busy: bool) -> None:
        set_enabled([
            self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_dest,
            self.btn_rem, self.combo_fmt, self.edit_sufixo, self.edit_classes, self.tab_reclass,
        ], not busy)
        self.btn_run.setText("Exportando…" if busy else "▶  Exportar Classes")


# ==========================================================================
#  ABAS DE PROCESSAMENTO GEOESPACIAL (POOL TAB / KEYPOINTS / LATTICE)
# ==========================================================================

class PoolTab(ToolTab):
    """Pasta de entrada/saída + parâmetros + tabela de resultados + log."""

    HEADERS: list[str] = []
    SAIDA_LABEL = "Pasta de saída:"
    SPLIT_SIZES = (380, 440)

    def __init__(self, status) -> None:
        super().__init__(status)
        self.cancel_event = threading.Event()

        layout_principal = QVBoxLayout(self)

        grupo_pastas = QGroupBox("Pastas")
        self.form_pastas = QFormLayout(grupo_pastas)
        self.edit_entrada = QLineEdit()
        self.edit_saida = QLineEdit()
        self.form_pastas.addRow("Pasta com LAZ/LAS:",
                                browse_row(self.edit_entrada, "Selecione a pasta com LAZ/LAS"))
        self.form_pastas.addRow(self.SAIDA_LABEL,
                                browse_row(self.edit_saida, "Selecione a pasta de saída"))

        params_widget = self.build_params()

        self.btn_iniciar = mkbtn("Iniciar", self.on_iniciar, "primary")
        self.btn_cancelar = mkbtn("Cancelar", self.on_cancelar)
        self.btn_cancelar.setEnabled(False)
        botoes = QHBoxLayout()
        botoes.addWidget(self.btn_iniciar)
        botoes.addWidget(self.btn_cancelar)

        self.tabela = make_table(self.HEADERS)
        self.log = LogView(110)

        conteudo = QWidget()
        lc = QVBoxLayout(conteudo)
        lc.setContentsMargins(0, 0, 0, 0)
        lc.addWidget(grupo_pastas)
        lc.addWidget(params_widget)
        lc.addStretch(1)

        rolagem = QScrollArea()
        rolagem.setWidgetResizable(True)
        rolagem.setWidget(conteudo)
        rolagem.setFrameShape(QFrame.Shape.NoFrame)

        painel_params = QWidget()
        lp = QVBoxLayout(painel_params)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.addWidget(rolagem)
        lp.addLayout(botoes)
        lp.addWidget(self.pb)

        painel_res = QWidget()
        lr = QVBoxLayout(painel_res)
        lr.setContentsMargins(0, 0, 0, 0)
        lr.addWidget(self.tabela, 3)
        lr.addWidget(mklabel("Log:", "hint"))
        lr.addWidget(self.log, 2)

        divisor = QSplitter(Qt.Orientation.Vertical)
        divisor.addWidget(painel_params)
        divisor.addWidget(painel_res)
        divisor.setStretchFactor(0, 1)
        divisor.setStretchFactor(1, 1)
        divisor.setSizes(list(self.SPLIT_SIZES))
        layout_principal.addWidget(divisor)

    # ---- a implementar nas subclasses ----
    def build_params(self) -> QWidget:
        raise NotImplementedError

    def prepare(self, arquivos: list[Path], pasta_saida: str):
        """Retorna (target, args, total_barra) ou None se a validação falhar."""
        raise NotImplementedError

    def row_values(self, r) -> list:
        raise NotImplementedError

    def ok_path(self, r) -> str:
        raise NotImplementedError

    # ---- fluxo comum ----
    def on_iniciar(self) -> None:
        if not LASPY_AVAILABLE:
            self.log.add("Erro: instale o laspy (pip install laspy[lazrs]).", "err")
            return
        pasta_entrada = Path(self.edit_entrada.text().strip())
        pasta_saida = self.edit_saida.text().strip()

        if not pasta_entrada.is_dir():
            self.log.add("Erro: pasta de entrada inválida.", "err")
            return
        if not pasta_saida:
            self.log.add("Erro: informe a pasta de saída.", "err")
            return

        arquivos = sorted(list(pasta_entrada.glob("*.laz")) + list(pasta_entrada.glob("*.las")))
        if not arquivos:
            self.log.add("Nenhum arquivo .laz/.las encontrado na pasta de entrada.", "err")
            return

        job = self.prepare(arquivos, pasta_saida)
        if job is None:
            return
        target, args, total = job

        self.cancel_event.clear()
        self.tabela.setRowCount(0)
        self._start_pb(total)
        self.log.add(f"Iniciando processamento de {len(arquivos)} folha(s)...")
        self._busy(True)
        self.runner.start(target, *args)

    def on_cancelar(self) -> None:
        self.cancel_event.set()
        self.log.add("Cancelamento solicitado, encerrando processos em andamento...", "muted")

    def _busy(self, busy: bool) -> None:
        self.btn_iniciar.setEnabled(not busy)
        self.btn_cancelar.setEnabled(busy)

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
        elif kind == "log":
            self.log.add(a[0])
        elif kind == "result":
            r = a[0]
            nome = Path(r.laz_path).name
            table_add(self.tabela, [nome, "OK" if r.ok else "FALHA"] + self.row_values(r))
            if r.ok:
                self.log.add(f"[OK] {nome} -> {self.ok_path(r)}", "ok")
            else:
                self.log.add(f"[FALHA] {nome}: {r.message}", "err")

    def _finished(self) -> None:
        self._busy(False)
        self.pb.setVisible(False)
        self.log.add("Processamento finalizado.")


# ==========================================================================
#  ABA 7 — PONTOS-CHAVE (LAS)
# ==========================================================================

class KeyPointsTab(PoolTab):
    HEADERS = ["Folha", "Status", "Pts solo", "Pts afinados", "Grid (col x lin)", "Tempo (s)"]
    SAIDA_LABEL = "Pasta de saída:"

    def build_params(self) -> QWidget:
        g = QGroupBox("Parâmetros (equivalentes à macro FnScanClassifyModelKey / FnScanOutput)")
        f = QFormLayout(g)
        self.edit_classes = QLineEdit("2")
        f.addRow("Classe(s) de solo (entrada, separadas por vírgula):", self.edit_classes)
        self.spin_classe_chave = spin_i(0, 255, 8)
        f.addRow(
            "Classe dos pontos-chave (LAS; >31 usa byte bruto compatível com GM/TerraScan):",
            self.spin_classe_chave,
        )
        self.spin_resolucao = spin_d(0.1, 100.0, 1.0, 2, 0.5)
        f.addRow("Resolução do grid opcional (m):", self.spin_resolucao)
        self.spin_tolerancia = spin_d(0.001, 5.0, 0.05, 3, 0.01)
        f.addRow("Tolerância vertical (afinamento, m):", self.spin_tolerancia)
        self.spin_cell = spin_d(0.1, 20.0, 0.5, 2, 0.1)
        f.addRow("Célula de análise do afinamento (m):", self.spin_cell)
        self.spin_epsg = spin_i(1000, 99999, 31982)
        f.addRow("EPSG (só usado se exportar grid opcional):", self.spin_epsg)

        self.check_las = QCheckBox("Exportar LAS (pontos-chave, igual à macro TerraScan)")
        self.check_las.setChecked(True)
        self.check_saida_laz = QCheckBox("Gravar saída comprimida (.laz) em vez de .las")
        self.check_tif = QCheckBox("Exportar GeoTIFF (.tif) — grid interpolado, opcional")
        self.check_asc = QCheckBox("Exportar ASCII Grid (.asc) — importável no TerraModeler, opcional")
        f.addRow(self.check_las)
        f.addRow(self.check_saida_laz)
        f.addRow(self.check_tif)
        f.addRow(self.check_asc)

        self.spin_proc = spin_i(1, 32, 4)
        f.addRow("Processos paralelos:", self.spin_proc)
        return g

    def prepare(self, arquivos, pasta_saida):
        if not (self.check_las.isChecked() or self.check_tif.isChecked() or self.check_asc.isChecked()):
            self.log.add("Erro: selecione ao menos um formato de saída.", "err")
            return None
        classes = parse_classes(self.edit_classes.text())
        if not classes:
            self.log.add("Erro: informe ao menos uma classe de solo válida (ex.: 2 ou 2,9).", "err")
            return None
        lista = [
            ProcessParams(
                laz_path=str(arq),
                output_dir=pasta_saida,
                ground_classes=classes,
                resolution=self.spin_resolucao.value(),
                thin_tolerance=self.spin_tolerancia.value(),
                thin_cell_size=self.spin_cell.value(),
                export_las=self.check_las.isChecked(),
                model_key_class=self.spin_classe_chave.value(),
                export_geotiff=self.check_tif.isChecked(),
                export_ascii=self.check_asc.isChecked(),
                saida_laz=self.check_saida_laz.isChecked(),
                epsg=self.spin_epsg.value(),
            )
            for arq in arquivos
        ]
        return work_keypoints, (lista, self.spin_proc.value(), self.cancel_event), len(lista)

    def row_values(self, r: ProcessResult) -> list:
        return [r.n_pontos_solo, r.n_pontos_afinados, f"{r.n_col} x {r.n_lin}", f"{r.tempo_s:.1f}"]

    def ok_path(self, r: ProcessResult) -> str:
        return r.las_path or r.geotiff_path or r.ascii_path


# ==========================================================================
#  ABA 8 — GEOTIFF (LATTICE MODEL)
# ==========================================================================

class LatticeTab(PoolTab):
    HEADERS = ["Folha", "Status", "Pts no TIN", "Vizinhos usados", "Grid (col x lin)", "Tempo (s)"]
    SAIDA_LABEL = "Pasta de saída (Directory):"
    SPLIT_SIZES = (420, 400)

    def build_params(self) -> QWidget:
        g = QGroupBox("Parâmetros (equivalentes ao Export Lattice Model)")
        f = QFormLayout(g)
        self.edit_classes = QLineEdit("2,9,103")
        f.addRow("Class (classes separadas por vírgula):", self.edit_classes)
        self.spin_expand = spin_d(0.0, 10000.0, 1.0)
        f.addRow("Expand by (m):", self.spin_expand)
        self.spin_grid = spin_d(0.01, 1000.0, 1.0, 3)
        f.addRow("Grid spacing (m):", self.spin_grid)
        self.spin_max_tri = spin_d(0.1, 100000.0, 200.0)
        f.addRow("Max triangle (m, comprimento de aresta):", self.spin_max_tri)
        self.spin_buffer = spin_d(0.0, 100000.0, 100.0)
        f.addRow("Model buffer (m, busca em folhas vizinhas):", self.spin_buffer)
        self.spin_outside = spin_d(-1e9, 1e9, -99999.0)
        f.addRow("Outside Z (nodata):", self.spin_outside)
        self.check_tfw = QCheckBox("Create TFW files")
        self.check_tfw.setChecked(True)
        f.addRow(self.check_tfw)
        self.edit_ext = QLineEdit("tif")
        f.addRow("Extension:", self.edit_ext)
        self.spin_epsg = spin_i(1000, 99999, 31982)
        f.addRow("EPSG do GeoTIFF de saída:", self.spin_epsg)
        self.spin_proc = spin_i(1, 32, 4)
        f.addRow("Processos paralelos:", self.spin_proc)

        self.check_mosaico = QCheckBox("Gerar também um GeoTIFF geral (mosaico) com todas as folhas")
        self.check_mosaico.toggled.connect(lambda on: self.edit_nome_mosaico.setEnabled(on))
        f.addRow(self.check_mosaico)
        self.edit_nome_mosaico = QLineEdit("mosaico_geral.tif")
        self.edit_nome_mosaico.setEnabled(False)
        f.addRow("Nome do arquivo do mosaico:", self.edit_nome_mosaico)
        return g

    def prepare(self, arquivos, pasta_saida):
        classes = parse_classes(self.edit_classes.text())
        if not classes:
            self.log.add("Erro: informe ao menos uma classe válida (ex.: 2,9,103).", "err")
            return None
        kw = dict(
            output_dir=pasta_saida,
            classes=classes,
            expand_by=self.spin_expand.value(),
            grid_spacing=self.spin_grid.value(),
            max_triangle=self.spin_max_tri.value(),
            model_buffer=self.spin_buffer.value(),
            outside_z=self.spin_outside.value(),
            criar_tfw=self.check_tfw.isChecked(),
            extensao=self.edit_ext.text().strip() or "tif",
            epsg=self.spin_epsg.value(),
        )
        nome_mosaico = self.edit_nome_mosaico.text().strip() or "mosaico_geral.tif"
        args = (arquivos, kw, self.check_mosaico.isChecked(), nome_mosaico,
                self.spin_proc.value(), self.cancel_event)
        return work_lattice, args, len(arquivos)

    def row_values(self, r: LatticeResult) -> list:
        return [r.n_pontos_tin, r.n_vizinhos_usados, f"{r.n_col} x {r.n_lin}", f"{r.tempo_s:.1f}"]

    def ok_path(self, r: LatticeResult) -> str:
        return r.tif_path


# ==========================================================================
#  ABA 9 — CURVAS DE NÍVEL (CONTOURS)
# ==========================================================================



# ==========================================================================
#  ABA: CONVERSOR LAS/LAZ -> LAZ (STREAMING POR CHUNKS + RELATÓRIO CSV)
# ==========================================================================

# ----------------------------- utilitários -----------------------------

def human_bytes(num: Optional[int]) -> str:
    if num is None:
        return "-"
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    n = float(num)
    for u in units:
        if n < step:
            return f"{n:.2f} {u}"
        n /= step
    return f"{n:.2f} EB"


def now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_makedirs(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def open_in_file_manager(path: str) -> None:
    if not path:
        return
    p = path
    if os.path.isfile(p):
        p = os.path.dirname(p)
    if os.path.isdir(p):
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(p)))


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ----------------------------- widgets -----------------------------

class DropListWidget(QListWidget):
    """Lista com suporte a drag&drop de arquivos e pastas."""

    def __init__(self, owner=None):
        super().__init__(owner)
        self.owner = owner
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = []
        for u in urls:
            p = u.toLocalFile()
            if p:
                paths.append(p)
        if self.owner is not None:
            self.owner.handle_dropped_paths(paths)
        event.acceptProposedAction()


class SmartTable(QTableWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(6)
        self.setHorizontalHeaderLabels([
            "Status", "Arquivo", "Pontos", "Versão", "Saída", "Mensagem"
        ])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.Stretch)


# ----------------------------- dados -----------------------------

@dataclass
class Job:
    in_path: str
    root_for_rel: Optional[str] = None


@dataclass
class JobResult:
    in_path: str
    out_path: str
    ok: bool
    message: str
    in_size: Optional[int] = None
    out_size: Optional[int] = None
    points: Optional[int] = None
    seconds: Optional[float] = None
    out_version: Optional[str] = None
    out_point_format: Optional[int] = None


class WorkerSignals(QObject):
    log = Signal(str)
    overall = Signal(int, int)
    file_progress = Signal(int, int)
    current_file = Signal(str)
    file_result = Signal(object)
    finished = Signal(list)
    failed = Signal(str)


# ----------------------------- worker -----------------------------

class LasToLazWorker(QObject):
    def __init__(
        self,
        jobs: List[Job],
        out_mode: str,
        out_folder: str,
        out_subfolder: str,
        keep_structure: bool,
        overwrite: bool,
        verify: bool,
        move_original: bool,
        chunk_points: int,
        read_evlrs: bool,
        backend_choice: str,
        target_version_choice: str,
        signals: WorkerSignals,
        out_format: str = "LAZ",
    ):
        super().__init__()
        self.jobs = jobs
        self.out_mode = out_mode
        self.out_folder = out_folder
        self.out_subfolder = out_subfolder
        self.keep_structure = keep_structure
        self.overwrite = overwrite
        self.verify = verify
        self.move_original = move_original
        self.chunk_points = max(50_000, int(chunk_points))
        self.read_evlrs = read_evlrs
        self.backend_choice = backend_choice
        self.target_version_choice = target_version_choice
        self.signals = signals
        self.out_format = out_format
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _pick_backend(self):
        if LazBackend is None:
            return None
        available = LazBackend.detect_available()
        if self.backend_choice == "AUTO":
            laszip_bk = getattr(LazBackend, "Laszip", None)
            if laszip_bk in available:
                return laszip_bk
            return None
        mapping = {
            "LAZRS_PAR": getattr(LazBackend, "LazrsParallel", None),
            "LAZRS": getattr(LazBackend, "Lazrs", None),
            "LASZIP": getattr(LazBackend, "Laszip", None),
        }
        candidate = mapping.get(self.backend_choice)
        if candidate in available:
            return candidate
        return None

    def _calc_out_path(self, job: Job) -> str:
        in_path = job.in_path
        base_name = os.path.splitext(os.path.basename(in_path))[0]
        ext = ".laz" if getattr(self, "out_format", "LAZ") == "LAZ" else ".las"
        out_name = base_name + ext

        if self.out_mode == "NEXT_TO_INPUT":
            out_dir = os.path.dirname(in_path)
            if self.out_subfolder.strip():
                out_dir = os.path.join(out_dir, self.out_subfolder.strip())
            safe_makedirs(out_dir)
            return os.path.join(out_dir, out_name)

        out_dir = self.out_folder
        if self.keep_structure and job.root_for_rel:
            try:
                rel = os.path.relpath(os.path.dirname(in_path), job.root_for_rel)
                if rel not in (".", ""):
                    out_dir = os.path.join(out_dir, rel)
            except Exception:
                pass
        safe_makedirs(out_dir)
        return os.path.join(out_dir, out_name)

    def _header_summary(self, header) -> str:
        try:
            mins = getattr(header, "mins", None)
            maxs = getattr(header, "maxs", None)
            bbox = ""
            if mins is not None and maxs is not None:
                bbox = f"bbox=({mins[0]:.3f},{mins[1]:.3f},{mins[2]:.3f})..({maxs[0]:.3f},{maxs[1]:.3f},{maxs[2]:.3f})"
            return f"v{header.version}, fmt={header.point_format.id}, pts={header.point_count}, {bbox}"
        except Exception:
            return "header=?"

    def _choose_pf_for_12(self, src_header) -> int:
        src_dims = set(src_header.point_format.dimension_names)
        has_rgb = {"red", "green", "blue"}.issubset(src_dims)
        has_gps = "gps_time" in src_dims
        if has_rgb and has_gps:
            return 3
        if has_rgb:
            return 2
        if has_gps:
            return 1
        return 0

    def _copy_header_fields(self, src, dst):
        for attr in [
            "file_source_id", "system_identifier", "generating_software",
            "creation_date", "uuid", "global_encoding"
        ]:
            try:
                setattr(dst, attr, getattr(src, attr))
            except Exception:
                pass
        try:
            dst.scales = np.array(src.scales, dtype=float)
        except Exception:
            pass
        try:
            dst.offsets = np.array(src.offsets, dtype=float)
        except Exception:
            pass

    def _clone_vlrs_non_crs(self, src_header, dst_header, out_version: str, src_pf: int, out_pf: int):
        try:
            src_vlrs = list(src_header.vlrs)
        except Exception:
            src_vlrs = []

        def is_crs_vlr(vlr) -> bool:
            try:
                return (
                    getattr(vlr, "user_id", "") == "LASF_Projection"
                    and getattr(vlr, "record_id", None) in (2111, 2112, 34735, 34736, 34737)
                )
            except Exception:
                return False

        def is_copc_vlr(vlr) -> bool:
            try:
                uid = str(getattr(vlr, "user_id", "")).strip().lower()
                desc = str(getattr(vlr, "description", "")).strip().lower()
                rec = getattr(vlr, "record_id", None)
                return "copc" in uid or "copc" in desc or (uid == "entwine" and rec == 1)
            except Exception:
                return False

        def is_extra_bytes_vlr(vlr) -> bool:
            try:
                return getattr(vlr, "user_id", "") == "LASF_Spec" and getattr(vlr, "record_id", None) == 4
            except Exception:
                return False

        skipped = 0
        for vlr in src_vlrs:
            if is_crs_vlr(vlr) or is_copc_vlr(vlr):
                skipped += 1
                continue
            if out_version == "1.2" and (src_pf != out_pf) and is_extra_bytes_vlr(vlr):
                skipped += 1
                continue
            try:
                dst_header.vlrs.append(vlr)
            except Exception:
                skipped += 1

        if skipped:
            self.signals.log.emit(f"[{now_stamp()}] VLRs copiados com filtros. Ignorados: {skipped}")

    def _apply_crs(self, src_header, dst_header):
        try:
            crs = src_header.parse_crs()
        except Exception:
            crs = None
        if crs is None:
            return
        try:
            dst_header.add_crs(crs, keep_compatibility=True)
            self.signals.log.emit(f"[{now_stamp()}] CRS preservado via add_crs().")
        except Exception as e:
            self.signals.log.emit(f"[{now_stamp()}] Aviso CRS: {e}")

    def _build_output_header(self, src_header) -> Tuple[object, str, int]:
        src_version = str(src_header.version)
        src_pf = int(src_header.point_format.id)

        if self.target_version_choice == "KEEP":
            out_version = src_version
        else:
            out_version = self.target_version_choice

        out_pf = src_pf
        if out_version == "1.2" and src_pf > 3:
            out_pf = self._choose_pf_for_12(src_header)

        new_hdr = laspy.LasHeader(version=out_version, point_format=out_pf)
        self._copy_header_fields(src_header, new_hdr)
        self._apply_crs(src_header, new_hdr)
        self._clone_vlrs_non_crs(src_header, new_hdr, out_version, src_pf, out_pf)

        if out_version == "1.4":
            try:
                evlrs = getattr(src_header, "evlrs", None)
                if evlrs:
                    new_hdr.evlrs = [v for v in evlrs if "copc" not in str(getattr(v, "user_id", "")).lower()]
            except Exception:
                pass

        return new_hdr, out_version, out_pf

    def _convert_chunk_if_needed(self, input_chunk, buffer_chunk, src_dims: set, dst_dims: set,
                                 out_version: str) -> Tuple[object, Dict[str, int]]:
        stats = {
            "clip_return": 0,
            "clip_nreturns": 0,
            "clip_class": 0,
        }

        n = len(input_chunk)
        out_chunk = buffer_chunk[:n]
        out_chunk.copy_fields_from(input_chunk)

        if "scan_angle" in src_dims and "scan_angle_rank" in dst_dims:
            try:
                ang = np.asarray(input_chunk["scan_angle"])
                out_chunk["scan_angle_rank"] = np.clip(np.rint(ang), -90, 90).astype(np.int8)
            except Exception:
                pass
        elif "scan_angle_rank" in src_dims and "scan_angle" in dst_dims:
            try:
                ang = np.asarray(input_chunk["scan_angle_rank"], dtype=np.float64)
                out_chunk["scan_angle"] = np.clip(np.round(ang / 0.006), -30000, 30000).astype(np.int16)
            except Exception:
                pass

        if out_version == "1.2":
            if "return_number" in dst_dims and "return_number" in src_dims:
                rn = np.asarray(input_chunk["return_number"])
                c = int(np.count_nonzero(rn > 7))
                if c:
                    stats["clip_return"] += c
                out_chunk["return_number"] = np.minimum(rn, 7).astype(out_chunk["return_number"].dtype)

            if "number_of_returns" in dst_dims and "number_of_returns" in src_dims:
                nr = np.asarray(input_chunk["number_of_returns"])
                c = int(np.count_nonzero(nr > 7))
                if c:
                    stats["clip_nreturns"] += c
                out_chunk["number_of_returns"] = np.minimum(nr, 7).astype(out_chunk["number_of_returns"].dtype)

            if "classification" in dst_dims and "classification" in src_dims:
                cl = np.asarray(input_chunk["classification"])
                c = int(np.count_nonzero(cl > 31))
                if c:
                    stats["clip_class"] += c
                out_chunk["classification"] = np.minimum(cl, 31).astype(out_chunk["classification"].dtype)

        return out_chunk, stats

    def run(self):
        results: List[JobResult] = []

        if laspy is None:
            self.signals.failed.emit("Não consegui importar 'laspy'. Instale com: pip install laspy[lazrs]")
            self.signals.finished.emit(results)
            return

        if LazBackend is not None:
            available = LazBackend.detect_available()
            if not available:
                self.signals.failed.emit("Nenhum backend LAZ disponível. Instale: pip install laspy[lazrs]")
                self.signals.finished.emit(results)
                return

        laz_backend = self._pick_backend()
        total_files = len(self.jobs)
        done_files = 0

        self.signals.log.emit(f"[{now_stamp()}] Iniciando conversão de {total_files} arquivo(s).")
        if LazBackend is not None:
            self.signals.log.emit(f"[{now_stamp()}] Backends disponíveis: {LazBackend.detect_available()}")
            self.signals.log.emit(f"[{now_stamp()}] Backend selecionado: {self.backend_choice} -> {laz_backend}")
        self.signals.log.emit(f"[{now_stamp()}] Versão de saída: {self.target_version_choice}")

        for job in self.jobs:
            if self._cancel:
                self.signals.log.emit(f"[{now_stamp()}] Cancelado pelo usuário.")
                break

            in_path = job.in_path
            self.signals.current_file.emit(in_path)

            try:
                if not os.path.exists(in_path):
                    raise FileNotFoundError(f"Arquivo não existe: {in_path}")

                ext = os.path.splitext(in_path)[1].lower()
                if ext not in (".las", ".laz"):
                    raise ValueError("Entrada inválida. Aceito apenas .las e .laz")

                out_path = self._calc_out_path(job)
                if not out_path:
                    raise ValueError("Pasta de saída inválida.")

                if os.path.exists(out_path) and not self.overwrite:
                    msg = "Saída já existe. Arquivo pulado porque sobrescrever está desligado."
                    result = JobResult(in_path, out_path, True, msg)
                    results.append(result)
                    self.signals.file_result.emit(result)
                    done_files += 1
                    self.signals.overall.emit(done_files, total_files)
                    self.signals.file_progress.emit(0, 0)
                    continue

                safe_makedirs(os.path.dirname(out_path))
                t0 = time.time()
                in_size = os.path.getsize(in_path)

                with laspy.open(in_path, mode="r", read_evlrs=self.read_evlrs, laz_backend=laz_backend) as reader:
                    src_header = reader.header
                    total_points = int(getattr(src_header, "point_count", 0) or 0)
                    src_version = str(src_header.version)
                    src_pf = int(src_header.point_format.id)

                    self.signals.log.emit(f"[{now_stamp()}] Lendo: {in_path} | {self._header_summary(src_header)}")

                    out_header, out_version, out_pf = self._build_output_header(src_header)
                    if out_version == "1.2" and src_version == "1.4" and src_pf >= 6:
                        self.signals.log.emit(
                            f"[{now_stamp()}] Downgrade detectado: v{src_version}/PF{src_pf} -> v{out_version}/PF{out_pf}"
                        )
                    elif out_version == "1.4" and src_version in ("1.2", "1.3") and src_pf <= 3:
                        self.signals.log.emit(
                            f"[{now_stamp()}] Upgrade detectado: v{src_version}/PF{src_pf} -> v{out_version}/PF{out_pf}"
                        )

                    src_dims = set(src_header.point_format.dimension_names)
                    dst_dims = set(out_header.point_format.dimension_names)
                    need_pf_convert = (src_pf != out_pf)

                    is_out_laz = out_path.lower().endswith(".laz")
                    writer_kw = {
                        "mode": "w",
                        "header": out_header,
                    }
                    if is_out_laz:
                        writer_kw["do_compress"] = True
                        if laz_backend is not None:
                            writer_kw["laz_backend"] = laz_backend

                    with laspy.open(out_path, **writer_kw) as writer:
                        done_pts = 0
                        if total_points <= 0:
                            self.signals.file_progress.emit(0, 0)
                        else:
                            self.signals.file_progress.emit(0, total_points)

                        if need_pf_convert:
                            buffer_chunk = laspy.PackedPointRecord.zeros(
                                point_count=self.chunk_points,
                                point_format=out_header.point_format,
                            )
                            agg = {"clip_return": 0, "clip_nreturns": 0, "clip_class": 0}

                            for input_chunk in reader.chunk_iterator(self.chunk_points):
                                if self._cancel:
                                    raise RuntimeError("Cancelado durante a escrita.")

                                out_chunk, st = self._convert_chunk_if_needed(
                                    input_chunk, buffer_chunk, src_dims, dst_dims, out_version
                                )
                                agg["clip_return"] += st["clip_return"]
                                agg["clip_nreturns"] += st["clip_nreturns"]
                                agg["clip_class"] += st["clip_class"]
                                writer.write_points(out_chunk)
                                done_pts += len(input_chunk)
                                self.signals.file_progress.emit(done_pts, total_points)

                            if out_version == "1.2" and any(agg.values()):
                                self.signals.log.emit(
                                    f"[{now_stamp()}] Ajustes 1.2 | return>{agg['clip_return']} | nreturns>{agg['clip_nreturns']} | class>{agg['clip_class']}"
                                )
                        else:
                            for points in reader.chunk_iterator(self.chunk_points):
                                if self._cancel:
                                    raise RuntimeError("Cancelado durante a escrita.")
                                writer.write_points(points)
                                done_pts += len(points)
                                self.signals.file_progress.emit(done_pts, total_points)

                out_size = os.path.getsize(out_path)
                secs = time.time() - t0

                if self.verify:
                    chk_kw = {"mode": "r", "read_evlrs": self.read_evlrs}
                    if is_out_laz and laz_backend is not None:
                        chk_kw["laz_backend"] = laz_backend
                    with laspy.open(out_path, **chk_kw) as chk:
                        out_pts = int(getattr(chk.header, "point_count", -1))
                    if total_points >= 0 and out_pts != total_points:
                        raise RuntimeError(f"Verificação falhou: saída={out_pts} pontos | entrada={total_points} pontos")

                if self.move_original:
                    orig_dir = os.path.dirname(in_path)
                    stash = os.path.join(orig_dir, "_LAS_ORIG")
                    safe_makedirs(stash)
                    dst = os.path.join(stash, os.path.basename(in_path))
                    if os.path.abspath(dst) != os.path.abspath(in_path):
                        if os.path.exists(dst):
                            base, ext0 = os.path.splitext(dst)
                            dst = f"{base}_{int(time.time())}{ext0}"
                        shutil.move(in_path, dst)
                        self.signals.log.emit(f"[{now_stamp()}] Original movido para: {dst}")

                savings = ""
                if in_size and out_size:
                    pct = (1.0 - (out_size / in_size)) * 100.0
                    if is_out_laz:
                        savings = f" | {human_bytes(in_size)} -> {human_bytes(out_size)} ({pct:+.2f}%)"
                    else:
                        savings = f" | {human_bytes(in_size)} -> {human_bytes(out_size)} (descomprimido)"

                msg = f"OK em {secs:.1f}s{savings}"
                self.signals.log.emit(f"[{now_stamp()}] {msg} | {out_path}")
                result = JobResult(
                    in_path=in_path,
                    out_path=out_path,
                    ok=True,
                    message=msg,
                    in_size=in_size,
                    out_size=out_size,
                    points=total_points,
                    seconds=secs,
                    out_version=out_version,
                    out_point_format=out_pf,
                )
                results.append(result)
                self.signals.file_result.emit(result)

            except Exception as e:
                tb = traceback.format_exc()
                self.signals.log.emit(f"[{now_stamp()}] ERRO: {in_path}\n{e}\n{tb}")
                out_guess = ""
                try:
                    out_guess = self._calc_out_path(job)
                except Exception:
                    pass
                result = JobResult(in_path, out_guess, False, str(e))
                results.append(result)
                self.signals.file_result.emit(result)

            done_files += 1
            self.signals.overall.emit(done_files, total_files)
            self.signals.file_progress.emit(0, 0)

        self.signals.finished.emit(results)


# ----------------------------- aba principal -----------------------------

class SmartLasToLazTab(QWidget):
    def __init__(self, status=None):
        super().__init__()
        self.status_cb = status or (lambda msg: None)
        self.settings = QSettings("LAS_Toolkit", "ConversorInteligente")
        self.root_map: Dict[str, Optional[str]] = {}
        self.results_last: List[JobResult] = []
        self.file_to_row: Dict[str, int] = {}
        self.thread: Optional[QThread] = None
        self.worker: Optional[LasToLazWorker] = None
        self.signals: Optional[WorkerSignals] = None

        self._build_ui()
        self.load_settings()
        self.refresh_out_mode_ui()
        self.append_log(f"[{now_stamp()}] Aba Conversor LAS → LAZ iniciada.")

        if laspy is None:
            self.append_log(f"[{now_stamp()}] AVISO: laspy não importou. Instale: pip install laspy[lazrs]")
        else:
            try:
                if LazBackend is not None:
                    self.append_log(f"[{now_stamp()}] Backends disponíveis: {LazBackend.detect_available()}")
            except Exception:
                pass

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # topo com cards simples
        top_cards = QHBoxLayout()
        card_files, self.lbl_count_files = self._make_card("Arquivos na lista", "0")
        card_ok, self.lbl_count_ok = self._make_card("OK no último run", "0")
        card_fail, self.lbl_count_fail = self._make_card("Falhas no último run", "0")
        card_size, self.lbl_est_size = self._make_card("Tamanho estimado", "-")
        top_cards.addWidget(card_files)
        top_cards.addWidget(card_ok)
        top_cards.addWidget(card_fail)
        top_cards.addWidget(card_size)
        main_layout.addLayout(top_cards)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        # grupo arquivos
        files_box = QGroupBox("Arquivos LAS/LAZ")
        files_layout = QVBoxLayout(files_box)

        filter_row = QHBoxLayout()
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("Filtrar lista por nome, caminho ou extensão...")
        self.ed_filter.textChanged.connect(self.apply_filter)
        self.btn_clear_filter = QPushButton("Limpar filtro")
        self.btn_clear_filter.clicked.connect(lambda: self.ed_filter.setText(""))
        filter_row.addWidget(self.ed_filter)
        filter_row.addWidget(self.btn_clear_filter)
        files_layout.addLayout(filter_row)

        self.listw = DropListWidget(self)
        self.listw.itemSelectionChanged.connect(self.update_preview_from_selection)
        files_layout.addWidget(self.listw)

        btn_row = QHBoxLayout()
        self.btn_add_files = QPushButton("+ Adicionar arquivos")
        self.btn_add_folder = QPushButton("Pasta")
        self.btn_remove = QPushButton("Remover selecionados")
        self.btn_clear = QPushButton("Limpar lista")
        self.btn_add_files.clicked.connect(self.add_files)
        self.btn_add_folder.clicked.connect(self.add_folder)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear_list)
        btn_row.addWidget(self.btn_add_files)
        btn_row.addWidget(self.btn_add_folder)
        btn_row.addWidget(self.btn_remove)
        btn_row.addWidget(self.btn_clear)
        files_layout.addLayout(btn_row)

        left_layout.addWidget(files_box)

        # execução
        run_box = QGroupBox("Execução")
        run_layout = QGridLayout(run_box)
        self.lbl_current = QLabel("Arquivo atual: -")
        self.pb_file = QProgressBar()
        self.pb_file.setFormat("Arquivo: %p%")
        self.pb_overall = QProgressBar()
        self.pb_overall.setFormat("Geral: %p%")
        self.btn_start = QPushButton("▶  Converter")
        self.btn_start.setStyleSheet("font-weight: bold;")
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_open_out = QPushButton("Abrir saída")
        self.btn_start.clicked.connect(self.start_conversion)
        self.btn_cancel.clicked.connect(self.cancel_conversion)
        self.btn_open_out.clicked.connect(self.open_output_folder)
        self.btn_cancel.setEnabled(False)

        run_layout.addWidget(self.lbl_current, 0, 0, 1, 3)
        run_layout.addWidget(self.pb_file, 1, 0, 1, 3)
        run_layout.addWidget(self.pb_overall, 2, 0, 1, 3)
        run_layout.addWidget(self.btn_start, 3, 0)
        run_layout.addWidget(self.btn_cancel, 3, 1)
        run_layout.addWidget(self.btn_open_out, 3, 2)
        left_layout.addWidget(run_box)

        # tabela de resultados
        table_box = QGroupBox("Status por arquivo")
        table_layout = QVBoxLayout(table_box)
        self.table = SmartTable()
        table_layout.addWidget(self.table)
        left_layout.addWidget(table_box)

        # logs
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        log_layout.addWidget(self.log)
        log_btns = QHBoxLayout()
        self.btn_clear_log = QPushButton("Limpar log")
        self.btn_save_log = QPushButton("Salvar log")
        self.btn_save_report = QPushButton("Salvar relatório CSV")
        self.btn_clear_log.clicked.connect(self.log.clear)
        self.btn_save_log.clicked.connect(self.save_log)
        self.btn_save_report.clicked.connect(self.save_report_from_last_results)
        log_btns.addWidget(self.btn_clear_log)
        log_btns.addWidget(self.btn_save_log)
        log_btns.addWidget(self.btn_save_report)
        log_layout.addLayout(log_btns)
        left_layout.addWidget(log_box)

        # configurações
        cfg_box = QGroupBox("Configurações")
        cfg = QGridLayout(cfg_box)

        self.cb_out_mode = QComboBox()
        self.cb_out_mode.addItem("Salvar em uma pasta", "OUT_FOLDER")
        self.cb_out_mode.addItem("Salvar ao lado do arquivo", "NEXT_TO_INPUT")
        self.cb_out_mode.currentIndexChanged.connect(self.refresh_out_mode_ui)

        self.ed_out_folder = QLineEdit()
        self.ed_out_folder.setPlaceholderText("Selecione a pasta de saída...")
        self.btn_pick_out = QPushButton("...")
        self.btn_pick_out.clicked.connect(self.pick_out_folder)

        self.ed_subfolder = QLineEdit("LAZ")
        self.ed_subfolder.setPlaceholderText("Ex.: LAZ")

        self.chk_keep_structure = QCheckBox("Manter estrutura de pastas")
        self.chk_keep_structure.setChecked(True)

        self.cb_out_format = QComboBox()
        self.cb_out_format.addItem(".LAZ (Comprimido)", "LAZ")
        self.cb_out_format.addItem(".LAS (Descomprimido)", "LAS")

        self.cb_out_version = QComboBox()
        self.cb_out_version.addItem("Manter igual ao arquivo", "KEEP")
        self.cb_out_version.addItem("LAS 1.4", "1.4")
        self.cb_out_version.addItem("LAS 1.2", "1.2")
        self.cb_out_version.setCurrentIndex(1)

        self.cb_backend = QComboBox()
        self.cb_backend.addItem("Auto (compatível V8i / laszip)", "AUTO")
        self.cb_backend.addItem("laszip (oficial C++ / V8i)", "LASZIP")
        self.cb_backend.addItem("lazrs parallel (mais rápido)", "LAZRS_PAR")
        self.cb_backend.addItem("lazrs single", "LAZRS")
        self.btn_check_backends = QPushButton("Checar backends")
        self.btn_check_backends.clicked.connect(self.check_backends)

        self.sp_chunk = QSpinBox()
        self.sp_chunk.setRange(50_000, 50_000_000)
        self.sp_chunk.setSingleStep(50_000)
        self.sp_chunk.setValue(50_000)

        self.chk_overwrite = QCheckBox("Sobrescrever se existir")
        self.chk_verify = QCheckBox("Verificar contagem de pontos após converter")
        self.chk_move_original = QCheckBox("Mover original para _LAS_ORIG após sucesso")
        self.chk_read_evlrs = QCheckBox("Ler EVLRs (LAS 1.4)")
        self.chk_verify.setChecked(True)
        self.chk_read_evlrs.setChecked(True)

        row = 0
        cfg.addWidget(QLabel("Modo de saída:"), row, 0)
        cfg.addWidget(self.cb_out_mode, row, 1, 1, 2)
        row += 1
        cfg.addWidget(QLabel("Pasta de saída:"), row, 0)
        cfg.addWidget(self.ed_out_folder, row, 1)
        cfg.addWidget(self.btn_pick_out, row, 2)
        row += 1
        cfg.addWidget(QLabel("Subpasta (modo ao lado):"), row, 0)
        cfg.addWidget(self.ed_subfolder, row, 1, 1, 2)
        row += 1
        cfg.addWidget(self.chk_keep_structure, row, 0, 1, 3)
        row += 1
        cfg.addWidget(QLabel("Formato de saída:"), row, 0)
        cfg.addWidget(self.cb_out_format, row, 1, 1, 2)
        row += 1
        cfg.addWidget(QLabel("Versão LAS de saída:"), row, 0)
        cfg.addWidget(self.cb_out_version, row, 1, 1, 2)
        row += 1
        cfg.addWidget(QLabel("Backend LAZ:"), row, 0)
        cfg.addWidget(self.cb_backend, row, 1, 1, 2)
        row += 1
        cfg.addWidget(self.btn_check_backends, row, 0, 1, 3)
        row += 1
        cfg.addWidget(QLabel("Chunk (pontos):"), row, 0)
        cfg.addWidget(self.sp_chunk, row, 1, 1, 2)
        row += 1
        cfg.addWidget(self.chk_overwrite, row, 0, 1, 3)
        row += 1
        cfg.addWidget(self.chk_verify, row, 0, 1, 3)
        row += 1
        cfg.addWidget(self.chk_move_original, row, 0, 1, 3)
        row += 1
        cfg.addWidget(self.chk_read_evlrs, row, 0, 1, 3)
        right_layout.addWidget(cfg_box)

        # preview
        prev_box = QGroupBox("Preview do arquivo selecionado")
        prev_layout = QVBoxLayout(prev_box)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        prev_layout.addWidget(self.preview)
        right_layout.addWidget(prev_box, 1)

        tip = QLabel(
            "Dica: arraste arquivos ou pastas (.las ou .laz) para a lista. Suporta conversão nos dois sentidos: compactar (LAS → LAZ) e descompactar (LAZ → LAS)."
        )
        tip.setWordWrap(True)
        tip.setFrameStyle(QFrame.Panel | QFrame.Sunken)
        tip.setStyleSheet("padding:6px; color:#555;")
        main_layout.addWidget(tip)

    def _make_card(self, title: str, value: str):
        box = QFrame()
        box.setObjectName("card")
        box.setFrameShape(QFrame.StyledPanel)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        box.setStyleSheet("""
            QFrame#card {
                border: 1px solid #c8c8c8;
                border-radius: 8px;
                background: #fdfdfd;
                padding: 4px;
            }
        """)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(10, 8, 10, 8)

        lbl_title = QLabel(title, box)
        lbl_title.setStyleSheet("color: #666; font-size: 11px;")

        lbl_value = QLabel(value, box)
        lbl_value.setStyleSheet("font-size: 20px; font-weight: bold;")

        lay.addWidget(lbl_title)
        lay.addWidget(lbl_value)
        return box, lbl_value

    # ---------------------- persistência ----------------------

    def save_settings(self):
        try:
            self.settings.setValue("out_mode", self.cb_out_mode.currentIndex())
            self.settings.setValue("out_folder", self.ed_out_folder.text().strip())
            self.settings.setValue("subfolder", self.ed_subfolder.text().strip())
            self.settings.setValue("keep_structure", self.chk_keep_structure.isChecked())
            self.settings.setValue("out_format", self.cb_out_format.currentIndex())
            self.settings.setValue("out_version", self.cb_out_version.currentIndex())
            self.settings.setValue("backend", self.cb_backend.currentIndex())
            self.settings.setValue("chunk_v2", self.sp_chunk.value())
            self.settings.setValue("overwrite", self.chk_overwrite.isChecked())
            self.settings.setValue("verify", self.chk_verify.isChecked())
            self.settings.setValue("move_original", self.chk_move_original.isChecked())
            self.settings.setValue("read_evlrs", self.chk_read_evlrs.isChecked())
        except Exception:
            pass

    def load_settings(self):
        try:
            self.cb_out_mode.setCurrentIndex(safe_int(self.settings.value("out_mode", 0)))
            self.ed_out_folder.setText(str(self.settings.value("out_folder", "")))
            self.ed_subfolder.setText(str(self.settings.value("subfolder", "LAZ")))
            self.chk_keep_structure.setChecked(str(self.settings.value("keep_structure", "true")).lower() == "true")
            self.cb_out_format.setCurrentIndex(safe_int(self.settings.value("out_format", 0)))
            self.cb_out_version.setCurrentIndex(safe_int(self.settings.value("out_version", 1)))
            self.cb_backend.setCurrentIndex(safe_int(self.settings.value("backend", 0)))
            self.sp_chunk.setValue(safe_int(self.settings.value("chunk_v2", 50000), 50000))
            self.chk_overwrite.setChecked(str(self.settings.value("overwrite", "false")).lower() == "true")
            self.chk_verify.setChecked(str(self.settings.value("verify", "true")).lower() == "true")
            self.chk_move_original.setChecked(str(self.settings.value("move_original", "false")).lower() == "true")
            self.chk_read_evlrs.setChecked(str(self.settings.value("read_evlrs", "true")).lower() == "true")
        except Exception:
            pass

    # ---------------------- helpers UI ----------------------

    def append_log(self, txt: str):
        self.log.appendPlainText(txt)
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log.setTextCursor(cursor)

    def refresh_out_mode_ui(self):
        mode = self.cb_out_mode.currentData()
        is_out_folder = mode == "OUT_FOLDER"
        self.ed_out_folder.setEnabled(is_out_folder)
        self.btn_pick_out.setEnabled(is_out_folder)
        self.chk_keep_structure.setEnabled(is_out_folder)
        self.ed_subfolder.setEnabled(not is_out_folder)

    def pick_out_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Selecione a pasta de saída")
        if d:
            self.ed_out_folder.setText(d)

    def open_output_folder(self):
        mode = self.cb_out_mode.currentData()
        if mode == "OUT_FOLDER":
            out = self.ed_out_folder.text().strip()
            if out and os.path.isdir(out):
                open_in_file_manager(out)
                return
            QMessageBox.information(self, "Saída", "Selecione uma pasta de saída válida.")
            return

        if self.listw.count() == 0:
            QMessageBox.information(self, "Saída", "Sem arquivos na lista.")
            return

        p = self.listw.item(0).data(Qt.UserRole)
        if p and os.path.exists(p):
            out_dir = os.path.dirname(p)
            sub = self.ed_subfolder.text().strip()
            if sub:
                out_dir = os.path.join(out_dir, sub)
            safe_makedirs(out_dir)
            open_in_file_manager(out_dir)

    def update_cards(self):
        self.lbl_count_files.setText(str(self.listw.count()))
        ok = sum(1 for r in self.results_last if r.ok)
        fail = sum(1 for r in self.results_last if not r.ok)
        self.lbl_count_ok.setText(str(ok))
        self.lbl_count_fail.setText(str(fail))

        total = 0
        for i in range(self.listw.count()):
            p = self.listw.item(i).data(Qt.UserRole)
            if p and os.path.exists(p):
                try:
                    total += os.path.getsize(p)
                except Exception:
                    pass
        self.lbl_est_size.setText(human_bytes(total) if total else "-")

    def apply_filter(self, text: str):
        text = (text or "").strip().lower()
        for i in range(self.listw.count()):
            item = self.listw.item(i)
            raw = item.data(Qt.UserRole) or ""
            item.setHidden(text not in raw.lower())

    # ---------------------- lista ----------------------

    def handle_dropped_paths(self, paths: List[str]):
        total_added = 0
        for p in paths:
            if os.path.isdir(p):
                total_added += self._add_folder_internal(p, silent=True)
            else:
                if self._add_file_internal(p, root_for_rel=os.path.dirname(p), silent=True):
                    total_added += 1
        self.append_log(f"[{now_stamp()}] Drag&drop concluído. Adicionados: {total_added}")
        self.update_cards()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Selecione arquivos LAS/LAZ",
            "",
            "LAS/LAZ (*.las *.laz *.LAS *.LAZ)"
        )
        added = 0
        for f in files:
            if self._add_file_internal(f, root_for_rel=os.path.dirname(f), silent=True):
                added += 1
        self.append_log(f"[{now_stamp()}] Arquivos adicionados: {added}")
        self.update_cards()

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecione a pasta com LAS/LAZ")
        if folder:
            added = self._add_folder_internal(folder)
            self.append_log(f"[{now_stamp()}] Pasta adicionada: {folder} | {added} arquivo(s)")
            self.update_cards()

    def _add_folder_internal(self, folder: str, silent: bool = False) -> int:
        added = 0
        for root, _, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith((".las", ".laz")):
                    p = os.path.join(root, fn)
                    if self._add_file_internal(p, root_for_rel=folder, silent=True):
                        added += 1
        if not silent:
            self.append_log(f"[{now_stamp()}] Pasta adicionada: {folder} | {added} arquivo(s)")
        return added

    def _add_file_internal(self, path: str, root_for_rel: Optional[str], silent: bool = False) -> bool:
        if not path.lower().endswith((".las", ".laz")):
            return False
        path = os.path.abspath(path)
        if path in self.root_map:
            return False

        item = QListWidgetItem(path)
        item.setData(Qt.UserRole, path)
        item.setData(Qt.UserRole + 1, root_for_rel)
        self.listw.addItem(item)
        self.root_map[path] = root_for_rel
        self._ensure_table_row(path)
        if not silent:
            self.append_log(f"[{now_stamp()}] Adicionado: {path}")
        return True

    def remove_selected(self):
        selected_paths = []
        for it in self.listw.selectedItems():
            p = it.data(Qt.UserRole)
            selected_paths.append(p)
        for p in selected_paths:
            self._remove_path(p)
        self.preview.clear()
        self.update_cards()

    def _remove_path(self, path: str):
        for i in range(self.listw.count() - 1, -1, -1):
            item = self.listw.item(i)
            if item.data(Qt.UserRole) == path:
                self.listw.takeItem(i)
                break
        self.root_map.pop(path, None)
        self.rebuild_table_from_list()

    def clear_list(self):
        self.listw.clear()
        self.root_map.clear()
        self.preview.clear()
        self.table.setRowCount(0)
        self.file_to_row.clear()
        self.update_cards()

    # ---------------------- tabela ----------------------

    def rebuild_table_from_list(self):
        self.table.setRowCount(0)
        self.file_to_row.clear()
        for i in range(self.listw.count()):
            path = self.listw.item(i).data(Qt.UserRole)
            self._ensure_table_row(path)

    def _ensure_table_row(self, path: str):
        if path in self.file_to_row:
            return self.file_to_row[path]
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.file_to_row[path] = row
        self._set_table_item(row, 0, "Na fila")
        self._set_table_item(row, 1, path)
        self._set_table_item(row, 2, "-")
        self._set_table_item(row, 3, "-")
        self._set_table_item(row, 4, "-")
        self._set_table_item(row, 5, "Aguardando")
        return row

    def _set_table_item(self, row: int, col: int, text: str, color: Optional[QColor] = None):
        item = self.table.item(row, col)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, col, item)
        item.setText(str(text))
        if color is not None:
            item.setForeground(color)

    def on_file_result(self, result: JobResult):
        row = self._ensure_table_row(result.in_path)
        status_text = "OK" if result.ok else "ERRO"
        status_color = QColor("green") if result.ok else QColor("red")
        self._set_table_item(row, 0, status_text, status_color)
        self._set_table_item(row, 1, result.in_path)
        self._set_table_item(row, 2, str(result.points) if result.points is not None else "-")
        ver_text = "-"
        if result.out_version is not None and result.out_point_format is not None:
            ver_text = f"v{result.out_version} / PF{result.out_point_format}"
        self._set_table_item(row, 3, ver_text)
        self._set_table_item(row, 4, result.out_path or "-")
        self._set_table_item(row, 5, result.message)

    # ---------------------- preview ----------------------

    def update_preview_from_selection(self):
        items = self.listw.selectedItems()
        if not items:
            self.preview.clear()
            return
        path = items[0].data(Qt.UserRole)
        self.preview.setPlainText(self.build_preview(path))

    def build_preview(self, path: str) -> str:
        if laspy is None:
            return "laspy não está instalado."
        if not os.path.exists(path):
            return "Arquivo não existe."
        try:
            s = os.path.getsize(path)
            with laspy.open(path, mode="r", read_evlrs=self.chk_read_evlrs.isChecked()) as r:
                h = r.header
                info = []
                info.append(f"Arquivo: {path}")
                info.append(f"Tamanho: {human_bytes(s)}")
                info.append(f"Versão LAS: {h.version}")
                info.append(f"Point format: {h.point_format.id}")
                info.append(f"Qtd. pontos: {h.point_count}")
                try:
                    info.append(f"Scales: {list(h.scales)}")
                    info.append(f"Offsets: {list(h.offsets)}")
                except Exception:
                    pass
                try:
                    mins = h.mins
                    maxs = h.maxs
                    info.append(f"Bounds mín.: ({mins[0]:.3f}, {mins[1]:.3f}, {mins[2]:.3f})")
                    info.append(f"Bounds máx.: ({maxs[0]:.3f}, {maxs[1]:.3f}, {maxs[2]:.3f})")
                except Exception:
                    pass
                try:
                    crs = h.parse_crs()
                    if crs:
                        info.append(f"CRS: {crs.to_string()}")
                    else:
                        info.append("CRS: não identificado")
                except Exception:
                    info.append("CRS: não disponível")
                try:
                    dims = list(h.point_format.dimension_names)
                    info.append("Dimensões: " + ", ".join(map(str, dims)))
                except Exception:
                    pass
                try:
                    info.append(f"VLRs: {len(h.vlrs)}")
                except Exception:
                    pass
                try:
                    evlrs = getattr(h, "evlrs", None)
                    if evlrs is not None:
                        info.append(f"EVLRs: {len(evlrs)}")
                except Exception:
                    pass
            return "\n".join(info)
        except Exception as e:
            return f"Erro ao ler o header:\n{e}\n\n{traceback.format_exc()}"

    # ---------------------- backends ----------------------

    def check_backends(self):
        if laspy is None or LazBackend is None:
            QMessageBox.warning(self, "Backends", "laspy não está disponível.")
            return
        try:
            av = LazBackend.detect_available()
            QMessageBox.information(self, "Backends LAZ", f"Disponíveis agora:\n{av}")
        except Exception as e:
            QMessageBox.warning(self, "Backends", f"Erro checando backends:\n{e}")

    # ---------------------- execução ----------------------

    def _collect_jobs(self) -> List[Job]:
        jobs: List[Job] = []
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            p = it.data(Qt.UserRole)
            root_for_rel = it.data(Qt.UserRole + 1)
            jobs.append(Job(in_path=p, root_for_rel=root_for_rel))
        return jobs

    def start_conversion(self):
        if self.thread is not None:
            QMessageBox.information(self, "Processando", "Já existe uma conversão em andamento.")
            return

        if self.listw.count() == 0:
            QMessageBox.information(self, "Nada para fazer", "Adicione arquivos .LAS/.LAZ primeiro.")
            return

        if laspy is None:
            QMessageBox.critical(self, "Dependência faltando", "Instale: pip install laspy[lazrs]")
            return

        mode = self.cb_out_mode.currentData()
        out_folder = self.ed_out_folder.text().strip()
        if mode == "OUT_FOLDER" and not out_folder:
            QMessageBox.warning(self, "Saída", "Selecione a pasta de saída.")
            return

        if self.chk_move_original.isChecked():
            ok = QMessageBox.question(
                self,
                "Confirmação",
                "Os originais serão movidos para a subpasta _LAS_ORIG após sucesso. Deseja continuar?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ok != QMessageBox.Yes:
                return

        self.save_settings()
        self.results_last = []
        self.rebuild_table_from_list()
        jobs = self._collect_jobs()

        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.pb_file.setValue(0)
        self.pb_overall.setValue(0)

        self.signals = WorkerSignals()
        self.signals.log.connect(self.append_log)
        self.signals.overall.connect(self.on_overall_progress)
        self.signals.file_progress.connect(self.on_file_progress)
        self.signals.current_file.connect(self.on_current_file)
        self.signals.file_result.connect(self.on_file_result)
        self.signals.finished.connect(self.on_finished)
        self.signals.failed.connect(self.on_failed)

        self.thread = QThread(self)
        self.worker = LasToLazWorker(
            jobs=jobs,
            out_mode=mode,
            out_folder=out_folder,
            out_subfolder=self.ed_subfolder.text().strip(),
            keep_structure=self.chk_keep_structure.isChecked(),
            overwrite=self.chk_overwrite.isChecked(),
            verify=self.chk_verify.isChecked(),
            move_original=self.chk_move_original.isChecked(),
            chunk_points=int(self.sp_chunk.value()),
            read_evlrs=self.chk_read_evlrs.isChecked(),
            backend_choice=self.cb_backend.currentData(),
            target_version_choice=self.cb_out_version.currentData(),
            signals=self.signals,
            out_format=self.cb_out_format.currentData() or "LAZ",
        )
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.append_log(f"[{now_stamp()}] Conversão iniciada em segundo plano.")

    def cancel_conversion(self):
        if self.worker:
            self.worker.cancel()
            self.append_log(f"[{now_stamp()}] Cancelamento solicitado...")

    def on_current_file(self, path: str):
        self.lbl_current.setText(f"Arquivo atual: {os.path.basename(path)}")
        self.status_cb(f"Processando: {os.path.basename(path)}")
        row = self._ensure_table_row(path)
        self._set_table_item(row, 0, "Processando")
        self._set_table_item(row, 5, "Em processamento...")

    def on_file_progress(self, done: int, total: int):
        if total <= 0:
            self.pb_file.setValue(0)
            return
        pct = int((done / total) * 100)
        self.pb_file.setValue(max(0, min(100, pct)))

    def on_overall_progress(self, done_files: int, total_files: int):
        if total_files <= 0:
            self.pb_overall.setValue(0)
            return
        pct = int((done_files / total_files) * 100)
        self.pb_overall.setValue(max(0, min(100, pct)))

    def on_failed(self, msg: str):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(1500)
        self.thread = None
        self.worker = None
        self.signals = None
        self.append_log(f"[{now_stamp()}] FALHA: {msg}")
        QMessageBox.critical(self, "Falha", msg)

    def on_finished(self, results: list):
        self.results_last = results or []
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.pb_file.setValue(0)

        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(1500)
        self.thread = None
        self.worker = None
        self.signals = None

        ok = sum(1 for r in self.results_last if r.ok)
        fail = sum(1 for r in self.results_last if not r.ok)
        self.append_log(f"[{now_stamp()}] Finalizado. OK={ok} | ERRO={fail}")
        self.status_cb(f"Conversão concluída. {ok} OK, {fail} erro(s).")
        self.update_cards()
        QMessageBox.information(self, "Concluído", f"Processamento finalizado.\nOK: {ok}\nErros: {fail}")

    # ---------------------- exportação ----------------------

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, "Salvar log", "log_las2laz.txt", "TXT (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.toPlainText())
            QMessageBox.information(self, "Log", f"Salvo em:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Log", f"Erro salvando log:\n{e}")

    def save_report_from_last_results(self):
        if not self.results_last:
            QMessageBox.information(self, "Relatório", "Ainda não existem resultados. Execute uma conversão primeiro.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Salvar relatório CSV",
            f"relatorio_las2laz_{time.strftime('%Y%m%d_%H%M%S')}.csv",
            "CSV (*.csv)"
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow([
                    "ok", "in_path", "out_path", "out_version", "out_point_format",
                    "points", "in_size_bytes", "out_size_bytes", "in_size", "out_size",
                    "delta_bytes", "delta_pct", "seconds", "message"
                ])
                for r in self.results_last:
                    inb = r.in_size if r.in_size is not None else ""
                    outb = r.out_size if r.out_size is not None else ""
                    db = ""
                    dp = ""
                    if isinstance(inb, int) and isinstance(outb, int) and inb > 0:
                        db = outb - inb
                        dp = (outb / inb - 1.0) * 100.0
                    w.writerow([
                        1 if r.ok else 0,
                        r.in_path,
                        r.out_path,
                        r.out_version if r.out_version else "",
                        r.out_point_format if r.out_point_format is not None else "",
                        r.points if r.points is not None else "",
                        inb,
                        outb,
                        human_bytes(r.in_size) if r.in_size is not None else "",
                        human_bytes(r.out_size) if r.out_size is not None else "",
                        db,
                        f"{dp:.3f}" if dp != "" else "",
                        f"{r.seconds:.3f}" if r.seconds is not None else "",
                        r.message,
                    ])
            QMessageBox.information(self, "Relatório", f"CSV salvo em:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Relatório", f"Erro salvando CSV:\n{e}")

# ==========================================================================
#  JANELA PRINCIPAL
# ==========================================================================

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("LAS / LAZ Toolkit — TerraScan / TerraModeler / Spatix")
        self.resize(1150, 850)

        status = self.statusBar().showMessage
        status("Pronto.")

        abas = QTabWidget()
        abas.addTab(MetadataTab(status), "Metadados")
        abas.addTab(PrjEditTab(status), "Editar PRJ")
        abas.addTab(PrjCopyTab(status), "Copiador PRJ")
        abas.addTab(LowercaseLazTab(status), ".LAZ → .laz")
        abas.addTab(ConvertVersionTab(status), "Converter Versão (1.4 ↔ 1.2)")
        abas.addTab(SmartLasToLazTab(status), "Conversor LAS ↔ LAZ")
        abas.addTab(ExportClassesTab(status), "Exportar Classes")

        geo_tabs = (
            (KeyPointsTab, "Pontos-Chave (LAS)"),
            (LatticeTab, "GeoTIFF (Lattice Model)"),
        )
        for cls, titulo in geo_tabs:
            if GEO_AVAILABLE and LASPY_AVAILABLE:
                abas.addTab(cls(status), titulo)
            else:
                faltando = GEO_IMPORT_ERROR or "laspy não instalado"
                abas.addTab(
                    MissingDepsTab(
                        titulo,
                        "Dependências ausentes para esta função:\n"
                        f"    {faltando}\n\n"
                        "Instale tudo com:\n    pip install -r requirements.txt",
                    ),
                    titulo,
                )
        self.setCentralWidget(abas)


def main() -> None:
    multiprocessing.freeze_support()  # necessário no Windows / executáveis PyInstaller

    try:  # DPI awareness no Windows
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    janela = MainWindow()
    janela.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()