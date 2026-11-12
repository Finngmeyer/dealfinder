"""FEMA National Flood Hazard Layer: the flood zone at a point.

Free and keyless. The map service numbers its layers, and the numbers can change, so the
"Flood Hazard Zones" layer is found by name. A point FEMA has not mapped comes back with no
zone, which is "unknown", not "safe".
"""

from __future__ import annotations

import json
from datetime import timedelta

from pydantic import BaseModel

from dealfinder.http import Fetcher, SourceUnavailable

SOURCE = "fema_nfhl"
SERVICE = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
LAYER_NAME = "Flood Hazard Zones"
_LAYER_CACHE = timedelta(days=30)


class FloodZone(BaseModel):
    zone: str | None  # FEMA's code, such as AE or X; None where FEMA has no map
    subtype: str | None = None
    in_flood_zone: bool | None  # a Special Flood Hazard Area, where lenders require insurance
    stale: bool = False

    def describe(self) -> str:
        if self.zone is None:
            return "FEMA has no flood map for this spot"
        kind = "a Special Flood Hazard Area" if self.in_flood_zone else "outside the flood zone"
        subtype = f" ({self.subtype.lower()})" if self.subtype else ""
        return f"FEMA flood map: zone {self.zone}{subtype}, {kind}"


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise SourceUnavailable(SOURCE, "the response was not JSON") from error
    if not isinstance(data, dict):
        raise SourceUnavailable(SOURCE, "the response had an unexpected shape")
    if "error" in data:  # ArcGIS reports errors inside an HTTP 200
        message = data["error"].get("message") if isinstance(data["error"], dict) else None
        raise SourceUnavailable(
            SOURCE, f"the map service reported an error: {message or 'unknown'}"
        )
    return data


def _parse_layer(body: bytes) -> int:
    layers = _json(body).get("layers")
    for layer in layers if isinstance(layers, list) else []:
        if isinstance(layer, dict) and layer.get("name") == LAYER_NAME:
            return int(layer["id"])
    raise SourceUnavailable(SOURCE, f"the map service has no '{LAYER_NAME}' layer")


def parse_zone(body: bytes) -> FloodZone:
    features = _json(body).get("features")
    if not isinstance(features, list):
        raise SourceUnavailable(SOURCE, "the response had no list of features")
    zones = [
        feature["attributes"]
        for feature in features
        if isinstance(feature, dict) and isinstance(feature.get("attributes"), dict)
    ]
    if not zones:
        return FloodZone(zone=None, in_flood_zone=None)
    # Where polygons overlap, the riskier one decides.
    worst = max(zones, key=lambda attributes: attributes.get("SFHA_TF") == "T")
    return FloodZone(
        zone=str(worst.get("FLD_ZONE") or "").strip() or None,
        subtype=str(worst.get("ZONE_SUBTY") or "").strip() or None,
        in_flood_zone=worst.get("SFHA_TF") == "T",
    )


def flood_zone(
    fetcher: Fetcher, *, latitude: float | None, longitude: float | None, max_age: timedelta
) -> FloodZone:
    if latitude is None or longitude is None:
        raise SourceUnavailable(SOURCE, "the listing has no coordinates")
    layers = fetcher.get(
        SOURCE, SERVICE, params={"f": "json"}, max_age=_LAYER_CACHE, validate=_parse_layer
    )
    fetched = fetcher.get(
        SOURCE,
        f"{SERVICE}/{_parse_layer(layers.body)}/query",
        params={
            # Five decimals is about a metre: fine for a building, and stable as a cache key.
            "geometry": f"{longitude:.5f},{latitude:.5f}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
            "returnGeometry": "false",
            "f": "json",
        },
        max_age=max_age,
        validate=parse_zone,
    )
    return parse_zone(fetched.body).model_copy(update={"stale": fetched.stale})
