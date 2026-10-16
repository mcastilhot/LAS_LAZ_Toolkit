#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LAS / LAZ Toolkit  (TerraScan / TerraModeler / Spatix)  -- v2
==============================================================

Aplicativo único (PySide6) que reúne:

    1. Metadados          - nome da folha e nº de pontos (lê só o cabeçalho)
    2. Renomear           - .las <-> .laz (somente a extensão)
    3. Editar PRJ         - troca .las/.laz dentro de projetos PRJ (com backup)
    4. Copiador PRJ       - copia os arquivos listados em um PRJ
    5. .LAZ -> .laz       - corrige extensão maiúscula
    6. Converter 1.4->1.2 - LAS/LAZ 1.4 (inclui COPC) para 1.2
    7. Pontos-Chave (LAS) - equivalente à macro FnScanClassifyModelKey
    8. GeoTIFF (Lattice)  - equivalente ao "Export Lattice Model"
    9. Curvas de Nível    - equivalente ao "Produce contours"
   10. Visualizador 3D    - nuvem LAZ ou MDT (GeoTIFF) colorido por cota, estilo QTM
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
import ctypes
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
    os.environ["PATH"] = os.pathsep.join(_dlls) + os.pathsep + os.environ.get("PATH", "")
    for _d in _dlls:
        if os.path.isdir(_d):
            try:
                os.add_dll_directory(_d)
            except Exception:
                pass

    # Qt6Core imports the unversioned ICU API (for example, ``ucnv_open``).
    # PyInstaller can otherwise collect an unrelated, version-suffixed ICU
    # from another package in the build environment. Bind Windows' ICU first
    # so the loader resolves ``icuuc.dll`` to the matching system runtime.
    _system_icu = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icuuc.dll")
    if os.path.isfile(_system_icu):
        _icu_handle = ctypes.WinDLL(_system_icu, winmode=0x00001100)

from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtGui import QColor, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
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
    LASPY_AVAILABLE = True
except ImportError:
    laspy = None  # type: ignore
    LASPY_AVAILABLE = False

try:
    from scipy.spatial import Delaunay, cKDTree
    import rasterio
    import rasterio.io
    import rasterio.windows
    from rasterio.transform import from_origin
    from rasterio.merge import merge as rasterio_merge
    from shapely.geometry import box, LineString, Polygon
    from shapely.strtree import STRtree
    from skimage import measure as skimage_measure
    import geopandas as gpd
    GEO_AVAILABLE = True
    GEO_IMPORT_ERROR = ""
except ImportError as _exc:  # pragma: no cover
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


# ── 2. Renomear ───────────────────────────────────────────────────────────
def work_rename(files, target_ext, emit):
    ok = erros = 0
    for i, src in enumerate(files):
        dst = src.with_suffix(target_ext)
        try:
            if dst.exists():
                raise FileExistsError(f"destino já existe: {dst.name}")
            src.rename(dst)
            emit("file", str(src), str(dst), True)
            ok += 1
        except Exception as e:
            emit("file", str(src), str(e), False)
            erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


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
# Formatos LAS 1.4 válidos: 0..10. Formatos 4/5 e 9/10 contêm waveform,
# que não existe nos formatos permitidos pelo LAS 1.2. NIR também não cabe.
# Mapeamento explícito evita degradar silenciosamente os formatos 0..5 para 3.
POINT_FORMAT_DOWNGRADE = {
    0: 0, 1: 1, 2: 2, 3: 3,
    4: 1, 5: 3,
    6: 1, 7: 3, 8: 3, 9: 1, 10: 3,
}


def _is_copc(las):
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
    def _sem_copc(vlrs):
        if not vlrs:
            return vlrs
        restantes = [v for v in vlrs if getattr(v, "user_id", "").strip().lower() != "copc"]
        return type(vlrs)(restantes)
    las.header.vlrs = _sem_copc(las.header.vlrs)
    las.header.evlrs = _sem_copc(las.header.evlrs)


def _set_raw_classification_byte(las, values):
    """Grava o byte INTEIRO de classificação do LAS 1.2 (como Global Mapper/TerraScan),
    permitindo classes de 0 a 255 em vez do limite de 31 (5 bits) do padrão."""
    values = np.asarray(values, dtype=np.uint8)
    arr = las.points.array
    if "raw_classification" in (arr.dtype.names or ()):
        arr["raw_classification"] = values
        return True
    try:
        las.points["raw_classification"] = values
        return True
    except Exception:
        return False


