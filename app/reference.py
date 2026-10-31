"""Reference data: a small UK gazetteer and a tick species catalogue.

The upstream API only gives a city name per sighting. To answer "sightings per
region" and "sightings near me" we enrich each city with coordinates, an ONS
region and a nation. The gazetteer also drives location normalisation: case,
whitespace, common aliases and small typos all resolve to one canonical name.

Coordinates are approximate city centres (good enough for city-level mapping).
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Place:
    name: str
    region: str
    nation: str
    lat: float
    lon: float


_PLACES: list[Place] = [
    # The 14 cities present in the Elanco dataset
    Place("Birmingham", "West Midlands", "England", 52.4862, -1.8904),
    Place("Bristol", "South West", "England", 51.4545, -2.5879),
    Place("Cardiff", "Wales", "Wales", 51.4816, -3.1791),
    Place("Edinburgh", "Scotland", "Scotland", 55.9533, -3.1883),
    Place("Glasgow", "Scotland", "Scotland", 55.8642, -4.2518),
    Place("Leeds", "Yorkshire and The Humber", "England", 53.8008, -1.5491),
    Place("Leicester", "East Midlands", "England", 52.6369, -1.1398),
    Place("Liverpool", "North West", "England", 53.4084, -2.9916),
    Place("London", "London", "England", 51.5072, -0.1276),
    Place("Manchester", "North West", "England", 53.4808, -2.2426),
    Place("Newcastle", "North East", "England", 54.9783, -1.6178),
    Place("Nottingham", "East Midlands", "England", 52.9548, -1.1581),
    Place("Sheffield", "Yorkshire and The Humber", "England", 53.3811, -1.4701),
    Place("Southampton", "South East", "England", 50.9097, -1.4044),
    # Extra UK cities so new upstream locations are still mapped to a region
    Place("Aberdeen", "Scotland", "Scotland", 57.1497, -2.0943),
    Place("Bath", "South West", "England", 51.3811, -2.3590),
    Place("Belfast", "Northern Ireland", "Northern Ireland", 54.5973, -5.9301),
    Place("Bournemouth", "South West", "England", 50.7192, -1.8808),
    Place("Brighton", "South East", "England", 50.8225, -0.1372),
    Place("Cambridge", "East of England", "England", 52.2053, 0.1218),
    Place("Coventry", "West Midlands", "England", 52.4068, -1.5197),
    Place("Derby", "East Midlands", "England", 52.9225, -1.4746),
    Place("Dundee", "Scotland", "Scotland", 56.4620, -2.9707),
    Place("Exeter", "South West", "England", 50.7184, -3.5339),
    Place("Hull", "Yorkshire and The Humber", "England", 53.7676, -0.3274),
    Place("Inverness", "Scotland", "Scotland", 57.4778, -4.2247),
    Place("Milton Keynes", "South East", "England", 52.0406, -0.7594),
    Place("Norwich", "East of England", "England", 52.6309, 1.2974),
    Place("Oxford", "South East", "England", 51.7520, -1.2577),
    Place("Plymouth", "South West", "England", 50.3755, -4.1427),
    Place("Portsmouth", "South East", "England", 50.8198, -1.0880),
    Place("Reading", "South East", "England", 51.4543, -0.9781),
    Place("Stoke-on-Trent", "West Midlands", "England", 53.0027, -2.1794),
    Place("Swansea", "Wales", "Wales", 51.6214, -3.9436),
    Place("York", "Yorkshire and The Humber", "England", 53.9600, -1.0873),
]

PLACES: dict[str, Place] = {p.name: p for p in _PLACES}

# The cities the Elanco API is known to serve (used to plan partitioned fetches).
DATASET_CITIES: tuple[str, ...] = (
    "Birmingham",
    "Bristol",
    "Cardiff",
    "Edinburgh",
    "Glasgow",
    "Leeds",
    "Leicester",
    "Liverpool",
    "London",
    "Manchester",
    "Newcastle",
    "Nottingham",
    "Sheffield",
    "Southampton",
)

_ALIASES: dict[str, str] = {
    "greater london": "London",
    "city of london": "London",
    "london city": "London",
    "newcastle upon tyne": "Newcastle",
    "newcastle-upon-tyne": "Newcastle",
    "kingston upon hull": "Hull",
    "stoke": "Stoke-on-Trent",
    "stoke on trent": "Stoke-on-Trent",
    "brighton and hove": "Brighton",
    "caerdydd": "Cardiff",
    "abertawe": "Swansea",
    "glesga": "Glasgow",
    "edinburgh city": "Edinburgh",
    "city of edinburgh": "Edinburgh",
    "manchester city centre": "Manchester",
}

UNMAPPED_REGION = "Unmapped"
UNMAPPED_NATION = "Unknown"


def _key(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[’'`.]", "", text)
    text = re.sub(r"[\s_]+", " ", text)
    return text


_BY_KEY: dict[str, str] = {_key(name): name for name in PLACES}
_BY_KEY.update({_key(alias): name for alias, name in _ALIASES.items()})


@dataclass(frozen=True)
class LocationMatch:
    name: str
    place: Place | None
    method: str  # exact | alias | fuzzy | unmapped


@lru_cache(maxsize=4096)
def resolve_location(raw: str, fuzzy_cutoff: float = 0.84) -> LocationMatch:
    """Map a free-text location to a canonical place.

    Order: exact (case/space-insensitive) -> alias -> fuzzy (typos) -> unmapped.
    Unmapped values are kept (title-cased) rather than discarded: a sighting in an
    unknown town is still a sighting, it just can't be placed on the map.
    """
    key = _key(raw)
    if key in _BY_KEY:
        name = _BY_KEY[key]
        method = "exact" if _key(name) == key else "alias"
        return LocationMatch(name, PLACES[name], method)
    close = difflib.get_close_matches(key, list(_BY_KEY), n=1, cutoff=fuzzy_cutoff)
    if close:
        name = _BY_KEY[close[0]]
        return LocationMatch(name, PLACES[name], "fuzzy")
    pretty = " ".join(w.capitalize() for w in key.split(" ")) if key else raw.strip()
    return LocationMatch(pretty, None, "unmapped")


def suggest_locations(raw: str, candidates: list[str], n: int = 3) -> list[str]:
    lookup = {_key(c): c for c in candidates}
    return [lookup[k] for k in difflib.get_close_matches(_key(raw), list(lookup), n=n, cutoff=0.5)]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def places_within(lat: float, lon: float, radius_km: float) -> list[tuple[Place, float]]:
    hits = [(p, haversine_km(lat, lon, p.lat, p.lon)) for p in PLACES.values()]
    return sorted([h for h in hits if h[1] <= radius_km], key=lambda h: h[1])


# ---------------------------------------------------------------------------
# Species
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Species:
    common_name: str
    latin_name: str
    in_dataset: bool = True


_SPECIES: list[Species] = [
    # Present in the Elanco dataset
    Species("Fox/badger tick", "Ixodes canisuga"),
    Species("Marsh tick", "Ixodes apronophorus"),
    Species("Passerine tick", "Dermacentor frontalis"),
    Species("Southern rodent tick", "Ixodes acuminatus"),
    Species("Tree-hole tick", "Ixodes arboricola"),
    # Other UK species, so future data normalises cleanly
    Species("Sheep tick", "Ixodes ricinus", in_dataset=False),
    Species("Hedgehog tick", "Ixodes hexagonus", in_dataset=False),
    Species("Ornate dog tick", "Dermacentor reticulatus", in_dataset=False),
    Species("Red sheep tick", "Haemaphysalis punctata", in_dataset=False),
    Species("Brown dog tick", "Rhipicephalus sanguineus", in_dataset=False),
]

UNIDENTIFIED_SPECIES = "Unidentified"


def _skey(text: str) -> str:
    return re.sub(r"\s+", " ", _key(text).replace("/", " ").replace("-", " ")).strip()


_SPECIES_ALIASES = {
    "fox tick": "Fox/badger tick",
    "badger tick": "Fox/badger tick",
    "castor bean tick": "Sheep tick",
    "deer tick": "Sheep tick",
}
_SPECIES_BY_COMMON: dict[str, Species] = {_skey(s.common_name): s for s in _SPECIES}
for _alias, _common in _SPECIES_ALIASES.items():
    _SPECIES_BY_COMMON[_skey(_alias)] = next(s for s in _SPECIES if s.common_name == _common)
_SPECIES_BY_LATIN: dict[str, Species] = {_skey(s.latin_name): s for s in _SPECIES}


def resolve_species(common: str | None, latin: str | None) -> tuple[Species | None, list[str]]:
    species, flags = _resolve_species_cached(common, latin)
    return species, list(flags)


@lru_cache(maxsize=4096)
def _resolve_species_cached(common: str | None, latin: str | None) -> tuple[Species | None, tuple[str, ...]]:
    species, flags = _resolve_species(common, latin)
    return species, tuple(flags)


def _resolve_species(common: str | None, latin: str | None) -> tuple[Species | None, list[str]]:
    """Resolve a species from its common and/or Latin name.

    Returns the species (or None if neither name is usable) plus quality flags.
    When the two names disagree, the Latin (scientific) name wins because it is
    the less ambiguous identifier; the record is flagged so it can be reviewed.
    """
    flags: list[str] = []
    by_common = _match_species(common, _SPECIES_BY_COMMON) if common else None
    by_latin = _match_species(latin, _SPECIES_BY_LATIN) if latin else None

    if by_common and by_latin:
        if by_common != by_latin:
            flags.append("species_conflict_latin_preferred")
        return by_latin, flags
    if by_latin:
        if not common:
            flags.append("species_inferred_from_latin")
        else:
            flags.append("species_common_name_unrecognised")
        return by_latin, flags
    if by_common:
        if not latin:
            flags.append("latin_inferred_from_species")
        else:
            flags.append("latin_name_unrecognised")
        return by_common, flags
    if common or latin:
        # Unknown species: keep the names we were given rather than dropping the sighting.
        flags.append("species_unrecognised")
        return Species(common or latin or UNIDENTIFIED_SPECIES, latin or "", in_dataset=False), flags
    flags.append("species_missing")
    return None, flags


def _match_species(name: str, table: dict[str, Species]) -> Species | None:
    k = _skey(name)
    if k in table:
        return table[k]
    close = difflib.get_close_matches(k, list(table), n=1, cutoff=0.86)
    return table[close[0]] if close else None


def species_catalogue() -> list[Species]:
    return list(_SPECIES)


def lookup_species(query: str) -> Species | None:
    """Find a catalogued species by common or Latin name (case-insensitive, typo-tolerant)."""
    return _match_species(query, _SPECIES_BY_COMMON) or _match_species(query, _SPECIES_BY_LATIN)


# A general UK tick-activity profile used ONLY when the data itself shows no
# significant seasonality. UK guidance describes ticks as most active from spring
# to autumn, peaking in late spring/early summer with a smaller autumn rise.
# These relative weights are an illustrative, configurable prior - not measured data.
SEASONAL_PRIOR: dict[int, float] = {
    1: 0.35,
    2: 0.45,
    3: 0.85,
    4: 1.45,
    5: 1.75,
    6: 1.6,
    7: 1.25,
    8: 1.1,
    9: 1.15,
    10: 0.95,
    11: 0.6,
    12: 0.4,
}
_mean = sum(SEASONAL_PRIOR.values()) / 12
SEASONAL_PRIOR = {m: v / _mean for m, v in SEASONAL_PRIOR.items()}
