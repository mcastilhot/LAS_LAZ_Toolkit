"""
LAS / LAZ Tools — TerraSolid / Spatix   (versão tkinter, sem PySide6/Qt)

Dependências:
    numpy, laspy[lazrs]          (laspy só é necessário para a aba de conversão 1.4→1.2)
    tkinterdnd2                  (OPCIONAL — habilita arrastar e soltar)
"""
import os
import re
import sys
import shutil
import queue
import threading
import html as _html
from pathlib import Path
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

try:
    import laspy
    LASPY_AVAILABLE = True
except ImportError:
    LASPY_AVAILABLE = False

# Arrastar e soltar (opcional)
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
#  PALETA
# ══════════════════════════════════════════════════════════════════════════════
C = {
    "bg": "#F5F6F7", "surface": "#FFFFFF", "border": "#DDE1E7", "border2": "#C8CDD6",
    "text": "#1A1D23", "text2": "#4B5563", "text3": "#8B95A1",
    "accent": "#2A5FAC", "accentHov": "#1E4A8A", "accentBg": "#EAF0FA",
    "danger": "#B91C1C", "selBg": "#E8EFF9", "altRow": "#FAFBFC",
    "green": "#1A6B3C", "logBg": "#FAFBFC",
}
FONT = ("Segoe UI", 10)
FONT_B = ("Segoe UI", 10, "bold")
FONT_S = ("Segoe UI", 9)
MONO = ("Consolas", 9)


def setup_style(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=C["bg"])
    st.configure(".", background=C["bg"], foreground=C["text"], font=FONT)
    st.configure("TFrame", background=C["bg"])
    st.configure("Card.TFrame", background=C["surface"], bordercolor=C["border"],
                 relief="solid", borderwidth=1)
    st.configure("TLabel", background=C["bg"], foreground=C["text"])
    st.configure("Card.TLabel", background=C["surface"])
    st.configure("Hint.TLabel", foreground=C["text3"], font=FONT_S)
    st.configure("Sec.TLabel", foreground=C["text2"], font=("Segoe UI", 9, "bold"))
    st.configure("CardSec.TLabel", background=C["surface"], foreground=C["text2"],
                 font=("Segoe UI", 9, "bold"))
    st.configure("StatVal.TLabel", background=C["surface"], font=("Segoe UI", 18, "bold"))
    st.configure("StatAcc.TLabel", background=C["surface"], foreground=C["accent"],
                 font=("Segoe UI", 18, "bold"))
    st.configure("StatLbl.TLabel", background=C["surface"], foreground=C["text3"], font=("Segoe UI", 8))

    st.configure("TButton", background=C["surface"], foreground=C["text2"],
                 bordercolor=C["border"], padding=(14, 7), font=FONT)
    st.map("TButton", background=[("active", "#ECEEF1"), ("disabled", C["bg"])],
           foreground=[("disabled", C["text3"])])
    st.configure("Primary.TButton", background=C["accent"], foreground="#FFFFFF",
                 bordercolor=C["accent"], padding=(20, 9), font=FONT_B)
    st.map("Primary.TButton", background=[("active", C["accentHov"]), ("disabled", "#8FB3DC")],
           foreground=[("disabled", "#FFFFFF")])
    st.configure("Danger.TButton", foreground=C["danger"])

    st.configure("TNotebook", background=C["bg"], borderwidth=0)
    st.configure("TNotebook.Tab", background=C["bg"], foreground=C["text2"],
                 padding=(18, 8), bordercolor=C["border"])
    st.map("TNotebook.Tab", background=[("selected", C["surface"])],
           foreground=[("selected", C["accent"])])

    st.configure("TCheckbutton", background=C["bg"], foreground=C["text2"])
    st.configure("Card.TCheckbutton", background=C["surface"], foreground=C["text2"])
    st.configure("TRadiobutton", background=C["bg"], foreground=C["text2"], padding=(6, 4))
    st.configure("Card.TRadiobutton", background=C["surface"], foreground=C["text2"], padding=(6, 4))
    st.configure("TEntry", fieldbackground=C["surface"], bordercolor=C["border"], padding=5)
    st.configure("TCombobox", fieldbackground=C["surface"], bordercolor=C["border"], padding=5)
    st.configure("Horizontal.TProgressbar", troughcolor=C["border"], background=C["accent"],
                 bordercolor=C["border"], thickness=6)


# ══════════════════════════════════════════════════════════════════════════════
#  LÓGICA (rodam em threads; comunicam via emit(tipo, *args))
# ══════════════════════════════════════════════════════════════════════════════

def work_rename(files, target_ext, emit):
    ok = erros = 0
    for i, src in enumerate(files):
        dst = src.with_suffix(target_ext)
        try:
            src.rename(dst)
            emit("file", str(src), str(dst), True); ok += 1
        except Exception as e:
            emit("file", str(src), str(e), False); erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


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
            emit("file", str(src), str(dst), True); ok += 1
        except Exception as e:
            emit("file", str(src), str(e), False); erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


def prj_replace(text, mode):
    n_las = n_laz = 0
    if mode in ("las_to_laz", "both"):
        def sub_las(m):
            nonlocal n_las; n_las += 1
            orig = m.group(0)
            if orig.upper().startswith("STORAGE=LAS"):
                repl = "LAZ" if orig[8:11].isupper() else "laz"
                return orig[:8] + repl + orig[11:]
            return orig[:-3] + ("LAZ" if orig[-3:].isupper() else "laz")
        text = re.sub(r'(?i)(?:Storage=LAS(?=\d))|(?:\.las\b)', sub_las, text)
    if mode in ("laz_to_las", "both"):
        def sub_laz(m):
            nonlocal n_laz; n_laz += 1
            orig = m.group(0)
            if orig.upper().startswith("STORAGE=LAZ"):
                repl = "LAS" if orig[8:11].isupper() else "las"
                return orig[:8] + repl + orig[11:]
            return orig[:-3] + ("LAS" if orig[-3:].isupper() else "las")
        text = re.sub(r'(?i)(?:Storage=LAZ(?=\d))|(?:\.laz\b)', sub_laz, text)
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
                total_las += n_las; total_laz += n_laz
                emit("file", str(prj), n_las, n_laz, True,
                     f"{n_las} .las→.laz  |  {n_laz} .laz→.las")
                files_ok += 1
        except Exception as e:
            emit("file", str(prj), 0, 0, False, str(e)); files_err += 1
        emit("progress", i + 1)
    emit("done", files_ok, files_err, total_las, total_laz)


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
                emit("file", name, True, "copiado"); ok += 1
            except Exception as e:
                emit("file", name, False, str(e)); erros += 1
        else:
            emit("file", name, False, "arquivo não encontrado"); erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