def work_downgrade(files, dest_folder, add_suffix, emit):
    ok = erros = 0
    for i, src in enumerate(files):
        try:
            las = laspy.read(str(src), laz_backend=_laz_backends() or None) \
                if src.suffix.lower() == ".laz" else laspy.read(str(src))
            versao = las.header.version
            if not (versao.major == 1 and versao.minor == 4):
                emit("file", src.name, False,
                     f"ignorado — versão atual é {versao.major}.{versao.minor}, não 1.4")
                erros += 1
                emit("progress", i + 1)
                continue

            era_copc = _is_copc(las)
            if era_copc:
                _strip_copc(las)

            origem_pf = las.header.point_format.id
            if origem_pf not in POINT_FORMAT_DOWNGRADE:
                raise ValueError(
                    f"Point format {origem_pf} não é suportado para conversão LAS 1.2. "
                    "Nenhum arquivo de saída foi gravado."
                )
            destino_pf = POINT_FORMAT_DOWNGRADE[origem_pf]

            try:
                crs = las.header.parse_crs()
            except Exception:
                crs = None
            evlrs_origem = las.header.evlrs or []
            evlrs_projection = [
                vlr for vlr in evlrs_origem
                if str(getattr(vlr, "user_id", "")).strip().lower() == "lasf_projection"
            ]
            evlrs_adicionais = [
                vlr for vlr in evlrs_origem
                if str(getattr(vlr, "user_id", "")).strip().lower()
                not in ("copc", "lasf_projection")
            ]
            if crs is None and evlrs_projection:
                raise RuntimeError(
                    "Há metadados de CRS em EVLR que não puderam ser interpretados. "
                    "A conversão foi cancelada para não remover a referência espacial."
                )

            # Classificação: guarda original (8 bits); limita temporariamente a 31
            # só para o laspy.convert não falhar.
            classif_orig = np.asarray(las.classification).astype(np.uint8).copy()
            mask_alta = classif_orig > 31
            n_class_alta = int(np.count_nonzero(mask_alta))
            if n_class_alta:
                las.classification = np.minimum(classif_orig, 31).astype(np.uint8)

            # Return number / number of returns (4 bits → 3 bits)
            n_ret_overflow = 0
            for campo in ("return_number", "number_of_returns"):
                arr = np.asarray(las[campo])
                m = arr > 7
                if m.any():
                    n_ret_overflow += int(np.count_nonzero(m))
                    las[campo] = np.minimum(arr, 7).astype(np.uint8)

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

            # LAS 1.2 reserva só 5 bits para classificação. Para manter valores
            # 32..255 como Global Mapper/TerraScan, grava o byte bruto completo.
            # Como esse uso é uma extensão não padrão, falha em vez de criar uma
            # saída que anuncie sucesso com as classes silenciosamente limitadas.
            classe_mantida = True
            if n_class_alta:
                names = convertido.points.array.dtype.names or ()
                if "raw_classification" in names:
                    raw_atual = np.asarray(convertido.points.array["raw_classification"]).copy()
                    raw_atual[mask_alta] = classif_orig[mask_alta]
                    classe_mantida = _set_raw_classification_byte(convertido, raw_atual)
                else:
                    classe_mantida = False
                if not classe_mantida:
                    raise RuntimeError(
                        "Não foi possível preservar classes acima de 31 no byte bruto "
                        "de classificação; a conversão foi cancelada para evitar perda."
                    )

            if crs is not None:
                try:
                    convertido.header.add_crs(crs)
                except Exception as exc:
                    raise RuntimeError(f"Não foi possível preservar o CRS: {exc}") from exc

            nome_saida = src.stem + ("_v12" if add_suffix else "") + ".laz"
            dest_path = Path(dest_folder) / nome_saida
            if dest_path.resolve() == src.resolve():
                raise FileExistsError(
                    "o destino coincide com o arquivo de origem; ative o sufixo _v12 "
                    "ou escolha outra pasta para manter o original intacto"
                )
            if dest_path.exists():
                raise FileExistsError(f"o arquivo de destino já existe: {dest_path}")
            try:
                convertido.write(str(dest_path))
            except NotImplementedError as e:
                emit("file", src.name, False, f"ignorado — não foi possível gravar: {e}")
                erros += 1
                emit("progress", i + 1)
                continue

            perdas = []
            if origem_pf in (8, 10):
                perdas.append("NIR")
            if origem_pf in (4, 5, 9, 10):
                perdas.append("waveform")
            if era_copc:
                perdas.append("indexação COPC/octree")
            if evlrs_adicionais:
                perdas.append(f"{len(evlrs_adicionais)} EVLR(s) adicional(is)")
            perda_txt = f"  (perdeu: {', '.join(perdas)})" if perdas else ""

            avisos = []
            if n_class_alta:
                avisos.append(
                    f"{n_class_alta} pts com classe >31 mantidos "
                    f"(byte completo, máx. {int(classif_orig.max())})"
                )
            if n_ret_overflow:
                avisos.append(
                    f"{n_ret_overflow} valores de retorno (>7) limitados a 7 "
                    "(limite do LAS 1.2)"
                )
            aviso_txt = ("  |  " + "  |  ".join(avisos)) if avisos else ""

            emit("file", src.name, True,
                 f"1.4 → 1.2  |  formato {origem_pf} → {destino_pf}{perda_txt}{aviso_txt}")
            ok += 1
        except Exception as e:
            emit("file", src.name, False, str(e))
            erros += 1
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

            # As EVLRs não são copiadas para o novo cabeçalho. Recria a definição
            # de CRS a partir do CRS interpretado, incluindo WKT guardado em EVLR.
            try:
                crs_saida = las.header.parse_crs()
            except Exception:
                crs_saida = None
            if crs_saida is not None:
                try:
                    las_saida.header.add_crs(crs_saida)
                except Exception as exc:
                    raise RuntimeError(f"Não foi possível preservar o CRS: {exc}") from exc
            else:
                # Se o CRS não puder ser interpretado, mantém os registros brutos
                # de projeção que estiverem em EVLR. VLRs de projeção já foram
                # copiados pelo laço acima.
                evlrs_origem = getattr(las.header, "evlrs", None)
                evlrs_crs = [
                    vlr for vlr in (evlrs_origem or [])
                    if str(getattr(vlr, "user_id", "")).strip().lower() == "lasf_projection"
                ]
                if evlrs_crs:
                    las_saida.evlrs = type(evlrs_origem)(evlrs_crs)

            # Preserva o registro completo dos pontos selecionados.
            las_saida.points = las.points[indices_finais].copy()

            # Reclassifica os pontos selecionados como Model Key-point.
            if params.model_key_class <= 31:
                las_saida.classification = np.full(
                    len(indices_finais),
                    params.model_key_class,
                    dtype=las_saida.classification.dtype,
                )
            else:
                raw_classification = np.full(
                    len(indices_finais), params.model_key_class, dtype=np.uint8
                )
                if not _set_raw_classification_byte(las_saida, raw_classification):
                    raise RuntimeError(
                        "O formato LAS de saída não permite gravar a classe acima de 31 "
                        "no byte bruto de classificação."
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
#  ABA 2 — RENOMEAR ARQUIVOS
# ==========================================================================

class RenameTab(ToolTab):
    def __init__(self, status) -> None:
        super().__init__(status)
        self.ok = self.err = 0
        lay = QVBoxLayout(self)

        tb = QHBoxLayout()
        tb.addWidget(mklabel("CONVERTER PARA:", "sec"))
        self.rb_laz = QRadioButton(".LAS → .LAZ")
        self.rb_las = QRadioButton(".LAZ → .LAS")
        self.rb_laz.setChecked(True)
        self.rb_laz.toggled.connect(self._refresh)
        tb.addWidget(self.rb_laz)
        tb.addWidget(self.rb_las)
        tb.addSpacing(16)
        self.btn_add = mkbtn("+ Adicionar", self._add_files)
        self.btn_dir = mkbtn("Pasta", self._add_folder)
        self.btn_clr = mkbtn("Limpar", self._clear, "danger")
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        tb.addStretch(1)
        lay.addLayout(tb)

        lay.addWidget(mklabel("Arraste e solte arquivos .LAS ou .LAZ aqui", "hint"))
        self.flist = FileList((".las", ".laz"), fmt=self._fmt)
        self.flist.dropped.connect(self._after_add)
        lay.addWidget(self.flist, 1)

        self.stats = Stats([("total", "Total na fila", False), ("las", "Arquivos .LAS", False),
                            ("laz", "Arquivos .LAZ", False), ("ok", "Renomeados", True),
                            ("err", "Erros", False)])
        lay.addWidget(self.stats)
        lay.addWidget(self.pb)

        bottom = QHBoxLayout()
        self.btn_rem = mkbtn("Remover selecionados", self._remove_sel)
        self.btn_run = mkbtn("▶  Renomear arquivos", self._run, "primary")
        bottom.addWidget(self.btn_rem)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run)
        lay.addLayout(bottom)

    @property
    def mode(self) -> str:
        return ".laz" if self.rb_laz.isChecked() else ".las"

    def _fmt(self, p: Path) -> str:
        if p.suffix.lower() == self.mode:
            return f"{p.name}  —  já é {self.mode[1:].upper()}"
        return f"{p.name}  →  {p.with_suffix(self.mode).name}"

    def _refresh(self) -> None:
        self.flist.refresh()
        las = sum(1 for p in self.flist.paths if p.suffix.lower() == ".las")
        self.stats.set("total", len(self.flist.paths))
        self.stats.set("las", las)
        self.stats.set("laz", len(self.flist.paths) - las)

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
            self._refresh()
            self.status(f"{n} arquivo(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self) -> None:
        n = self.flist.remove_selected()
        self._refresh()
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self) -> None:
        self.flist.reset_list()
        self.stats.reset()
        self.status("Lista limpa.")

    def _run(self) -> None:
        mode = self.mode
        targets = [p for p in self.flist.paths if p.suffix.lower() != mode]
        if not targets:
            info(self, "Nada a fazer", f"Todos os arquivos já têm extensão {mode[1:].upper()}.")
            return
        if not ask(self, "Confirmar",
                   f"Renomear {len(targets)} arquivo(s) para {mode[1:].upper()}?\n"
                   "Esta ação modifica os arquivos no disco."):
            return
        self.ok = self.err = 0
        self.stats.reset("ok", "err")
        self._start_pb(len(targets))
        self._busy(True)
        self.runner.start(work_rename, targets, mode)

    def _on_event(self, kind: str, *a) -> None:
        if kind == "progress":
            self.pb.setValue(a[0])
        elif kind == "file":
            original, resultado, sucesso = a
            if sucesso:
                self.ok += 1
                self.stats.set("ok", self.ok)
                orig = Path(original)
                for i, p in enumerate(self.flist.paths):
                    if p == orig:
                        self.flist.paths[i] = Path(resultado)
                        break
            else:
                self.err += 1
                self.stats.set("err", self.err)
        elif kind == "done":
            ok, erros = a
            self._busy(False)
            self.pb.setVisible(False)
            self._refresh()
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) renomeado(s).")
                info(self, "Concluído", f"{ok} arquivo(s) renomeado(s).")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                warn(self, "Concluído com erros", f"{ok} renomeado(s).\n{erros} com erro.")

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_rem], not busy)
        self.btn_run.setText("Processando…" if busy else "▶  Renomear arquivos")


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
        if not self.dest.text():
            warn(self, "Atenção", "Selecione a pasta de destino.")
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

        tb = QHBoxLayout()
        tb.addWidget(mklabel("ENTRADA (LAZ/LAS 1.4):", "sec"))
        self.btn_add = mkbtn("+ Adicionar", self._add_files)
        self.btn_dir = mkbtn("Pasta", self._add_folder)
        self.btn_clr = mkbtn("Limpar", self._clear, "danger")
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        tb.addStretch(1)
        lay.addLayout(tb)

        lay.addWidget(mklabel("Arraste e solte arquivos .LAZ ou .LAS (versão 1.4) aqui", "hint"))
        self.flist = FileList((".las", ".laz"))
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
        r.addWidget(self.dest, 1)
        r.addWidget(self.btn_dest)
        r.addWidget(self.chk)
        ol.addLayout(r)
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
            info(self, "Nada a fazer", "Adicione arquivos LAZ/LAS 1.4.")
            return
        if not self.dest.text():
            warn(self, "Atenção", "Selecione a pasta de destino.")
            return
        if not ask(self, "Confirmar conversão",
                   f"Converter {len(self.flist.paths)} arquivo(s) de LAZ/LAS 1.4 para 1.2?\n\n"
                   "Os formatos de ponto serão mapeados explicitamente: 0→0, 1→1, 2→2, 3→3, "
                   "4→1, 5→3, 6→1, 7→3, 8→3, 9→1, 10→3. "
                   "NIR (formatos 8 e 10) e waveform (4, 5, 9 e 10) não existem no LAS 1.2 "
                   "e serão perdidos. Ângulo de varredura será arredondado para graus inteiros; "
                   "valores de retorno acima de 7 serão limitados a 7; canal do scanner e flag "
                   "overlap do LAS 1.4 não são representáveis no LAS 1.2. "
                   "Arquivos COPC são aceitos: a indexação em octree é removida. EVLRs "
                   "adicionais incompatíveis também serão descartados; o CRS será preservado "
                   "ou a conversão será interrompida se não puder ser mantido. Nenhum ponto "
                   "é removido, embora os atributos listados acima possam mudar.\n\n"
                   "Classificação: valores de 0 a 255, inclusive acima de 31, são mantidos no byte "
                   "bruto como no Global Mapper/TerraScan. Isso é uma convenção não padrão: leitores "
                   "estritamente conformes podem interpretar classes acima de 31 como flags do LAS 1.2; "
                   "nesses pontos, o byte completo da classe tem prioridade sobre essas flags.\n\n"
                   "Arquivos de origem e destinos existentes não serão sobrescritos."):
            return
        self.ok = self.err = 0
        self.stats.reset("ok", "err")
        self.log.clear()
        self._start_pb(len(self.flist.paths))
        self._busy(True)
        self.runner.start(work_downgrade, list(self.flist.paths), self.dest.text(), self.chk.isChecked())

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
                self.status(f"Concluído — {ok} arquivo(s) convertido(s) para 1.2.")
                info(self, "Concluído", f"{ok} arquivo(s) convertido(s) para LAZ 1.2.")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                warn(self, "Concluído com erros", f"{ok} convertido(s).\n{erros} com erro (veja o log).")

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_dest,
                     self.btn_rem, self.chk], not busy)
        self.btn_run.setText("Convertendo…" if busy else "▶  Converter para 1.2")


