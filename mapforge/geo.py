"""Small geodesy helpers: bounding boxes, ground resolution, output grids."""
from __future__ import annotations

import math
from dataclasses import dataclass

from rasterio.transform import Affine

EARTH_RADIUS_M = 6_378_137.0
METERS_PER_DEG_LAT = 111_320.0
NM_TO_M = 1852.0


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    @classmethod
    def from_any(cls, v) -> "BBox":
        if isinstance(v, BBox):
            return v
        if isinstance(v, dict):
            v = (v["west"], v["south"], v["east"], v["north"])
        w, s, e, n = (float(x) for x in v)
        b = cls(w, s, e, n)
        b.validate()
        return b

    def validate(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("west must be < east, both within -180..180 (antimeridian crossing is not supported)")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("south must be < north, both within -90..90")

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    def intersects(self, other: "BBox | tuple") -> bool:
        o = BBox(*other) if isinstance(other, tuple) else other
        return not (o.east <= self.west or o.west >= self.east or o.north <= self.south or o.south >= self.north)

    def intersection(self, other: "BBox") -> "BBox | None":
        w, s = max(self.west, other.west), max(self.south, other.south)
        e, n = min(self.east, other.east), min(self.north, other.north)
        if w >= e or s >= n:
            return None
        return BBox(w, s, e, n)

    @property
    def center_lat(self) -> float:
        return (self.south + self.north) / 2

    def size_m(self) -> tuple[float, float]:
        w = (self.east - self.west) * METERS_PER_DEG_LAT * math.cos(math.radians(self.center_lat))
        h = (self.north - self.south) * METERS_PER_DEG_LAT
        return w, h


def meters_to_deg(res_m: float, lat: float) -> tuple[float, float]:
    """Ground resolution in metres -> (x_deg, y_deg) pixel size at a latitude."""
    y = res_m / METERS_PER_DEG_LAT
    x = y / max(math.cos(math.radians(lat)), 0.01)
    return x, y


def deg_to_meters(res_deg: float, lat: float = 0.0) -> float:
    return res_deg * METERS_PER_DEG_LAT


@dataclass(frozen=True)
class Grid:
    """An EPSG:4326 output raster grid."""

    bbox: BBox
    width: int
    height: int

    @property
    def xres(self) -> float:
        return (self.bbox.east - self.bbox.west) / self.width

    @property
    def yres(self) -> float:
        return (self.bbox.north - self.bbox.south) / self.height

    @property
    def transform(self) -> Affine:
        return Affine(self.xres, 0, self.bbox.west, 0, -self.yres, self.bbox.north)

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def window_bbox(self, col: int, row: int, w: int, h: int) -> BBox:
        return BBox(
            self.bbox.west + col * self.xres,
            self.bbox.north - (row + h) * self.yres,
            self.bbox.west + (col + w) * self.xres,
            self.bbox.north - row * self.yres,
        )


def grid_for(bbox: BBox, res_m: float) -> Grid:
    """Square-ish ground pixels of res_m metres at the bbox centre latitude."""
    xd, yd = meters_to_deg(res_m, bbox.center_lat)
    w = max(1, round((bbox.east - bbox.west) / xd))
    h = max(1, round((bbox.north - bbox.south) / yd))
    return Grid(bbox, w, h)


def web_mercator_zoom_for(res_m: float, lat: float, tile_size: int = 256) -> int:
    """Smallest zoom level whose ground resolution is at least as fine as res_m."""
    z0 = 2 * math.pi * EARTH_RADIUS_M * math.cos(math.radians(lat)) / tile_size
    return max(0, min(23, math.ceil(math.log2(z0 / max(res_m, 0.01)))))


def web_mercator_res(zoom: int, lat: float, tile_size: int = 256) -> float:
    return 2 * math.pi * EARTH_RADIUS_M * math.cos(math.radians(lat)) / tile_size / (2**zoom)


def interior_depth(xs, ys, fp: BBox):
    """Normalised distance of lon/lat points from the nearest edge of a footprint.

    0 on the edge, 0.5 at the centre, negative outside.  Used to decide which of several
    overlapping charts "owns" a pixel: the chart the pixel sits deepest inside wins, which
    pushes chart collars and legends out of mosaics without needing per-chart neatlines.
    """
    import numpy as np

    dx = np.minimum(xs - fp.west, fp.east - xs) / (fp.east - fp.west)
    dy = np.minimum(ys - fp.south, fp.north - ys) / (fp.north - fp.south)
    return np.minimum(dx, dy)