# ── Conversão LAS/LAZ 1.4 → 1.2 ───────────────────────────────────────────────
# 6->1, 7->3, 8->3 (perde NIR), 9->1 (perde waveform), 10->3 (perde NIR e waveform)
POINT_FORMAT_DOWNGRADE = {6: 1, 7: 3, 8: 3, 9: 1, 10: 3}


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
            las = laspy.read(str(src))
            versao = las.header.version
            if not (versao.major == 1 and versao.minor == 4):
                emit("file", src.name, False,
                     f"ignorado — versão atual é {versao.major}.{versao.minor}, não 1.4")
                erros += 1; emit("progress", i + 1); continue

            era_copc = _is_copc(las)
            if era_copc:
                _strip_copc(las)

            origem_pf = las.header.point_format.id
            destino_pf = POINT_FORMAT_DOWNGRADE.get(origem_pf, 3)

            try:
                crs = las.header.parse_crs()
            except Exception:
                crs = None

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

            # Mantém as classes originais gravando o byte completo
            classe_mantida = True
            if n_class_alta:
                names = convertido.points.array.dtype.names or ()
                if "raw_classification" in names:
                    raw_atual = np.asarray(convertido.points.array["raw_classification"]).copy()
                    raw_atual[mask_alta] = classif_orig[mask_alta]
                    classe_mantida = _set_raw_classification_byte(convertido, raw_atual)
                else:
                    classe_mantida = False

            if crs is not None:
                try:
                    convertido.header.add_crs(crs)
                except Exception:
                    pass

            nome_saida = src.stem + ("_v12" if add_suffix else "") + ".laz"
            dest_path = Path(dest_folder) / nome_saida
            try:
                convertido.write(str(dest_path))
            except NotImplementedError as e:
                emit("file", src.name, False, f"ignorado — não foi possível gravar: {e}")
                erros += 1; emit("progress", i + 1); continue

            perdas = []
            if origem_pf in (8, 10): perdas.append("NIR")
            if origem_pf in (9, 10): perdas.append("waveform")
            if era_copc: perdas.append("indexação COPC/octree")
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
        except Exception as e:
            emit("file", src.name, False, str(e)); erros += 1
        emit("progress", i + 1)
    emit("done", ok, erros)


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS COMPARTILHADOS
# ══════════════════════════════════════════════════════════════════════════════

class Runner:
    """Executa uma função em thread e entrega os eventos na thread da interface."""
    def __init__(self, widget, handler):
        self.w = widget
        self.handler = handler       # handler(tipo, *args)
        self.q = queue.Queue()
        self.running = False

    def start(self, target, *args):
        self.running = True
        threading.Thread(target=target, args=(*args, self._emit), daemon=True).start()
        self.w.after(60, self._poll)

    def _emit(self, kind, *a):
        self.q.put((kind, a))

    def _poll(self):
        try:
            while True:
                kind, a = self.q.get_nowait()
                self.handler(kind, *a)
                if kind == "done":
                    self.running = False
        except queue.Empty:
            pass
        if self.running:
            self.w.after(60, self._poll)


def enable_drop(widget, callback):
    if not DND_AVAILABLE:
        return
    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda e: callback(
            [Path(p) for p in widget.tk.splitlist(e.data)]))
    except Exception:
        pass


class FileList(ttk.Frame):
    """Listbox com rolagem; guarda uma lista de Paths."""
    def __init__(self, master, exts, fmt=None, height=10, font=FONT, on_select=None):
        super().__init__(master)
        self.exts = tuple(e.lower() for e in exts)
        self.fmt = fmt or (lambda p: p.name)
        self.paths = []
        self.lb = tk.Listbox(self, selectmode="extended", activestyle="none", height=height,
                             font=font, bg=C["surface"], fg=C["text"], bd=0,
                             highlightthickness=1, highlightbackground=C["border"],
                             highlightcolor=C["accent"], selectbackground=C["selBg"],
                             selectforeground=C["accent"], exportselection=False)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        if on_select:
            self.lb.bind("<<ListboxSelect>>", lambda e: on_select())
        enable_drop(self.lb, self.add_paths)

    def add_paths(self, paths):
        existentes = {str(p) for p in self.paths}
        add = 0
        for p in paths:
            p = Path(p)
            cand = []
            if p.is_dir():
                cand = [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in self.exts]
            elif p.is_file() and p.suffix.lower() in self.exts:
                cand = [p]
            for f in cand:
                if str(f) not in existentes:
                    self.paths.append(f); existentes.add(str(f)); add += 1
        if add:
            self.refresh()
        return add

    def refresh(self):
        self.lb.delete(0, "end")
        for p in self.paths:
            self.lb.insert("end", "  " + self.fmt(p))

    def selected_paths(self):
        return [self.paths[i] for i in self.lb.curselection()]

    def remove_selected(self):
        sel = {str(p) for p in self.selected_paths()}
        self.paths = [p for p in self.paths if str(p) not in sel]
        self.refresh()
        return len(sel)

    def clear(self):
        self.paths = []
        self.refresh()


class Stats(ttk.Frame):
    """Linha de cartões de estatística."""
    def __init__(self, master, items):
        super().__init__(master)
        self.vars = {}
        for i, (key, label, accent) in enumerate(items):
            card = ttk.Frame(self, style="Card.TFrame", padding=(12, 8))
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            self.columnconfigure(i, weight=1)
            v = tk.StringVar(value="0")
            self.vars[key] = v
            ttk.Label(card, textvariable=v, anchor="center",
                      style="StatAcc.TLabel" if accent else "StatVal.TLabel").pack(fill="x")
            ttk.Label(card, text=label, anchor="center", style="StatLbl.TLabel").pack(fill="x")

    def set(self, key, val):
        self.vars[key].set(str(val))

    def reset(self, *keys):
        for k in (keys or self.vars.keys()):
            self.vars[k].set("0")