# ==========================================================================
#  BASE DAS ABAS 7-9 (processamento em lote por pasta)
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

class ContoursTab(PoolTab):
    HEADERS = ["Folha", "Status", "Pts TIN", "Vizinhos", "Linhas", "Tempo (s)"]
    SAIDA_LABEL = "Directory (pasta de saída):"
    SPLIT_SIZES = (460, 360)

    def build_params(self) -> QWidget:
        self.edit_prefixo = QLineEdit("contours")
        self.form_pastas.addRow("Name prefix:", self.edit_prefixo)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)

        gm = QGroupBox("Model settings")
        fm = QFormLayout(gm)
        self.spin_buffer = spin_d(0.0, 100000.0, 200.0)
        fm.addRow("Model buffer (m ao redor da folha):", self.spin_buffer)
        self.spin_res = spin_d(0.05, 50.0, 1.0)
        fm.addRow("Resolução do grid interno (m):", self.spin_res)
        self.edit_classes = QLineEdit("8")
        fm.addRow("Laser points -- Classes:", self.edit_classes)
        self.spin_epsg = spin_i(1000, 99999, 31982)
        fm.addRow("EPSG da saída vetorial:", self.spin_epsg)
        self.combo_fmt = QComboBox()
        self.combo_fmt.addItems(["GPKG", "SHP"])
        fm.addRow("Formato das curvas:", self.combo_fmt)
        self.check_continua = QCheckBox("Gerar arquivo único contínuo (sem emendas por folha)")
        fm.addRow(self.check_continua)
        self.check_bordas = QCheckBox("Encaixar pontas das curvas nas bordas das folhas")
        self.check_bordas.setChecked(True)
        fm.addRow(self.check_bordas)
        wl.addWidget(gm)

        gc = QGroupBox("Contours")
        gl = QVBoxLayout(gc)
        linha_set = QHBoxLayout()
        linha_set.addWidget(mkbtn("Carregar configurações (.set)...", self.on_carregar_set))
        linha_set.addStretch(1)
        gl.addLayout(linha_set)

        cols = QHBoxLayout()
        esq, dirr = QFormLayout(), QFormLayout()
        cols.addLayout(esq, 1)
        cols.addLayout(dirr, 1)
        gl.addLayout(cols)

        self.check_basic = QCheckBox("Draw basic contours")
        self.check_basic.setChecked(True)
        self.spin_basic = spin_d(0.01, 1000.0, 1.0, 3)
        esq.addRow(self.check_basic, self.spin_basic)
        self.check_major = QCheckBox("Draw major contours")
        self.check_major.setChecked(True)
        self.spin_major = spin_d(0.01, 1000.0, 5.0, 3)
        esq.addRow(self.check_major, self.spin_major)
        self.check_minor = QCheckBox("Draw minor contours")
        self.spin_minor = spin_d(0.01, 1000.0, 0.5, 3)
        esq.addRow(self.check_minor, self.spin_minor)
        self.check_smooth = QCheckBox("Smoothing (suavização Chaikin)")
        self.check_smooth.setChecked(True)
        esq.addRow(self.check_smooth)
        self.check_thin = QCheckBox("Thin (simplificar vértices)")
        self.spin_thin = spin_d(0.001, 100.0, 0.5, 3)
        esq.addRow(self.check_thin, self.spin_thin)
        self.spin_proc = spin_i(1, 32, 4)
        esq.addRow("Processos paralelos:", self.spin_proc)

        self.check_min_area = QCheckBox("Usar área mínima (descarta anéis pequenos)")
        dirr.addRow(self.check_min_area)
        self.spin_hill = spin_d(0.0, 1000000.0, 200.0)
        dirr.addRow("Hill area (m²):", self.spin_hill)
        self.spin_dep = spin_d(0.0, 1000000.0, 200.0)
        dirr.addRow("Dep area (m²):", self.spin_dep)
        self.spin_dec = spin_i(0, 6, 2)
        dirr.addRow("Casas decimais da cota:", self.spin_dec)

        aviso = mklabel('Obs.: "Survey elements" e "Vector elements" do TerraModeler não são '
                        "suportados nesta versão -- só pontos LiDAR (Laser points).", "hint")
        aviso.setWordWrap(True)
        gl.addWidget(aviso)
        wl.addWidget(gc)
        return wrap

    def on_carregar_set(self) -> None:
        caminho, _ = QFileDialog.getOpenFileName(
            self, "Carregar configurações de curvas (.set)", "", "TerraModeler settings (*.set)")
        if not caminho:
            return
        try:
            config = carregar_configuracoes_set(caminho)
        except Exception as exc:  # noqa: BLE001
            self.log.add(f"Erro ao ler .set: {exc}", "err")
            return

        def _float(chave, padrao):
            try:
                return float(config.get(chave, padrao))
            except ValueError:
                return padrao

        def _bool(chave, padrao):
            try:
                return int(config.get(chave, "1" if padrao else "0")) != 0
            except ValueError:
                return padrao

        self.spin_basic.setValue(_float("BasicInterval", self.spin_basic.value()))
        self.spin_major.setValue(_float("MajorInterval", self.spin_major.value()))
        self.spin_minor.setValue(_float("MinorInterval", self.spin_minor.value()))
        self.check_basic.setChecked(_bool("DrawBasic", self.check_basic.isChecked()))
        self.check_major.setChecked(_bool("DrawMajor", self.check_major.isChecked()))
        self.check_minor.setChecked(_bool("DrawMinor", self.check_minor.isChecked()))
        self.check_smooth.setChecked(_bool("Smoothing", self.check_smooth.isChecked()))
        self.check_thin.setChecked(_bool("Thin", self.check_thin.isChecked()))
        self.spin_thin.setValue(_float("ThinDeviation", self.spin_thin.value()))
        self.check_min_area.setChecked(_bool("UseMinArea", self.check_min_area.isChecked()))
        self.spin_hill.setValue(_float("HillArea", self.spin_hill.value()))
        self.spin_dep.setValue(_float("DepArea", self.spin_dep.value()))
        try:
            self.spin_dec.setValue(int(config.get("LabelDecimals", self.spin_dec.value())))
        except ValueError:
            pass
        self.log.add(f"Configurações carregadas de {Path(caminho).name}.", "ok")

    def prepare(self, arquivos, pasta_saida):
        classes = parse_classes(self.edit_classes.text())
        if not classes:
            self.log.add("Erro: informe ao menos uma classe válida (ex.: 8).", "err")
            return None
        if not (self.check_basic.isChecked() or self.check_major.isChecked()
                or self.check_minor.isChecked()):
            self.log.add("Erro: marque ao menos um tipo de curva (basic/major/minor).", "err")
            return None

        continua = self.check_continua.isChecked()
        kw = dict(
            output_dir=pasta_saida,
            prefixo=self.edit_prefixo.text().strip() or "contours",
            classes=classes,
            resolucao_grid=self.spin_res.value(),
            draw_basic=self.check_basic.isChecked(),
            draw_major=self.check_major.isChecked(),
            draw_minor=self.check_minor.isChecked(),
            basic_interval=self.spin_basic.value(),
            major_interval=self.spin_major.value(),
            minor_interval=self.spin_minor.value(),
            smoothing=self.check_smooth.isChecked(),
            thin=self.check_thin.isChecked(),
            thin_deviation=self.spin_thin.value(),
            use_min_area=self.check_min_area.isChecked(),
            hill_area=self.spin_hill.value(),
            dep_area=self.spin_dep.value(),
            label_decimals=self.spin_dec.value(),
            formato_saida=self.combo_fmt.currentText(),
            ajustar_bordas=self.check_bordas.isChecked(),
            epsg=self.spin_epsg.value(),
        )
        args = (arquivos, self.spin_buffer.value(), kw, continua,
                self.spin_proc.value(), self.cancel_event)
        return work_contours, args, (1 if continua else len(arquivos))

    def row_values(self, r: ContourResult) -> list:
        return [r.n_pontos_tin, r.n_vizinhos_usados, r.n_linhas, f"{r.tempo_s:.1f}"]

    def ok_path(self, r: ContourResult) -> str:
        return r.curvas_path


