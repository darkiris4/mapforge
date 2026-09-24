"""User-defined endpoints (stored in data/config/endpoints.json).

This is how controlled services are connected — e.g. NGA GEGD WMS/WMTS or an agency ArcGIS
ImageServer behind PKI.  Paste the service URL you use in your browser/GIS, choose the auth
type, and point at your PEM certificate + key.  Secrets stay in the local config file.
"""
from __future__ import annotations

import re

from .base import Auth, Source
from .services import ArcGISImageServer, TileService

TYPES = {
    "xyz": "XYZ / TMS tiles ({z}/{x}/{y} URL, Web Mercator)",
    "wmts": "WMTS (GetCapabilities URL)",
    "wms": "WMS (GetMap base URL)",
    "arcgis-image": "ArcGIS ImageServer (…/ImageServer)",
    "arcgis-tile": "ArcGIS tiled MapServer (…/MapServer)",
}

PRESETS = [
    {"label": "NGA GEGD — WMTS (PKI)", "type": "wmts", "auth": {"type": "pki"}, "group": "NGA / controlled (PKI)",
     "hint": "Paste the WMTS GetCapabilities URL from your GEGD account (Account → Services / Web Services) "
             "and the layer / profile name."},
    {"label": "NGA GEGD — WMS (PKI)", "type": "wms", "auth": {"type": "pki"}, "group": "NGA / controlled (PKI)",
     "hint": "Paste the WMS base URL from your GEGD account and the layer name."},
    {"label": "Controlled ArcGIS ImageServer (PKI)", "type": "arcgis-image", "auth": {"type": "pki"},
     "group": "NGA / controlled (PKI)", "hint": "URL ending in /ImageServer."},
    {"label": "Commercial XYZ tiles (API key in URL)", "type": "xyz", "auth": {"type": "none"},
     "group": "Custom endpoints", "hint": "e.g. https://host/tiles/{z}/{x}/{y}.jpg?key=… — check the licence allows export."},
]


def endpoint_to_source(ep: dict) -> Source:
    auth = Auth.from_dict(ep.get("auth"))
    common = dict(
        id=f"ep-{re.sub(r'[^a-z0-9]+', '-', ep['id'].lower())}",
        name=ep["name"], url=ep["url"],
        group=ep.get("group") or ("NGA / controlled (PKI)" if auth.type == "pki" else "Custom endpoints"),
        kind=ep.get("kind", "rgb"),
        description=ep.get("description", ""), license=ep.get("license", "Per your agreement with the provider."),
        default_res_m=float(ep.get("default_res_m") or 2.0), auth=auth,
        access="pki" if auth.type != "none" else "public",
    )
    t = ep["type"]
    if t == "arcgis-image":
        return ArcGISImageServer(band_ids=ep.get("band_ids", "0,1,2" if common["kind"] == "rgb" else ""),
                                 min_res_m=float(ep.get("min_res_m") or 0.1), **common)
    if t == "arcgis-tile":
        common["url"] = ep["url"].rstrip("/") + "/tile/{z}/{y}/{x}"
        return TileService(service="xyz", max_zoom=ep.get("max_zoom", 19), **common)
    return TileService(service=t, layer=ep.get("layer", ""), max_zoom=ep.get("max_zoom", 19),
                       image_format=ep.get("format", "image/jpeg"), tile_matrix_set=ep.get("tile_matrix_set", ""),
                       **common)


def custom_sources(settings) -> list[Source]:
    out = []
    for ep in settings.load_json("endpoints.json", []):
        try:
            out.append(endpoint_to_source(ep))
        except Exception:
            continue
    return out