def make_log(master, height=5):
    frame = ttk.Frame(master)
    t = tk.Text(frame, height=height, font=("Consolas", 8), bg=C["logBg"], fg=C["text"],
                bd=0, highlightthickness=1, highlightbackground=C["border"], wrap="none",
                state="disabled")
    sb = ttk.Scrollbar(frame, command=t.yview)
    t.configure(yscrollcommand=sb.set)
    t.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
    t.tag_configure("ok", foreground=C["green"])
    t.tag_configure("err", foreground=C["danger"])
    t.tag_configure("muted", foreground=C["text3"])
    return frame, t


def log_add(t, text, tag):
    t.configure(state="normal")
    t.insert("end", text + "\n", tag)
    t.see("end")
    t.configure(state="disabled")


def log_clear(t):
    t.configure(state="normal"); t.delete("1.0", "end"); t.configure(state="disabled")


def card(master, title=None, pady=(12, 12)):
    f = ttk.Frame(master, style="Card.TFrame", padding=(16, pady[0], 16, pady[1]))
    if title:
        ttk.Label(f, text=title, style="CardSec.TLabel").pack(anchor="w", pady=(0, 8))
    return f


def set_enabled(widgets, enabled):
    for w in widgets:
        try:
            w.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 1 — RENOMEAR ARQUIVOS
# ══════════════════════════════════════════════════════════════════════════════

class RenameTab(ttk.Frame):
    def __init__(self, master, status):
        super().__init__(master, padding=(24, 18))
        self.status = status
        self.mode = tk.StringVar(value=".laz")
        self.ok = self.err = 0
        self.runner = Runner(self, self._on_event)

        tb = ttk.Frame(self); tb.pack(fill="x")
        ttk.Label(tb, text="CONVERTER PARA:", style="Sec.TLabel").pack(side="left", padx=(0, 6))
        ttk.Radiobutton(tb, text=".LAS → .LAZ", value=".laz", variable=self.mode,
                        command=self._refresh).pack(side="left")
        ttk.Radiobutton(tb, text=".LAZ → .LAS", value=".las", variable=self.mode,
                        command=self._refresh).pack(side="left", padx=(0, 16))
        self.btn_add = ttk.Button(tb, text="+ Adicionar", command=self._add_files)
        self.btn_dir = ttk.Button(tb, text="Pasta", command=self._add_folder)
        self.btn_clr = ttk.Button(tb, text="Limpar", style="Danger.TButton", command=self._clear)
        for b in (self.btn_add, self.btn_dir, self.btn_clr):
            b.pack(side="left", padx=3)

        ttk.Label(self, text="Arraste e solte arquivos .LAS ou .LAZ aqui"
                  if DND_AVAILABLE else "Use os botões para adicionar arquivos .LAS ou .LAZ",
                  style="Hint.TLabel").pack(anchor="w", pady=(12, 4))
        self.flist = FileList(self, (".las", ".laz"), fmt=self._fmt)
        self.flist.pack(fill="both", expand=True)

        self.stats = Stats(self, [("total", "Total na fila", False), ("las", "Arquivos .LAS", False),
                                  ("laz", "Arquivos .LAZ", False), ("ok", "Renomeados", True),
                                  ("err", "Erros", False)])
        self.stats.pack(fill="x", pady=12)

        self.pb = ttk.Progressbar(self, mode="determinate")
        bottom = ttk.Frame(self); bottom.pack(fill="x", pady=(6, 0))
        self.btn_rem = ttk.Button(bottom, text="Remover selecionados", command=self._remove_sel)
        self.btn_run = ttk.Button(bottom, text="▶  Renomear arquivos", style="Primary.TButton",
                                  command=self._run)
        self.btn_rem.pack(side="left"); self.btn_run.pack(side="right")
        self.pb_pack = lambda: self.pb.pack(fill="x", before=bottom)

    def _fmt(self, p):
        if p.suffix.lower() == self.mode.get():
            return f"{p.name}  —  já é {self.mode.get()[1:].upper()}"
        return f"{p.name}  →  {p.with_suffix(self.mode.get()).name}"

    def _refresh(self):
        self.flist.refresh()
        las = sum(1 for p in self.flist.paths if p.suffix.lower() == ".las")
        self.stats.set("total", len(self.flist.paths))
        self.stats.set("las", las)
        self.stats.set("laz", len(self.flist.paths) - las)

    def _add_files(self):
        ps = filedialog.askopenfilenames(title="Selecionar arquivos",
                                         filetypes=[("Nuvem de pontos", "*.las *.laz *.LAS *.LAZ")])
        self._add(ps)

    def _add_folder(self):
        f = filedialog.askdirectory(title="Selecionar pasta")
        if f: self._add([f])

    def _add(self, paths):
        n = self.flist.add_paths(paths)
        if n:
            self._refresh()
            self.status(f"{n} arquivo(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self):
        n = self.flist.remove_selected(); self._refresh()
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self):
        self.flist.clear(); self.stats.reset(); self.status("Lista limpa.")

    def _run(self):
        mode = self.mode.get()
        targets = [p for p in self.flist.paths if p.suffix.lower() != mode]
        if not targets:
            messagebox.showinfo("Nada a fazer", f"Todos os arquivos já têm extensão {mode[1:].upper()}.")
            return
        if not messagebox.askyesno("Confirmar",
                f"Renomear {len(targets)} arquivo(s) para {mode[1:].upper()}?\n"
                "Esta ação modifica os arquivos no disco."):
            return
        self.ok = self.err = 0; self.stats.reset("ok", "err")
        self.pb.configure(maximum=len(targets), value=0); self.pb_pack()
        self._busy(True)
        self.runner.start(work_rename, targets, mode)

    def _on_event(self, kind, *a):
        if kind == "progress":
            self.pb.configure(value=a[0])
        elif kind == "file":
            original, resultado, sucesso = a
            if sucesso:
                self.ok += 1; self.stats.set("ok", self.ok)
                orig = Path(original)
                for i, p in enumerate(self.flist.paths):
                    if p == orig:
                        self.flist.paths[i] = Path(resultado); break
            else:
                self.err += 1; self.stats.set("err", self.err)
        elif kind == "done":
            ok, erros = a
            self._busy(False); self.pb.pack_forget(); self._refresh()
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) renomeado(s).")
                messagebox.showinfo("Concluído", f"{ok} arquivo(s) renomeado(s).")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                messagebox.showwarning("Concluído com erros", f"{ok} renomeado(s).\n{erros} com erro.")

    def _busy(self, busy):
        set_enabled([self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_rem], not busy)
        self.btn_run.configure(text="Processando…" if busy else "▶  Renomear arquivos")


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 2 — EDITAR PRJ
# ══════════════════════════════════════════════════════════════════════════════