# ==========================================================================
#  VISUALIZADOR 3D (estilo Quick Terrain Modeler)
# ==========================================================================
# Roda em PROCESSO SEPARADO (PyVista/VTK): a janela 3D não trava a interface
# do toolkit e um problema de OpenGL não derruba o programa.

PYVISTA_AVAILABLE = importlib.util.find_spec("pyvista") is not None


@dataclass
class ViewerParams:
    fonte: str                              # "laz" (nuvem) ou "tif" (MDT)
    paths: list[str]
    classes: list[int]                      # vazio = todas as classes
    exagero_z: float = 1.0
    tamanho_ponto: float = 2.0
    max_pontos: int = 10_000_000            # orçamento de pontos (nuvem)
    cmap: str = "jet"                       # azul (baixo) -> vermelho (alto)
    fundo: str = "black"
    eye_dome: bool = True                   # realça relevo/profundidade na nuvem
    png_path: str = ""                      # se preenchido, salva PNG sem abrir janela
    png_w: int = 1920
    png_h: int = 1080


def _carregar_nuvem_view(paths, classes, max_pontos):
    """Lê as nuvens em chunks, filtra classes e decima aleatoriamente até ~max_pontos."""
    rng = np.random.default_rng(0)
    alvo = np.asarray(sorted(set(classes))) if classes else None
    total = 0
    for p in paths:
        with _open_reader(p) as f:
            total += int(f.header.point_count)
    frac = min(1.0, max_pontos / max(total, 1))

    partes = []
    for p in paths:
        with _open_reader(p) as f:
            for pts in f.chunk_iterator(1_000_000):
                x = np.asarray(pts.x)
                m = np.ones(x.size, dtype=bool) if alvo is None \
                    else np.isin(np.asarray(pts.classification), alvo)
                if frac < 1.0:
                    m &= rng.random(x.size) < frac
                if m.any():
                    partes.append(np.column_stack(
                        [x[m], np.asarray(pts.y)[m], np.asarray(pts.z)[m]]))
    if not partes:
        raise RuntimeError("Nenhum ponto encontrado (verifique as classes selecionadas).")
    return np.concatenate(partes, axis=0)


