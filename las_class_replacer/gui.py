from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from las_class_replacer.processor import batch_replace_points, relabel_classes, replace_points


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Substituidor LAS/LAZ")
        self.geometry("780x600")
        self.minsize(720, 560)

        self.mode = tk.StringVar(value="replace")
        self.use_boundary = tk.BooleanVar(value=True)
        self.buffer = tk.StringVar(value="20")
        self.target_class = tk.StringVar(value="2")
        self.source_class = tk.StringVar(value="2")
        self.to_class = tk.StringVar(value="1")

        self.target_file = tk.StringVar()
        self.source_file = tk.StringVar()
        self.output_file = tk.StringVar()
        self.target_files: list[Path] = []
        self.source_files: list[Path] = []
        self.target_dir = tk.StringVar()
        self.source_dir = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.boundary = tk.StringVar()

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.input_container: ttk.Frame | None = None
        self._build()
        self._poll_log()

    def _build(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        mode_frame = ttk.LabelFrame(root, text="Modo")
        mode_frame.pack(fill="x")
        ttk.Radiobutton(mode_frame, text="Substituir pontos A por pontos B", variable=self.mode, value="replace", command=self._sync_mode).pack(anchor="w", padx=8, pady=4)
        ttk.Radiobutton(mode_frame, text="Trocar classificacao no arquivo A", variable=self.mode, value="relabel", command=self._sync_mode).pack(anchor="w", padx=8, pady=4)
        ttk.Radiobutton(mode_frame, text="Lote por pastas com arquivos de mesmo nome", variable=self.mode, value="batch", command=self._sync_mode).pack(anchor="w", padx=8, pady=4)

        self.input_container = ttk.Frame(root)
        self.input_container.pack(fill="x", pady=(12, 0))

        self.file_frame = ttk.LabelFrame(self.input_container, text="Arquivos")
        self._row_file(self.file_frame, "Arquivo(s) A", self.target_file, self._pick_target_files, 0)
        self._row_file(self.file_frame, "Arquivo(s) B", self.source_file, self._pick_source_files, 1)
        self._row_file(self.file_frame, "Saida", self.output_file, self._pick_output_path, 2)

        self.dir_frame = ttk.LabelFrame(self.input_container, text="Pastas")
        self._row_dir(self.dir_frame, "Pasta A", self.target_dir, 0)
        self._row_dir(self.dir_frame, "Pasta B", self.source_dir, 1)
        self._row_dir(self.dir_frame, "Pasta de saida", self.output_dir, 2)

        options = ttk.LabelFrame(root, text="Classes e area")
        options.pack(fill="x", pady=(12, 0))
        ttk.Label(options, text="Classe no A").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(options, textvariable=self.target_class, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(options, text="Classe no B").grid(row=0, column=2, sticky="w", padx=8)
        ttk.Entry(options, textvariable=self.source_class, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(options, text="Nova classe").grid(row=0, column=4, sticky="w", padx=8)
        ttk.Entry(options, textvariable=self.to_class, width=8).grid(row=0, column=5, sticky="w")

        ttk.Checkbutton(options, text="Limitar por SHP/DXF", variable=self.use_boundary).grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(options, textvariable=self.boundary).grid(row=1, column=1, columnspan=4, sticky="ew", padx=4)
        ttk.Button(options, text="Escolher", command=self._pick_boundary).grid(row=1, column=5, padx=4)
        ttk.Label(options, text="Buffer").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Combobox(options, textvariable=self.buffer, width=10, values=("0", "5", "10", "20", "30", "50", "75", "100")).grid(row=2, column=1, sticky="w")
        options.columnconfigure(4, weight=1)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=12)
        ttk.Button(actions, text="Processar", command=self._run).pack(side="right")

        self.log = tk.Text(root, height=12, wrap="word")
        self.log.pack(fill="both", expand=True)
        self._sync_mode()

    def _row_file(self, parent, label: str, variable: tk.StringVar, command, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(parent, text="Escolher", command=command).grid(row=row, column=2, padx=4)
        parent.columnconfigure(1, weight=1)

    def _row_dir(self, parent, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=4)
        ttk.Button(parent, text="Escolher", command=lambda: self._pick_dir(variable)).grid(row=row, column=2, padx=4)
        parent.columnconfigure(1, weight=1)

    def _sync_mode(self) -> None:
        if self.mode.get() == "batch":
            self.file_frame.pack_forget()
            self.dir_frame.pack(fill="x")
        else:
            self.dir_frame.pack_forget()
            self.file_frame.pack(fill="x")

    def _pick_target_files(self) -> None:
        self.target_files = self._pick_las_many()
        self.target_file.set(_summarize_files(self.target_files))

    def _pick_source_files(self) -> None:
        self.source_files = self._pick_las_many()
        self.source_file.set(_summarize_files(self.source_files))

    def _pick_las_many(self) -> list[Path]:
        paths = filedialog.askopenfilenames(filetypes=[("LAS/LAZ", "*.las *.laz"), ("Todos", "*.*")])
        return [Path(path) for path in paths]

    def _pick_output_path(self) -> None:
        if len(self.target_files) > 1:
            path = filedialog.askdirectory(title="Escolha a pasta de saida")
        else:
            path = filedialog.asksaveasfilename(defaultextension=".laz", filetypes=[("LAZ", "*.laz"), ("LAS", "*.las")])
        if path:
            self.output_file.set(path)

    def _pick_boundary(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("SHP/DXF", "*.shp *.dxf"), ("Todos", "*.*")])
        if path:
            self.boundary.set(path)
            self.use_boundary.set(True)

    def _pick_dir(self, variable: tk.StringVar) -> None:
        path = filedialog.askdirectory()
        if path:
            variable.set(path)

    def _run(self) -> None:
        try:
            buffer_distance = float(self.buffer.get())
            target_class = int(self.target_class.get())
            source_class = int(self.source_class.get())
            to_class = int(self.to_class.get())
        except ValueError:
            messagebox.showerror("Valores invalidos", "Classes e buffer precisam ser numericos.")
            return

        boundary = self.boundary.get().strip() if self.use_boundary.get() else None
        if self.use_boundary.get() and not boundary:
            messagebox.showerror("Area ausente", "Escolha um arquivo SHP ou DXF, ou desmarque a limitacao por area.")
            return

        thread = threading.Thread(
            target=self._process,
            args=(self.mode.get(), target_class, source_class, to_class, boundary, buffer_distance),
            daemon=True,
        )
        thread.start()

    def _process(self, mode: str, target_class: int, source_class: int, to_class: int, boundary: str | None, buffer_distance: float) -> None:
        try:
            self.log_queue.put("Processando...\n")
            if mode == "replace":
                results = self._replace_selected_files(target_class, source_class, boundary, buffer_distance)
                self.log_queue.put(f"Concluido: {len(results)} arquivo(s) processado(s).\n")
                for result in results:
                    self.log_queue.put(f"{Path(result.output).name}: removidos={result.points_removed}, adicionados={result.source_points_added}, total={result.target_points}\n")
            elif mode == "relabel":
                results = self._relabel_selected_files(target_class, to_class, boundary, buffer_distance)
                self.log_queue.put(f"Concluido: {len(results)} arquivo(s) processado(s).\n")
                for result in results:
                    self.log_queue.put(f"{Path(result.output).name}: alterados={result.points_changed}, total={result.target_points}\n")
            else:
                results = batch_replace_points(
                    self.target_dir.get(),
                    self.source_dir.get(),
                    self.output_dir.get(),
                    target_class,
                    source_class,
                    boundary,
                    buffer_distance,
                )
                self.log_queue.put(f"Concluido: {len(results)} arquivo(s) processado(s).\n")
                for result in results:
                    self.log_queue.put(f"{Path(result.output).name}: removidos={result.points_removed}, adicionados={result.source_points_added}, total={result.target_points}\n")
        except Exception as exc:
            self.log_queue.put(f"ERRO: {exc}\n")

    def _replace_selected_files(self, target_class: int, source_class: int, boundary: str | None, buffer_distance: float):
        targets = self._selected_targets()
        sources = self._selected_sources()
        if not targets:
            raise ValueError("Escolha pelo menos um arquivo A.")
        if not sources:
            raise ValueError("Escolha pelo menos um arquivo B.")

        source_by_name = {path.name.lower(): path for path in sources}
        output_root = self._output_root_for_many(targets)
        results = []

        for target in targets:
            source = sources[0] if len(sources) == 1 and len(targets) == 1 else source_by_name.get(target.name.lower())
            if source is None:
                self.log_queue.put(f"Pulando {target.name}: nao encontrei arquivo B com o mesmo nome.\n")
                continue
            output = self._output_for_target(target, output_root, len(targets))
            results.append(replace_points(target, source, output, target_class, source_class, boundary, buffer_distance))
        return results

    def _relabel_selected_files(self, from_class: int, to_class: int, boundary: str | None, buffer_distance: float):
        targets = self._selected_targets()
        if not targets:
            raise ValueError("Escolha pelo menos um arquivo A.")

        output_root = self._output_root_for_many(targets)
        results = []
        for target in targets:
            output = self._output_for_target(target, output_root, len(targets))
            results.append(relabel_classes(target, output, from_class, to_class, boundary, buffer_distance))
        return results

    def _selected_targets(self) -> list[Path]:
        if self.target_files:
            return self.target_files
        path = self.target_file.get().strip()
        return [Path(path)] if path else []

    def _selected_sources(self) -> list[Path]:
        if self.source_files:
            return self.source_files
        path = self.source_file.get().strip()
        return [Path(path)] if path else []

    def _output_root_for_many(self, targets: list[Path]) -> Path:
        output_text = self.output_file.get().strip()
        if not output_text:
            raise ValueError("Escolha a saida.")
        output = Path(output_text)
        if len(targets) > 1:
            output.mkdir(parents=True, exist_ok=True)
        return output

    def _output_for_target(self, target: Path, output_root: Path, target_count: int) -> Path:
        if target_count == 1 and output_root.suffix.lower() in {".las", ".laz"}:
            return output_root
        return output_root / f"{target.stem}_substituido{target.suffix}"

    def _poll_log(self) -> None:
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log.insert("end", item)
            self.log.see("end")
        self.after(200, self._poll_log)


def main() -> None:
    App().mainloop()


def _summarize_files(paths: list[Path]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        return str(paths[0])
    return f"{len(paths)} arquivos selecionados"


if __name__ == "__main__":
    main()
