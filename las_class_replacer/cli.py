from __future__ import annotations

import argparse
from pathlib import Path

from .processor import batch_replace_points, relabel_classes, replace_points


def main() -> None:
    parser = argparse.ArgumentParser(description="Substitui classes/pontos em arquivos LAS/LAZ.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    relabel = subparsers.add_parser("relabel", help="Troca o valor de classificacao dos pontos.")
    relabel.add_argument("--target", required=True)
    relabel.add_argument("--output", required=True)
    relabel.add_argument("--from-class", required=True, type=int)
    relabel.add_argument("--to-class", required=True, type=int)
    _add_boundary_args(relabel)

    replace = subparsers.add_parser("replace-points", help="Remove pontos do target e adiciona pontos do source.")
    replace.add_argument("--target", required=True)
    replace.add_argument("--source", required=True)
    replace.add_argument("--output", required=True)
    replace.add_argument("--target-class", required=True, type=int)
    replace.add_argument("--source-class", required=True, type=int)
    _add_boundary_args(replace)

    batch = subparsers.add_parser("batch-replace", help="Substitui pontos em lote por arquivos de mesmo nome.")
    batch.add_argument("--target-dir", required=True)
    batch.add_argument("--source-dir", required=True)
    batch.add_argument("--output-dir", required=True)
    batch.add_argument("--target-class", required=True, type=int)
    batch.add_argument("--source-class", required=True, type=int)
    _add_boundary_args(batch)

    args = parser.parse_args()

    if args.command == "relabel":
        result = relabel_classes(
            target=args.target,
            output=args.output,
            from_class=args.from_class,
            to_class=args.to_class,
            boundary=args.boundary,
            buffer_distance=args.buffer,
        )
        print(f"OK: {result.output} | pontos alterados: {result.points_changed}")
    elif args.command == "replace-points":
        result = replace_points(
            target=args.target,
            source=args.source,
            output=args.output,
            target_class=args.target_class,
            source_class=args.source_class,
            boundary=args.boundary,
            buffer_distance=args.buffer,
        )
        print(
            f"OK: {result.output} | removidos do A: {result.points_removed} | "
            f"adicionados do B: {result.source_points_added} | total: {result.target_points}"
        )
    elif args.command == "batch-replace":
        results = batch_replace_points(
            target_dir=args.target_dir,
            source_dir=args.source_dir,
            output_dir=args.output_dir,
            target_class=args.target_class,
            source_class=args.source_class,
            boundary=args.boundary,
            buffer_distance=args.buffer,
        )
        print(f"OK: {len(results)} arquivo(s) processado(s).")
        for result in results:
            print(
                f"{Path(result.output).name}: removidos={result.points_removed}, "
                f"adicionados={result.source_points_added}, total={result.target_points}"
            )


def _add_boundary_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--boundary", help="Arquivo .shp ou .dxf para limitar a operacao.")
    parser.add_argument("--buffer", default=0.0, type=float, help="Buffer em metros/unidades do CRS.")


if __name__ == "__main__":
    main()
