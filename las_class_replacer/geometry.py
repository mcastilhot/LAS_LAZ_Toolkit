from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep


SUPPORTED_BOUNDARIES = {".shp", ".dxf"}


def load_boundary(path: str | Path, buffer_distance: float = 0.0):
    boundary_path = Path(path)
    suffix = boundary_path.suffix.lower()
    if suffix not in SUPPORTED_BOUNDARIES:
        raise ValueError(f"Formato de area nao suportado: {suffix}. Use .shp ou .dxf.")

    if suffix == ".shp":
        geometry = _load_shp(boundary_path)
    else:
        geometry = _load_dxf(boundary_path)

    if geometry.is_empty:
        raise ValueError(f"Nenhuma geometria valida encontrada em {boundary_path}.")

    if buffer_distance:
        geometry = geometry.buffer(float(buffer_distance))

    return geometry


def points_inside_geometry(x: np.ndarray, y: np.ndarray, geometry, chunk_size: int = 250_000) -> np.ndarray:
    prepared = prep(geometry)
    result = np.zeros(x.shape[0], dtype=bool)
    minx, miny, maxx, maxy = geometry.bounds

    for start in range(0, x.shape[0], chunk_size):
        end = min(start + chunk_size, x.shape[0])
        xs = x[start:end]
        ys = y[start:end]
        bbox_mask = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
        if not np.any(bbox_mask):
            continue

        local_indexes = np.flatnonzero(bbox_mask)
        absolute_indexes = start + local_indexes
        result[absolute_indexes] = [
            prepared.covers(Point(float(xs[i]), float(ys[i]))) for i in local_indexes
        ]

    return result


def _load_shp(path: Path):
    frame = gpd.read_file(path)
    geometries = [geom for geom in frame.geometry if geom is not None and not geom.is_empty]
    if not geometries:
        raise ValueError(f"O arquivo SHP nao possui geometrias validas: {path}")
    return unary_union(geometries)


def _load_dxf(path: Path):
    import ezdxf

    doc = ezdxf.readfile(path)
    modelspace = doc.modelspace()
    geometries = []

    for entity in modelspace:
        geometry = _entity_to_geometry(entity)
        if geometry is not None and not geometry.is_empty:
            geometries.append(geometry)

    if not geometries:
        raise ValueError(f"O arquivo DXF nao possui entidades geometricas suportadas: {path}")

    return unary_union(geometries)


def _entity_to_geometry(entity):
    entity_type = entity.dxftype()

    if entity_type == "LWPOLYLINE":
        points = [(float(p[0]), float(p[1])) for p in entity.get_points()]
        return _line_or_polygon(points, bool(entity.closed))

    if entity_type == "POLYLINE":
        points = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
        return _line_or_polygon(points, bool(entity.is_closed))

    if entity_type == "LINE":
        start = entity.dxf.start
        end = entity.dxf.end
        return LineString([(float(start.x), float(start.y)), (float(end.x), float(end.y))])

    if entity_type == "CIRCLE":
        center = entity.dxf.center
        return Point(float(center.x), float(center.y)).buffer(float(entity.dxf.radius))

    if entity_type == "ARC":
        center = entity.dxf.center
        radius = float(entity.dxf.radius)
        start = np.deg2rad(float(entity.dxf.start_angle))
        end = np.deg2rad(float(entity.dxf.end_angle))
        if end < start:
            end += 2 * np.pi
        angles = np.linspace(start, end, 96)
        return LineString(
            [(float(center.x) + radius * np.cos(a), float(center.y) + radius * np.sin(a)) for a in angles]
        )

    if entity_type == "HATCH":
        polygons = []
        for path in entity.paths:
            vertices = getattr(path, "vertices", None)
            if vertices:
                points = [(float(v[0]), float(v[1])) for v in vertices]
                if len(points) >= 3:
                    polygons.append(Polygon(points))
        if polygons:
            return unary_union(polygons)

    return None


def _line_or_polygon(points: list[tuple[float, float]], closed: bool):
    if len(points) < 2:
        return None
    if closed and len(points) >= 3:
        return Polygon(points)
    return LineString(points)