def _carregar_mdt_view(paths, max_celulas=4_000_000):
    """Lê/mosaica GeoTIFF(s) e devolve (x, y, z, mascara_valida), decimado se muito grande."""
    srcs = [rasterio.open(p) for p in paths]
    try:
        nodata = srcs[0].nodata
        arr, transform = rasterio_merge(srcs)
    finally:
        for s in srcs:
            s.close()
    z = arr[0].astype(np.float64)
    mask = np.isfinite(z)
    if nodata is not None:
        mask &= ~np.isclose(z, nodata)
    mask &= z > -9000
    if not mask.any():
        raise RuntimeError("O GeoTIFF não tem células válidas.")
    h, w = z.shape
    passo = max(int(np.ceil(np.sqrt(h * w / max_celulas))), 1)
    z, mask = z[::passo, ::passo], mask[::passo, ::passo]
    x = transform.c + (np.arange(w)[::passo] + 0.5) * transform.a
    y = transform.f + (np.arange(h)[::passo] + 0.5) * transform.e
    return x, y, z, mask


def _viewer_process(params, fila):
    """Processo filho: carrega os dados, monta a cena PyVista e abre/salva."""
    try:
        import pyvista as pv

        salvar = bool(params.png_path)
        pl = pv.Plotter(off_screen=salvar, window_size=[params.png_w, params.png_h])
        pl.set_background(params.fundo)
        cor_texto = "white" if params.fundo in ("black", "darkgray", "gray") else "black"
        barra = dict(title="Cota (m)", color=cor_texto, vertical=True)

        if params.fonte == "laz":
            xyz = _carregar_nuvem_view(params.paths, params.classes, params.max_pontos)
            origem = xyz.min(axis=0)
            pts = (xyz - origem).astype(np.float32)   # coordenadas locais: evita perda de precisão
            cota = xyz[:, 2].astype(np.float32)
            pts[:, 2] *= params.exagero_z
            nuvem = pv.PolyData(pts)
            nuvem["Cota (m)"] = cota
            pl.add_mesh(nuvem, scalars="Cota (m)", cmap=params.cmap,
                        point_size=params.tamanho_ponto, scalar_bar_args=barra)
            if params.eye_dome:
                try:
                    pl.enable_eye_dome_lighting()
                except Exception:
                    pass
            resumo = f"{len(pts):,} pontos".replace(",", ".")
        else:
            x, y, z, mask = _carregar_mdt_view(params.paths)
            zmin = float(z[mask].min())
            zf = np.where(mask, z, zmin)
            X, Y = np.meshgrid(x - x.min(), y - y.min())
            grade = pv.StructuredGrid(X, Y, (zf - zmin) * params.exagero_z)
            grade["Cota (m)"] = zf.ravel(order="F")
            if not mask.all():
                grade["valido"] = mask.ravel(order="F").astype(np.uint8)
                grade = grade.threshold(0.5, scalars="valido", all_scalars=True)
            sup = grade.extract_surface().compute_normals(
                point_normals=True, cell_normals=False, inplace=False)
            pl.add_mesh(sup, scalars="Cota (m)", cmap=params.cmap,
                        smooth_shading=True, scalar_bar_args=barra)
            resumo = f"superfície {z.shape[1]} x {z.shape[0]} células"

        pl.add_axes()
        pl.view_isometric()
        fila.put(("log", f"Dados carregados: {resumo}."))
        if salvar:
            pl.show(screenshot=params.png_path)
            fila.put(("ok", f"PNG salvo em: {params.png_path}"))
        else:
            fila.put(("log", "Janela 3D aberta (mouse: girar / zoom / mover; feche a janela para terminar)."))
            pl.show()
            fila.put(("ok", "Janela 3D fechada."))
    except Exception:  # noqa: BLE001
        fila.put(("err", traceback.format_exc()))