class PrjEditTab(ttk.Frame):
    def __init__(self, master, status):
        super().__init__(master, padding=(24, 18))
        self.status = status
        self.mode = tk.StringVar(value="las_to_laz")
        self.bak = tk.BooleanVar(value=True)
        self.alter = self.err = self.n_las = self.n_laz = 0
        self.runner = Runner(self, self._on_event)

        opt = card(self); opt.pack(fill="x")
        row = ttk.Frame(opt, style="Card.TFrame"); row.pack(fill="x")
        ttk.Label(row, text="SUBSTITUIÇÃO:", style="CardSec.TLabel").pack(side="left", padx=(0, 6))
        for txt, val in ((".LAS → .LAZ", "las_to_laz"), (".LAZ → .LAS", "laz_to_las"), ("Ambos", "both")):
            ttk.Radiobutton(row, text=txt, value=val, variable=self.mode,
                            style="Card.TRadiobutton").pack(side="left")
        ttk.Checkbutton(row, text="Criar backup antes de salvar", variable=self.bak,
                        style="Card.TCheckbutton").pack(side="left", padx=16)
        self.btn_clr = ttk.Button(row, text="Limpar", style="Danger.TButton", command=self._clear)
        self.btn_dir = ttk.Button(row, text="Pasta", command=self._add_dir)
        self.btn_add = ttk.Button(row, text="+ PRJ", command=self._add_prj)
        for b in (self.btn_clr, self.btn_dir, self.btn_add):
            b.pack(side="right", padx=3)

        pw = ttk.PanedWindow(self, orient="horizontal"); pw.pack(fill="both", expand=True, pady=12)
        left = ttk.Frame(pw)
        ttk.Label(left, text="Arquivos PRJ" + (" — arraste e solte" if DND_AVAILABLE else ""),
                  style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        self.flist = FileList(left, (".prj",), on_select=self._preview)
        self.flist.pack(fill="both", expand=True)
        pw.add(left, weight=1)

        right = ttk.Frame(pw)
        ttk.Label(right, text="Pré-visualização do conteúdo", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        pf = ttk.Frame(right); pf.pack(fill="both", expand=True)
        self.preview = tk.Text(pf, font=MONO, bg=C["surface"], fg=C["text"], bd=0,
                               highlightthickness=1, highlightbackground=C["border"],
                               wrap="word", state="disabled")
        sb = ttk.Scrollbar(pf, command=self.preview.yview)
        self.preview.configure(yscrollcommand=sb.set)
        self.preview.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.preview.tag_configure("las", background="#FEF3C7", foreground="#92400E",
                                   font=("Consolas", 9, "bold"))
        self.preview.tag_configure("laz", background="#DBEAFE", foreground="#1E40AF",
                                   font=("Consolas", 9, "bold"))
        pw.add(right, weight=2)

        self.stats = Stats(self, [("total", "PRJs na fila", False), ("alter", "Alterados", True),
                                  ("err", "Erros", False), ("l2z", "Ref. .las→.laz", False),
                                  ("z2l", "Ref. .laz→.las", False)])
        self.stats.pack(fill="x")

        ttk.Label(self, text="Log de operações", style="Hint.TLabel").pack(anchor="w", pady=(10, 4))
        lf, self.log = make_log(self, 4); lf.pack(fill="x")

        self.pb = ttk.Progressbar(self, mode="determinate")
        bottom = ttk.Frame(self); bottom.pack(fill="x", pady=(8, 0))
        self.btn_rem = ttk.Button(bottom, text="Remover selecionados", command=self._remove_sel)
        self.btn_run = ttk.Button(bottom, text="▶  Aplicar substituições", style="Primary.TButton",
                                  command=self._run)
        self.btn_rem.pack(side="left"); self.btn_run.pack(side="right")
        self.pb_pack = lambda: self.pb.pack(fill="x", before=bottom)

    def _add_prj(self):
        ps = filedialog.askopenfilenames(title="Selecionar PRJ",
                                         filetypes=[("TerraSolid/Spatix PRJ", "*.prj *.PRJ")])
        self._add(ps)

    def _add_dir(self):
        f = filedialog.askdirectory(title="Selecionar pasta")
        if f: self._add([f])

    def _add(self, paths):
        n = self.flist.add_paths(paths)
        if n:
            self.stats.set("total", len(self.flist.paths))
            self.status(f"{n} PRJ(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self):
        n = self.flist.remove_selected()
        self.stats.set("total", len(self.flist.paths)); self._set_preview("")
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self):
        self.flist.clear(); self._set_preview(""); log_clear(self.log); self.stats.reset()
        self.status("Lista limpa.")

    def _set_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        if text:
            self.preview.insert("1.0", text)
            for pat, tag in ((r'(?i)Storage=(LAS)(?=\d)', "las"), (r'(?i)Storage=(LAZ)(?=\d)', "laz"),
                             (r'(?i)(\.las)\b', "las"), (r'(?i)(\.laz)\b', "laz")):
                for m in re.finditer(pat, text):
                    self.preview.tag_add(tag, f"1.0+{m.start(1)}c", f"1.0+{m.end(1)}c")
        self.preview.configure(state="disabled")

    def _preview(self):
        sel = self.flist.selected_paths()
        if not sel:
            return
        try:
            self._set_preview(sel[0].read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            self._set_preview(f"Erro: {e}")

    def _run(self):
        if not self.flist.paths:
            messagebox.showinfo("Nada a fazer", "Adicione arquivos PRJ."); return
        modo_txt = {"las_to_laz": ".LAS → .LAZ", "laz_to_las": ".LAZ → .LAS",
                    "both": ".LAS ↔ .LAZ"}[self.mode.get()]
        bak_txt = "com backup" if self.bak.get() else "SEM backup"
        if not messagebox.askyesno("Confirmar substituição",
                f"Aplicar substituição {modo_txt} em {len(self.flist.paths)} PRJ(s)?\n"
                f"Modo: {bak_txt}.\nEsta ação modifica arquivos no disco."):
            return
        self.stats.reset("alter", "err", "l2z", "z2l"); log_clear(self.log)
        self.alter = self.err = self.n_las = self.n_laz = 0
        self.pb.configure(maximum=len(self.flist.paths), value=0); self.pb_pack()
        self._busy(True)
        self.runner.start(work_prj_edit, list(self.flist.paths), self.mode.get(), self.bak.get())

    def _on_event(self, kind, *a):
        if kind == "progress":
            self.pb.configure(value=a[0])
        elif kind == "file":
            path, n_las, n_laz, ok, msg = a
            name = Path(path).name
            if ok:
                if n_las > 0 or n_laz > 0:
                    self.alter += 1; self.stats.set("alter", self.alter)
                    self.n_las += n_las; self.n_laz += n_laz
                    self.stats.set("l2z", self.n_las); self.stats.set("z2l", self.n_laz)
                    tag, prefix = "ok", "✔"
                else:
                    tag, prefix = "muted", "—"
            else:
                self.err += 1; self.stats.set("err", self.err); tag, prefix = "err", "✖"
            log_add(self.log, f"{prefix}  {name}  —  {msg}", tag)
        elif kind == "done":
            ok, erros, n_las, n_laz = a
            self._busy(False); self.pb.pack_forget(); self._preview()
            if erros == 0:
                self.status(f"Concluído — {ok} PRJ(s) alterado(s).")
                messagebox.showinfo("Concluído",
                    f"{ok} PRJ(s) alterado(s).\n.las→.laz: {n_las}  |  .laz→.las: {n_laz}")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                messagebox.showwarning("Concluído com erros", f"{ok} processado(s).\n{erros} com erro.")

    def _busy(self, busy):
        set_enabled([self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_rem], not busy)
        self.btn_run.configure(text="Processando…" if busy else "▶  Aplicar substituições")


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 3 — COPIADOR PRJ
# ══════════════════════════════════════════════════════════════════════════════

class PrjCopyTab(ttk.Frame):
    def __init__(self, master, status):
        super().__init__(master, padding=(24, 18))
        self.status = status
        self.entries = []
        self.folder = tk.StringVar()
        self.dest = tk.StringVar()
        self.runner = Runner(self, self._on_event)
        self.ok = self.err = 0
        self.problemas = []

        s1 = card(self, "1. PASTA COM O ARQUIVO PRJ"); s1.pack(fill="x")
        r = ttk.Frame(s1, style="Card.TFrame"); r.pack(fill="x")
        ttk.Entry(r, textvariable=self.folder, state="readonly").pack(side="left", fill="x", expand=True)
        self.btn_pick = ttk.Button(r, text="Selecionar pasta", command=self._pick_folder)
        self.btn_pick.pack(side="left", padx=(8, 0))
        r2 = ttk.Frame(s1, style="Card.TFrame"); r2.pack(fill="x", pady=(8, 0))
        self.combo = ttk.Combobox(r2, state="readonly", values=[])
        self.combo.pack(side="left", fill="x", expand=True)
        self.btn_read = ttk.Button(r2, text="Ler PRJ", style="Primary.TButton", command=self._read_prj)
        self.btn_read.pack(side="left", padx=(8, 0))

        s2 = card(self, "2. ARQUIVOS ENCONTRADOS NO PRJ"); s2.pack(fill="both", expand=True, pady=12)
        r3 = ttk.Frame(s2, style="Card.TFrame"); r3.pack(fill="x", pady=(0, 6))
        self.btn_sel = ttk.Button(r3, text="Selecionar todos", command=self._sel_all)
        self.btn_des = ttk.Button(r3, text="Desmarcar todos", command=self._des_all)
        self.btn_sel.pack(side="left"); self.btn_des.pack(side="left", padx=6)
        self.count_lbl = ttk.Label(r3, text="0 / 0 selecionados", style="Card.TLabel",
                                   foreground=C["text3"])
        self.count_lbl.pack(side="right")
        self.flist = FileList(s2, (".las", ".laz"), font=MONO, on_select=self._upd_count, height=8)
        self.flist.pack(fill="both", expand=True)

        s3 = card(self, "3. PASTA DE DESTINO"); s3.pack(fill="x")
        r4 = ttk.Frame(s3, style="Card.TFrame"); r4.pack(fill="x")
        ttk.Entry(r4, textvariable=self.dest, state="readonly").pack(side="left", fill="x", expand=True)
        self.btn_dest = ttk.Button(r4, text="Selecionar destino", command=self._pick_dest)
        self.btn_dest.pack(side="left", padx=(8, 0))

        self.pb = ttk.Progressbar(self, mode="determinate")
        bottom = ttk.Frame(self); bottom.pack(fill="x", pady=(12, 0))
        self.copy_lbl = ttk.Label(bottom, text="", style="Hint.TLabel"); self.copy_lbl.pack(side="left")
        self.btn_copy = ttk.Button(bottom, text="▶  Copiar arquivos selecionados",
                                   style="Primary.TButton", command=self._copy)
        self.btn_copy.pack(side="right")
        self.pb_pack = lambda: self.pb.pack(fill="x", before=bottom)

    def _sel_all(self):
        self.flist.lb.selection_set(0, "end"); self._upd_count()

    def _des_all(self):
        self.flist.lb.selection_clear(0, "end"); self._upd_count()

    def _upd_count(self):
        total = len(self.flist.paths); sel = len(self.flist.lb.curselection())
        self.count_lbl.configure(text=f"{sel} / {total} selecionados")
        self.copy_lbl.configure(text=f"{sel} arquivo(s) a copiar")

    def _pick_folder(self):
        f = filedialog.askdirectory(title="Selecionar pasta do PRJ")
        if not f: return
        self.folder.set(f)
        prjs = [x for x in os.listdir(f) if x.lower().endswith(".prj")]
        self.combo.configure(values=prjs)
        if prjs: self.combo.current(0)
        else: self.combo.set("")
        self.status(f"{len(prjs)} PRJ(s) encontrado(s) na pasta." if prjs
                    else "Nenhum arquivo .PRJ encontrado na pasta selecionada.")

    def _read_prj(self):
        folder, name = self.folder.get(), self.combo.get()
        if not folder or not name:
            messagebox.showwarning("Atenção", "Selecione a pasta e o arquivo PRJ primeiro."); return
        try:
            content = Path(folder, name).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível ler o PRJ:\n{e}"); return
        matches = re.findall(r'[^\s\n\r\t"\'<>|*?]+\.la[sz]', content, re.IGNORECASE)
        seen = set(); unique = []
        for m in matches:
            key = os.path.basename(m).lower()
            if key not in seen:
                seen.add(key); unique.append(m.strip())
        self.entries = unique
        # aqui os "paths" são as strings do PRJ (podem ser relativas/inexistentes)
        self.flist.paths = [Path(e) for e in unique]
        self.flist.fmt = lambda p: str(p)
        self.flist.refresh()
        self._sel_all()
        self.status(f"{len(unique)} arquivo(s) encontrado(s) no PRJ.")

    def _pick_dest(self):
        f = filedialog.askdirectory(title="Selecionar pasta de destino")
        if f:
            self.dest.set(f); self.status(f"Destino: {f}")

    def _copy(self):
        sel = self.flist.lb.curselection()
        if not sel:
            messagebox.showwarning("Atenção", "Selecione ao menos um arquivo."); return
        if not self.dest.get():
            messagebox.showwarning("Atenção", "Selecione a pasta de destino."); return
        if not messagebox.askyesno("Confirmar cópia",
                f"Copiar {len(sel)} arquivo(s) para:\n{self.dest.get()}?"):
            return
        entries = [self.entries[i] for i in sel]
        self.ok = self.err = 0; self.problemas = []
        self.pb.configure(maximum=len(entries), value=0); self.pb_pack()
        self._busy(True)
        self.runner.start(work_copy, entries, self.folder.get(), self.dest.get())

    def _on_event(self, kind, *a):
        if kind == "progress":
            self.pb.configure(value=a[0])
            self.status(f"Copiando… {self.ok + self.err}/{int(self.pb['maximum'])}")
        elif kind == "file":
            name, ok, msg = a
            if ok: self.ok += 1
            else:
                self.err += 1; self.problemas.append(f"{name}  ({msg})")
        elif kind == "done":
            ok, erros = a
            self._busy(False); self.pb.pack_forget()
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) copiado(s).")
                messagebox.showinfo("Concluído", f"{ok} arquivo(s) copiado(s) com sucesso.")
            else:
                self.status(f"Concluído — {ok} OK, {erros} não encontrado(s)/erro(s).")
                messagebox.showwarning("Concluído com avisos",
                    f"{ok} copiado(s).\n{erros} arquivo(s) com problema:\n\n" + "\n".join(self.problemas))

    def _busy(self, busy):
        set_enabled([self.btn_copy, self.btn_read, self.btn_pick, self.btn_dest,
                     self.btn_sel, self.btn_des], not busy)
        self.btn_copy.configure(text="Copiando…" if busy else "▶  Copiar arquivos selecionados")


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 4 — .LAZ → .laz
# ══════════════════════════════════════════════════════════════════════════════

class LowercaseLazTab(ttk.Frame):
    def __init__(self, master, status):
        super().__init__(master, padding=(24, 18))
        self.status = status
        self.folder = None
        self.files = []
        self.folder_var = tk.StringVar()
        self.sub = tk.BooleanVar(value=False)
        self.ok = self.err = 0
        self.runner = Runner(self, self._on_event)

        s = card(self, "PASTA COM ARQUIVOS .LAZ"); s.pack(fill="x")
        r = ttk.Frame(s, style="Card.TFrame"); r.pack(fill="x")
        ttk.Entry(r, textvariable=self.folder_var, state="readonly").pack(side="left", fill="x", expand=True)
        self.btn_folder = ttk.Button(r, text="Selecionar pasta", command=self._pick_folder)
        self.btn_folder.pack(side="left", padx=(8, 0))
        self.chk = ttk.Checkbutton(s, text="Incluir subpastas", variable=self.sub,
                                   style="Card.TCheckbutton", command=self._scan)
        self.chk.pack(anchor="w", pady=(8, 0))

        ttk.Label(self, text="Pré-visualização — somente .LAZ maiúsculo será renomeado para .laz minúsculo",
                  style="Hint.TLabel").pack(anchor="w", pady=(12, 4))
        self.lb = FileList(self, (".laz",), font=MONO)
        self.lb.pack(fill="both", expand=True)

        self.stats = Stats(self, [("found", "Arquivos .LAZ encontrados", False),
                                  ("ok", "Renomeados", True), ("err", "Erros", False)])
        self.stats.pack(fill="x", pady=12)

        self.pb = ttk.Progressbar(self, mode="determinate")
        bottom = ttk.Frame(self); bottom.pack(fill="x", pady=(6, 0))
        self.btn_refresh = ttk.Button(bottom, text="Atualizar lista", command=self._scan)
        self.btn_run = ttk.Button(bottom, text="▶  Trocar .LAZ por .laz", style="Primary.TButton",
                                  command=self._run)
        self.btn_refresh.pack(side="left"); self.btn_run.pack(side="right")
        self.pb_pack = lambda: self.pb.pack(fill="x", before=bottom)

    def _pick_folder(self):
        f = filedialog.askdirectory(title="Selecionar pasta com arquivos .LAZ")
        if not f: return
        self.folder = Path(f); self.folder_var.set(f); self._scan()

    def _scan(self):
        self.files = []
        self.lb.paths = []; self.lb.refresh()
        if not self.folder or not self.folder.exists():
            self.stats.set("found", 0); return
        it = self.folder.rglob("*") if self.sub.get() else self.folder.iterdir()
        # comparação exata: .laz minúsculo é ignorado
        self.files = sorted([p for p in it if p.is_file() and p.suffix == ".LAZ"],
                            key=lambda p: str(p).lower())
        self.lb.paths = list(self.files)
        self.lb.fmt = lambda p: f"{p.name}  →  {p.name[:-4]}.laz"
        self.lb.refresh()
        self.stats.set("found", len(self.files)); self.stats.set("ok", 0); self.stats.set("err", 0)
        self.status(f"{len(self.files)} arquivo(s) com extensão .LAZ maiúscula encontrado(s).")

    def _run(self):
        if not self.folder:
            messagebox.showinfo("Selecione uma pasta", "Selecione primeiro a pasta que contém os arquivos .LAZ.")
            return
        self._scan()
        if not self.files:
            messagebox.showinfo("Nada a fazer", "Não há arquivos com extensão .LAZ maiúscula nesta pasta.")
            return
        if not messagebox.askyesno("Confirmar",
                f"Trocar a extensão de {len(self.files)} arquivo(s) de .LAZ para .laz?\n\n"
                "O nome do arquivo será mantido. Apenas as letras da extensão serão convertidas para minúsculas."):
            return
        self.ok = self.err = 0; self.stats.reset("ok", "err")
        self.pb.configure(maximum=len(self.files), value=0); self.pb_pack()
        self._busy(True)
        self.runner.start(work_lowercase_laz, list(self.files))

    def _on_event(self, kind, *a):
        if kind == "progress":
            self.pb.configure(value=a[0])
        elif kind == "file":
            if a[2]: self.ok += 1; self.stats.set("ok", self.ok)
            else: self.err += 1; self.stats.set("err", self.err)
        elif kind == "done":
            ok, erros = a
            self._busy(False); self.pb.pack_forget(); self._scan()
            self.stats.set("ok", ok); self.stats.set("err", erros)
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) alterado(s) de .LAZ para .laz.")
                messagebox.showinfo("Concluído", f"{ok} arquivo(s) alterado(s) de .LAZ para .laz.")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                messagebox.showwarning("Concluído com erros",
                                       f"{ok} arquivo(s) alterado(s).\n{erros} arquivo(s) apresentaram erro.")

    def _busy(self, busy):
        set_enabled([self.btn_folder, self.btn_refresh, self.btn_run, self.chk], not busy)
        self.btn_run.configure(text="Processando…" if busy else "▶  Trocar .LAZ por .laz")


# ══════════════════════════════════════════════════════════════════════════════
#  ABA 5 — CONVERTER LAZ 1.4 → 1.2
# ══════════════════════════════════════════════════════════════════════════════

class ConvertVersionTab(ttk.Frame):
    def __init__(self, master, status):
        super().__init__(master, padding=(24, 18))
        self.status = status
        self.dest = tk.StringVar()
        self.suffix = tk.BooleanVar(value=True)
        self.ok = self.err = 0
        self.runner = Runner(self, self._on_event)

        if not LASPY_AVAILABLE:
            tk.Label(self, text="A biblioteca laspy não está instalada.\n"
                     "Instale com:  pip install laspy[lazrs]  e reinicie o programa.",
                     bg="#FEF2F2", fg=C["danger"], justify="left", anchor="w", padx=12, pady=10,
                     font=FONT).pack(fill="x", pady=(0, 10))

        tb = ttk.Frame(self); tb.pack(fill="x")
        ttk.Label(tb, text="ENTRADA (LAZ/LAS 1.4):", style="Sec.TLabel").pack(side="left", padx=(0, 8))
        self.btn_add = ttk.Button(tb, text="+ Adicionar", command=self._add_files)
        self.btn_dir = ttk.Button(tb, text="Pasta", command=self._add_folder)
        self.btn_clr = ttk.Button(tb, text="Limpar", style="Danger.TButton", command=self._clear)
        for b in (self.btn_add, self.btn_dir, self.btn_clr): b.pack(side="left", padx=3)

        ttk.Label(self, text="Arraste e solte arquivos .LAZ ou .LAS (versão 1.4) aqui"
                  if DND_AVAILABLE else "Use os botões para adicionar arquivos .LAZ ou .LAS (versão 1.4)",
                  style="Hint.TLabel").pack(anchor="w", pady=(12, 4))
        self.flist = FileList(self, (".las", ".laz"))
        self.flist.pack(fill="both", expand=True)

        opt = card(self); opt.pack(fill="x", pady=12)
        r = ttk.Frame(opt, style="Card.TFrame"); r.pack(fill="x")
        ttk.Label(r, text="DESTINO:", style="CardSec.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(r, textvariable=self.dest, state="readonly").pack(side="left", fill="x", expand=True)
        self.btn_dest = ttk.Button(r, text="Selecionar pasta", command=self._pick_dest)
        self.btn_dest.pack(side="left", padx=8)
        self.chk = ttk.Checkbutton(r, text="Adicionar sufixo _v12 ao nome do arquivo",
                                   variable=self.suffix, style="Card.TCheckbutton")
        self.chk.pack(side="left")

        self.stats = Stats(self, [("total", "Total na fila", False), ("ok", "Convertidos", True),
                                  ("err", "Erros", False)])
        self.stats.pack(fill="x")

        ttk.Label(self, text="Log de operações", style="Hint.TLabel").pack(anchor="w", pady=(10, 4))
        lf, self.log = make_log(self, 5); lf.pack(fill="x")

        self.pb = ttk.Progressbar(self, mode="determinate")
        bottom = ttk.Frame(self); bottom.pack(fill="x", pady=(8, 0))
        self.btn_rem = ttk.Button(bottom, text="Remover selecionados", command=self._remove_sel)
        self.btn_run = ttk.Button(bottom, text="▶  Converter para 1.2", style="Primary.TButton",
                                  command=self._run)
        if not LASPY_AVAILABLE:
            self.btn_run.configure(state="disabled")
        self.btn_rem.pack(side="left"); self.btn_run.pack(side="right")
        self.pb_pack = lambda: self.pb.pack(fill="x", before=bottom)

    def _add_files(self):
        ps = filedialog.askopenfilenames(title="Selecionar arquivos",
                                         filetypes=[("Nuvem de pontos", "*.las *.laz *.LAS *.LAZ")])
        self._add(ps)

    def _add_folder(self):
        f = filedialog.askdirectory(title="Selecionar pasta")
        if f: self._add([f])

    def _add(self, paths):
        n = self.flist.add_paths(paths)
        if n:
            self.stats.set("total", len(self.flist.paths))
            self.status(f"{n} arquivo(s) adicionado(s). Total: {len(self.flist.paths)}.")

    def _remove_sel(self):
        n = self.flist.remove_selected(); self.stats.set("total", len(self.flist.paths))
        self.status(f"{n} item(ns) removido(s).")

    def _clear(self):
        self.flist.clear(); log_clear(self.log); self.stats.reset(); self.status("Lista limpa.")

    def _pick_dest(self):
        f = filedialog.askdirectory(title="Selecionar pasta de destino")
        if f:
            self.dest.set(f); self.status(f"Destino: {f}")

    def _run(self):
        if not LASPY_AVAILABLE:
            messagebox.showerror("Dependência ausente",
                                 "Instale a biblioteca laspy para usar esta aba:\npip install laspy[lazrs]")
            return
        if not self.flist.paths:
            messagebox.showinfo("Nada a fazer", "Adicione arquivos LAZ/LAS 1.4."); return
        if not self.dest.get():
            messagebox.showwarning("Atenção", "Selecione a pasta de destino."); return
        if not messagebox.askyesno("Confirmar conversão",
                f"Converter {len(self.flist.paths)} arquivo(s) de LAZ/LAS 1.4 para 1.2?\n\n"
                "Arquivos com point format 6-10 terão o formato ajustado (6→1, 7→3, 8→3, 9→1, 10→3); "
                "NIR e waveform, quando existirem, serão perdidos. Arquivos COPC são aceitos: a "
                "indexação em octree é removida, mas todos os pontos são preservados.\n\n"
                "Classificação: os valores de classe (inclusive acima de 31) são mantidos, gravando "
                "o byte completo, como o Global Mapper e o TerraScan fazem.\n\n"
                "Os arquivos de origem não são modificados."):
            return
        self.ok = self.err = 0; self.stats.reset("ok", "err"); log_clear(self.log)
        self.pb.configure(maximum=len(self.flist.paths), value=0); self.pb_pack()
        self._busy(True)
        self.runner.start(work_downgrade, list(self.flist.paths), self.dest.get(), self.suffix.get())

    def _on_event(self, kind, *a):
        if kind == "progress":
            self.pb.configure(value=a[0])
        elif kind == "file":
            name, ok, msg = a
            if ok:
                self.ok += 1; self.stats.set("ok", self.ok); tag, prefix = "ok", "✔"
            else:
                self.err += 1; self.stats.set("err", self.err); tag, prefix = "err", "✖"
            log_add(self.log, f"{prefix}  {name}  —  {msg}", tag)
        elif kind == "done":
            ok, erros = a
            self._busy(False); self.pb.pack_forget()
            if erros == 0:
                self.status(f"Concluído — {ok} arquivo(s) convertido(s) para 1.2.")
                messagebox.showinfo("Concluído", f"{ok} arquivo(s) convertido(s) para LAZ 1.2.")
            else:
                self.status(f"Concluído com erros — {ok} OK, {erros} erro(s).")
                messagebox.showwarning("Concluído com erros",
                                       f"{ok} convertido(s).\n{erros} com erro (veja o log).")

    def _busy(self, busy):
        set_enabled([self.btn_run, self.btn_add, self.btn_dir, self.btn_clr, self.btn_dest,
                     self.btn_rem, self.chk], not busy)
        self.btn_run.configure(text="Convertendo…" if busy else "▶  Converter para 1.2")


# ══════════════════════════════════════════════════════════════════════════════
#  JANELA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def main():
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    root.title("LAS / LAZ Tools  —  TerraSolid / Spatix")
    root.geometry("1050x760")
    root.minsize(1000, 700)
    setup_style(root)

    header = tk.Frame(root, bg=C["surface"], height=66, highlightthickness=1,
                      highlightbackground=C["border"])
    header.pack(fill="x"); header.pack_propagate(False)
    tk.Label(header, text="LAS / LAZ Tools", bg=C["surface"], fg=C["text"],
             font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=28, pady=(10, 0))
    tk.Label(header, text="Renomeação de arquivos  ·  Edição de projetos PRJ  ·  Copiador de folhas  ·  "
                          ".LAZ→.laz  ·  Conversão de versão",
             bg=C["surface"], fg=C["text3"], font=FONT_S).pack(anchor="w", padx=28)

    status_var = tk.StringVar(value="Pronto.")
    sb = tk.Label(root, textvariable=status_var, anchor="w", bg=C["surface"], fg=C["text3"],
                  font=FONT_S, padx=10, pady=4, highlightthickness=1,
                  highlightbackground=C["border"])
    sb.pack(side="bottom", fill="x")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=0, pady=(8, 0))
    status = status_var.set
    for cls, title in ((RenameTab, "  Renomear Arquivos  "), (PrjEditTab, "  Editar PRJ  "),
                       (PrjCopyTab, "  Copiador PRJ  "), (LowercaseLazTab, "  .LAZ → .laz  "),
                       (ConvertVersionTab, "  Converter LAZ 1.4→1.2  ")):
        nb.add(cls(nb, status), text=title)

    root.mainloop()


if __name__ == "__main__":
    main()