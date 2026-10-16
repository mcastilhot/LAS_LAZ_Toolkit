from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import laspy
import numpy as np

from .geometry import load_boundary, points_inside_geometry


LAS_SUFFIXES = {".las", ".laz"}


@dataclass(frozen=True)
class OperationResult:
    output: Path
    target_points: int
    source_points_added: int = 0
    points_changed: int = 0
    points_removed: int = 0


def relabel_classes(
    target: str | Path,
    output: str | Path,
    from_class: int,
    to_class: int,
    boundary: str | Path | None = None,
    buffer_distance: float = 0.0,
) -> OperationResult:
    target_path = Path(target)
    output_path = Path(output)
    las = laspy.read(target_path)

    mask = np.asarray(las.classification) == int(from_class)
    if boundary:
        geometry = load_boundary(boundary, buffer_distance)
        mask &= points_inside_geometry(np.asarray(las.x), np.asarray(las.y), geometry)

    changed = int(np.count_nonzero(mask))
    las.classification[mask] = int(to_class)
    _ensure_parent(output_path)
    las.write(output_path)

    return OperationResult(output=output_path, target_points=len(las.points), points_changed=changed)


def replace_points(
    target: str | Path,
    source: str | Path,
    output: str | Path,
    target_class: int,
    source_class: int,
    boundary: str | Path | None = None,
    buffer_distance: float = 0.0,
) -> OperationResult:
    target_path = Path(target)
    source_path = Path(source)
    output_path = Path(output)

    target_las = laspy.read(target_path)
    source_las = laspy.read(source_path)
    _validate_compatible(target_las, source_las)

    target_mask = np.asarray(target_las.classification) == int(target_class)
    source_mask = np.asarray(source_las.classification) == int(source_class)

    if boundary:
        geometry = load_boundary(boundary, buffer_distance)
        target_mask &= points_inside_geometry(np.asarray(target_las.x), np.asarray(target_las.y), geometry)
        source_mask &= points_inside_geometry(np.asarray(source_las.x), np.asarray(source_las.y), geometry)

    keep_target = ~target_mask
    removed = int(np.count_nonzero(target_mask))
    added = int(np.count_nonzero(source_mask))

    output_las = laspy.LasData(target_las.header)
    output_las.points = _merge_points(target_las.points[keep_target], source_las.points[source_mask])
    output_las.update_header()

    _ensure_parent(output_path)
    output_las.write(output_path)

    return OperationResult(
        output=output_path,
        target_points=len(output_las.points),
        source_points_added=added,
        points_removed=removed,
    )


def batch_replace_points(
    target_dir: str | Path,
    source_dir: str | Path,
    output_dir: str | Path,
    target_class: int,
    source_class: int,
    boundary: str | Path | None = None,
    buffer_distance: float = 0.0,
) -> list[OperationResult]:
    target_root = Path(target_dir)
    source_root = Path(source_dir)
    output_root = Path(output_dir)
    _ensure_parent(output_root / "placeholder.txt")

    results: list[OperationResult] = []
    for target_file in _iter_las_files(target_root):
        source_file = _matching_source(target_file, target_root, source_root)
        if source_file is None:
            continue
        relative = target_file.relative_to(target_root)
        output_file = output_root / relative
        results.append(
            replace_points(
                target=target_file,
                source=source_file,
                output=output_file,
                target_class=target_class,
                source_class=source_class,
                boundary=boundary,
                buffer_distance=buffer_distance,
            )
        )
    return results


def _iter_las_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in LAS_SUFFIXES:
            yield path


def _matching_source(target_file: Path, target_root: Path, source_root: Path) -> Path | None:
    relative = target_file.relative_to(target_root)
    exact = source_root / relative
    if exact.exists():
        return exact

    for suffix in LAS_SUFFIXES:
        candidate = (source_root / relative).with_suffix(suffix)
        if candidate.exists():
            return candidate
    return None


def _merge_points(first, second):
    if len(second) == 0:
        return first.copy()
    if len(first) == 0:
        return second.copy()
    data = np.concatenate((first.array, second.array))
    return laspy.ScaleAwarePointRecord(data, first.point_format, first.scales, first.offsets)


def _validate_compatible(target_las, source_las) -> None:
    if target_las.header.point_format.id != source_las.header.point_format.id:
        raise ValueError(
            "Os arquivos LAS/LAZ precisam ter o mesmo point format para substituir pontos. "
            f"Target={target_las.header.point_format.id}, Source={source_las.header.point_format.id}."
        )
    if list(target_las.point_format.dimension_names) != list(source_las.point_format.dimension_names):
        raise ValueError("Os arquivos LAS/LAZ possuem dimensoes diferentes.")
    if not np.allclose(target_las.header.scales, source_las.header.scales):
        raise ValueError("Os arquivos LAS/LAZ possuem escalas diferentes. Reprojete/normalize antes de substituir pontos.")
    if not np.allclose(target_las.header.offsets, source_las.header.offsets):
        raise ValueError("Os arquivos LAS/LAZ possuem offsets diferentes. Reprojete/normalize antes de substituir pontos.")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