def work_viewer(params, emit):
    ctx = multiprocessing.get_context("spawn")
    fila = ctx.Queue()
    proc = ctx.Process(target=_viewer_process, args=(params, fila), daemon=True)
    proc.start()
    emit("log", "Carregando dados no processo do visualizador...", "")
    ok = True

    def _drenar():
        nonlocal ok
        while True:
            try:
                tipo, msg = fila.get(timeout=0.3)
            except Exception:
                return
            if tipo == "err":
                ok = False
                emit("log", msg, "err")
            else:
                emit("log", msg, "ok" if tipo == "ok" else "")

    while proc.is_alive():
        _drenar()
    _drenar()
    proc.join()
    emit("done", ok)


class ViewerTab(ToolTab):
    CORES = ["jet", "turbo", "viridis", "terrain", "gist_earth", "rainbow", "gray"]

    def __init__(self, status) -> None:
        super().__init__(status)
        lay = QVBoxLayout(self)

        tb = QHBoxLayout()
        tb.addWidget(mklabel("FONTE (nuvem .las/.laz  ou  MDT .tif):", "sec"))
        self.btn_add = mkbtn("+ Adicionar", self._add_files)
        self.btn_dir = mkbtn("Pasta", self._add_folder)
        self.btn_clr = mkbtn("Limpar", self._clear, "danger")
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            tb.addWidget(b)
        tb.addStretch(1)
        lay.addLayout(tb)

        lay.addWidget(mklabel(
            "Arraste arquivos .las/.laz (pontos) OU .tif (superfície) — não misture os dois tipos", "hint"))
        self.flist = FileList((".las", ".laz", ".tif", ".tiff"))
        lay.addWidget(self.flist, 1)

        g = QGroupBox("Parâmetros")
        f = QFormLayout(g)
        self.edit_classes = QLineEdit("")
        self.edit_classes.setPlaceholderText("vazio = todas (ex.: 2,6 para solo + edificações)")
        f.addRow("Classes (só para nuvem):", self.edit_classes)
        self.spin_ex = spin_d(0.1, 20.0, 1.0, 2, 0.5)
        f.addRow("Exagero vertical (×):", self.spin_ex)
        self.spin_pt = spin_d(1.0, 10.0, 2.0, 1, 0.5)
        f.addRow("Tamanho do ponto (nuvem):", self.spin_pt)
        self.spin_max = spin_i(1, 200, 10)
        f.addRow("Máx. de pontos (milhões, nuvem):", self.spin_max)
        self.combo_cmap = QComboBox()
        self.combo_cmap.addItems(self.CORES)
        f.addRow("Rampa de cores (jet = azul→vermelho, como o QTM):", self.combo_cmap)
        self.combo_fundo = QComboBox()
        self.combo_fundo.addItems(["black", "white", "darkgray"])
        f.addRow("Fundo:", self.combo_fundo)
        self.chk_edl = QCheckBox("Realce de profundidade (Eye-Dome Lighting, nuvem)")
        self.chk_edl.setChecked(True)
        f.addRow(self.chk_edl)
        self.spin_w = spin_i(320, 8000, 1920)
        self.spin_h = spin_i(240, 8000, 1080)
        wh = QHBoxLayout()
        wh.addWidget(self.spin_w)
        wh.addWidget(mklabel("×"))
        wh.addWidget(self.spin_h)
        f.addRow("Tamanho do PNG (px):", wh)
        lay.addWidget(g)

        lay.addWidget(mklabel("Log", "hint"))
        self.log = LogView(90)
        lay.addWidget(self.log)

        bottom = QHBoxLayout()
        self.btn_png = mkbtn("Salvar PNG...", self._png)
        self.btn_open = mkbtn("▶  Abrir janela 3D", self._abrir, "primary")
        bottom.addStretch(1)
        bottom.addWidget(self.btn_png)
        bottom.addWidget(self.btn_open)
        lay.addLayout(bottom)

    def _add_files(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar arquivos", "", "Nuvem / MDT (*.las *.laz *.tif *.tiff)")
        self._add([Path(p) for p in ps])

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Selecionar pasta")
        if d:
            self._add([Path(d)])

    def _add(self, paths) -> None:
        n = self.flist.add_paths(paths)
        self.status(f"{n} arquivo(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _clear(self) -> None:
        self.flist.reset_list()
        self.status("Lista limpa.")

    def _montar(self, png_path: str = ""):
        paths = list(self.flist.paths)
        if not paths:
            warn(self, "Atenção", "Adicione arquivos .las/.laz ou .tif.")
            return None
        tipos = {"laz" if p.suffix.lower() in (".las", ".laz") else "tif" for p in paths}
        if len(tipos) > 1:
            warn(self, "Atenção", "Não misture nuvem (.las/.laz) e MDT (.tif) na mesma lista.")
            return None
        fonte = tipos.pop()
        if fonte == "tif" and not GEO_AVAILABLE:
            warn(self, "Dependência ausente", f"Leitura de GeoTIFF requer rasterio:\n{GEO_IMPORT_ERROR}")
            return None
        if fonte == "laz" and not LASPY_AVAILABLE:
            warn(self, "Dependência ausente", "Instale o laspy: pip install laspy[lazrs]")
            return None
        txt = self.edit_classes.text().strip()
        classes = parse_classes(txt) if txt else []
        if classes is None:
            warn(self, "Atenção", "Classes inválidas. Use números separados por vírgula (ex.: 2,6).")
            return None
        return ViewerParams(
            fonte=fonte,
            paths=[str(p) for p in paths],
            classes=classes,
            exagero_z=self.spin_ex.value(),
            tamanho_ponto=self.spin_pt.value(),
            max_pontos=self.spin_max.value() * 1_000_000,
            cmap=self.combo_cmap.currentText(),
            fundo=self.combo_fundo.currentText(),
            eye_dome=self.chk_edl.isChecked(),
            png_path=png_path,
            png_w=self.spin_w.value(),
            png_h=self.spin_h.value(),
        )

    def _iniciar(self, params) -> None:
        self._busy(True)
        self.log.add("")
        self.runner.start(work_viewer, params)

    def _abrir(self) -> None:
        params = self._montar()
        if params:
            self._iniciar(params)

    def _png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Salvar imagem", "", "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        params = self._montar(png_path=path)
        if params:
            self._iniciar(params)

    def _on_event(self, kind: str, *a) -> None:
        if kind == "log":
            self.log.add(a[0], a[1] if len(a) > 1 else "")
        elif kind == "done":
            self.status("Visualizador finalizado." if a[0] else "Visualizador terminou com erro (veja o log).")

    def _busy(self, busy: bool) -> None:
        set_enabled([self.btn_open, self.btn_png, self.btn_add, self.btn_dir, self.btn_clr], not busy)
        self.btn_open.setText("Visualizando…" if busy else "▶  Abrir janela 3D")



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
        abas.addTab(RenameTab(status), "Renomear Arquivos")
        abas.addTab(PrjEditTab(status), "Editar PRJ")
        abas.addTab(PrjCopyTab(status), "Copiador PRJ")
        abas.addTab(LowercaseLazTab(status), ".LAZ → .laz")
        abas.addTab(ConvertVersionTab(status), "Converter LAZ 1.4→1.2")

        geo_tabs = (
            (KeyPointsTab, "Pontos-Chave (LAS)"),
            (LatticeTab, "GeoTIFF (Lattice Model)"),
            (ContoursTab, "Curvas de Nível (Contours)"),
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
        if PYVISTA_AVAILABLE:
            abas.addTab(ViewerTab(status), "Visualizador 3D")
        else:
            abas.addTab(
                MissingDepsTab(
                    "Visualizador 3D",
                    "Dependências ausentes para esta função:\n"
                    "    No module named 'pyvista'\n\n"
                    "Instale com:\n    python -m pip install pyvista matplotlib",
                ),
                "Visualizador 3D",
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
