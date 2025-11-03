"""Utilities for downloading daily Keirin race cards.

The module primarily targets the public Rakuten K-Dreams JSON feed, but it also
ships with an offline fallback dataset so predictions can be generated in
restricted environments where the remote service is not reachable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Iterator, List, Mapping, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Iterator, List, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from keirin_models import DEFAULT_LEG_TYPES, Rider

LOGGER = logging.getLogger(__name__)


class DataSource:
    """Supported providers for race-card data."""

    AUTO = "auto"
    RAKUTEN = "rakuten"
    KEIRIN_JP = "keirin_jp"
    LOCAL = "local"


class ProxyMode:
    """Proxy strategies supported by the downloader."""

    SYSTEM = "system"
    NONE = "none"


RAKUTEN_API_URLS = (
    LOCAL = "local"


API_URLS = (
    "https://keirin.rakuten.co.jp/keirinapi/sp/racecard/list",
    "https://keirin.rakuten.co.jp/keirinapi/racecard/list",
    "https://keirin.rakuten.co.jp/keirinapi/sp/racecard",
)
"""Candidate endpoints for the Rakuten K-Dreams race card feed."""

USER_AGENT = "keirin-predictor/1.2 (+https://github.com/openai)"
"""User agent string so the upstream service can identify the client."""


KEIRIN_JP_API_URLS = (
    "https://keirin.jp/pc/dfw/dataplaza/api/racecard/list",
    "https://keirin.jp/pc/dfw/dataplaza/api/racecard",
    "https://keirin.jp/pc/dfw/dataplaza/api",
)
"""Candidate endpoints for the official keirin.jp race-card feed."""


DATA_PACKAGE = "data"
LOCAL_DATA_FILENAME = "local_racecards.json"
LOCAL_DATA_FALLBACK = Path(__file__).resolve().parent / "data" / LOCAL_DATA_FILENAME
LOCAL_DATA_FILE = Path(__file__).resolve().parent / "data" / "local_racecards.json"
"""Bundled offline dataset used when live fetching fails."""


@dataclass(frozen=True)
class RaceCard:
    """Structured representation of a single race card."""

    race_id: str
    venue_name: str
    race_number: int
    race_title: str
    riders: List[Rider]


@dataclass(frozen=True)
class FetchOutcome:
    """Result of a race-card download attempt."""

    race_cards: List[RaceCard]
    provider: str
    proxy_mode: str
    errors: Tuple[str, ...]


def fetch_racecards(
    target_date: date,
    *,
    timeout: float = 20.0,
    source: str = DataSource.AUTO,
    proxy_mode: str = ProxyMode.SYSTEM,
) -> FetchOutcome:
) -> List[RaceCard]:
    """Download and parse keirin race cards for ``target_date``.

    Parameters
    ----------
    target_date:
        Date to request from the Rakuten K-Dreams public API.
    timeout:
        Socket timeout in seconds for the HTTP request.
    source:
        Data provider selection.  ``"rakuten"`` forces live downloads, while
        ``"local"`` loads the bundled offline dataset.  The default ``"auto"``
        mode first attempts the live endpoint and falls back to the local copy
        when the network request fails.
    proxy_mode:
        How the HTTP client should resolve proxies. ``"system"`` honours the
        current environment's proxy variables, whereas ``"none"`` disables
        proxies entirely which can help when corporate gateways block the
        Rakuten API.
    Returns
    -------
    FetchOutcome
        The parsed race cards together with metadata describing which
        provider supplied the data.  When ``source`` is ``AUTO`` the
        function records any errors from earlier live attempts in the
        :class:`FetchOutcome` so callers can surface fallbacks to users.
    """

    valid_sources = {
        DataSource.AUTO,
        DataSource.RAKUTEN,
        DataSource.KEIRIN_JP,
        DataSource.LOCAL,
    }
    if source not in valid_sources:
        raise ValueError(f"Unknown data source: {source}")

    if proxy_mode not in {ProxyMode.SYSTEM, ProxyMode.NONE}:
        raise ValueError(f"Unknown proxy mode: {proxy_mode}")

    errors: List[str] = []

    if source in {DataSource.AUTO, DataSource.RAKUTEN, DataSource.KEIRIN_JP}:
        live_attempts = _plan_live_attempts(source, proxy_mode)

        for provider, attempt_proxy in live_attempts:
            try:
                payload = _download_live_payload(
                    provider,
                    target_date,
                    timeout=timeout,
                    proxy_mode=attempt_proxy,
                )
            except Exception as exc:  # pragma: no cover - network dependent
                summary = _summarize_error(exc)
                errors.append(f"{provider} via {attempt_proxy}: {summary}")
                continue

            try:
                race_cards = list(_parse_racecards(payload))
                return FetchOutcome(
                    race_cards=race_cards,
                    provider=provider,
                    proxy_mode=attempt_proxy,
                    errors=tuple(errors),
                )
            except Exception as exc:  # pragma: no cover - payload dependent
                summary = _summarize_error(exc)
                errors.append(f"{provider} parsing error: {summary}")

        if source in {DataSource.RAKUTEN, DataSource.KEIRIN_JP}:
            raise RuntimeError(
                "; ".join(errors) if errors else "Live data source produced no races"
            )

        if errors:
            LOGGER.warning(
                "Live data sources failed (%s); attempting offline dataset",
                "; ".join(errors),
            )

    if source in {DataSource.AUTO, DataSource.LOCAL}:
        payload = _load_local_payload(target_date)
        race_cards = list(_parse_racecards(payload))
        return FetchOutcome(
            race_cards=race_cards,
            provider=DataSource.LOCAL,
            proxy_mode=ProxyMode.NONE,
            errors=tuple(errors),
        )
    """

    if source not in {DataSource.AUTO, DataSource.RAKUTEN, DataSource.LOCAL}:
        raise ValueError(f"Unknown data source: {source}")

    if source in {DataSource.AUTO, DataSource.RAKUTEN}:
        try:
            payload = _download_payload(target_date, timeout=timeout)
            return list(_parse_racecards(payload))
        except Exception as exc:  # pragma: no cover - network dependent
            if source == DataSource.RAKUTEN:
                raise
            LOGGER.warning("Live fetch failed (%s); attempting offline dataset", exc)

    if source in {DataSource.AUTO, DataSource.LOCAL}:
        payload = _load_local_payload(target_date)
        return list(_parse_racecards(payload))

    raise RuntimeError("No race cards available for the requested source")


def _plan_live_attempts(source: str, proxy_mode: str) -> List[Tuple[str, str]]:
    """Return ordered provider/proxy combinations to try for live fetching."""

    attempts: List[Tuple[str, str]] = []
    preferred_proxies = [proxy_mode]
    alternate_proxy = ProxyMode.NONE if proxy_mode != ProxyMode.NONE else ProxyMode.SYSTEM
    if alternate_proxy not in preferred_proxies:
        preferred_proxies.append(alternate_proxy)

    if source == DataSource.AUTO:
        providers = [DataSource.KEIRIN_JP, DataSource.RAKUTEN]
    else:
        providers = [source]

    for provider in providers:
        for proxy in preferred_proxies:
            attempts.append((provider, proxy))

    seen: set[Tuple[str, str]] = set()
    ordered: List[Tuple[str, str]] = []
    for attempt in attempts:
        if attempt in seen:
            continue
        seen.add(attempt)
        ordered.append(attempt)
    return ordered


def _summarize_error(exc: Exception) -> str:
    """Return a compact textual representation of ``exc`` suitable for logs."""

    if isinstance(exc, HTTPError):
        parts = ["HTTP", str(exc.code)]
        if exc.reason:
            parts.append(str(exc.reason))
        return " ".join(parts)

    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, OSError):
            return f"{reason.__class__.__name__}: {reason}"
        return str(reason)

    text = str(exc)
    if len(text) > 200:
        return text[:197] + "..."
    return text


def _download_live_payload(
    provider: str,
    target_date: date,
    *,
    timeout: float,
    proxy_mode: str,
) -> Any:
    if provider == DataSource.RAKUTEN:
        return _download_rakuten_payload(target_date, timeout=timeout, proxy_mode=proxy_mode)
    if provider == DataSource.KEIRIN_JP:
        return _download_keirin_jp_payload(target_date, timeout=timeout, proxy_mode=proxy_mode)
    raise ValueError(f"Unsupported live provider: {provider}")


def _download_rakuten_payload(target_date: date, *, timeout: float, proxy_mode: str) -> Any:
def _download_payload(target_date: date, *, timeout: float) -> Any:
    """Try each supported Rakuten K-Dreams endpoint until one succeeds."""

    ymd = target_date.strftime("%Y%m%d")
    iso = target_date.isoformat()
    params = [
        ("ymd", ymd),
        ("date", ymd),
        ("targetDate", ymd),
        ("ymd", iso),
        ("date", iso),
    ]

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://keirin.rakuten.co.jp/",
        "Origin": "https://keirin.rakuten.co.jp",
        "Connection": "keep-alive",
    }

    errors: list[str] = []
    opener = build_opener(
        ProxyHandler({}) if proxy_mode == ProxyMode.NONE else ProxyHandler()
    )

    for base_url in RAKUTEN_API_URLS:
    for base_url in API_URLS:
        for key, value in params:
            url = f"{base_url}?{key}={value}"
            request = Request(url, headers=headers)
            try:
                with opener.open(request, timeout=timeout) as response:
                with urlopen(request, timeout=timeout) as response:
                    if response.status != 200:
                        errors.append(f"HTTP {response.status} for {url}")
                        continue
                    body = response.read()
            except HTTPError as exc:  # pragma: no cover - requires network conditions.
                errors.append(f"HTTP {exc.code} {exc.reason} for {url}")
                continue
            except URLError as exc:  # pragma: no cover - requires network conditions.
                errors.append(f"URL error {exc.reason} for {url}")
                continue

            try:
                text = body.decode("utf-8-sig")
                if text.strip().startswith("{"):
                    return json.loads(text)
                # Some variants wrap the JSON in a callback such as ``callback(...)``.
                match = re.search(r"([\[{].*[\]}])", text, flags=re.DOTALL)
                if match:
                    return json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                errors.append(f"JSON decode error for {url}: {exc}")
                continue

    if errors:
        raise RuntimeError(
            "Unable to download race cards from Rakuten K-Dreams (" + "; ".join(errors) + ")"
        )
    raise RuntimeError("Rakuten K-Dreams race card feed returned no data")


def _download_keirin_jp_payload(
    target_date: date,
    *,
    timeout: float,
    proxy_mode: str,
) -> Any:
    """Attempt to download race cards from keirin.jp's public API."""

    ymd = target_date.strftime("%Y%m%d")
    iso = target_date.isoformat()
    params = [
        ("date", ymd),
        ("targetDate", ymd),
        ("date", iso),
        ("targetDate", iso),
        ("ymd", ymd),
    ]

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://keirin.jp/",
        "Origin": "https://keirin.jp",
        "Connection": "keep-alive",
        "X-Requested-With": "XMLHttpRequest",
    }

    errors: list[str] = []
    opener = build_opener(
        ProxyHandler({}) if proxy_mode == ProxyMode.NONE else ProxyHandler()
    )

    for base_url in KEIRIN_JP_API_URLS:
        for key, value in params:
            url = f"{base_url}?{key}={value}"
            request = Request(url, headers=headers)
            try:
                with opener.open(request, timeout=timeout) as response:
                    if response.status != 200:
                        errors.append(f"HTTP {response.status} for {url}")
                        continue
                    body = response.read()
            except HTTPError as exc:  # pragma: no cover - requires network conditions.
                errors.append(f"HTTP {exc.code} {exc.reason} for {url}")
                continue
            except URLError as exc:  # pragma: no cover - requires network conditions.
                errors.append(f"URL error {exc.reason} for {url}")
                continue

            try:
                text = body.decode("utf-8-sig")
                if text.strip().startswith("{"):
                    return json.loads(text)
                match = re.search(r"([\[{].*[\]}])", text, flags=re.DOTALL)
                if match:
                    return json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                errors.append(f"JSON decode error for {url}: {exc}")
                continue

    if errors:
        raise RuntimeError(
            "Unable to download race cards from keirin.jp (" + "; ".join(errors) + ")"
        )
    raise RuntimeError("keirin.jp race card feed returned no data")


def _load_local_payload(target_date: date) -> Any:
    dataset: Any | None = None

    try:
        resource_path = resources.files(DATA_PACKAGE).joinpath(LOCAL_DATA_FILENAME)
    except (ModuleNotFoundError, AttributeError):
        resource_path = None

    if resource_path is not None and resource_path.is_file():
        with resource_path.open("r", encoding="utf-8") as handle:
            dataset = json.load(handle)
    elif LOCAL_DATA_FALLBACK.exists():
        with LOCAL_DATA_FALLBACK.open("r", encoding="utf-8") as handle:
            dataset = json.load(handle)
    else:
        raise RuntimeError("Local race-card dataset is missing")

def _load_local_payload(target_date: date) -> Any:
    if not LOCAL_DATA_FILE.exists():
        raise RuntimeError("Local race-card dataset is missing")

    with LOCAL_DATA_FILE.open("r", encoding="utf-8") as handle:
        dataset = json.load(handle)

    key = target_date.isoformat()
    if key in dataset:
        return dataset[key]

    if "days" in dataset and isinstance(dataset["days"], list):
        for entry in dataset["days"]:
            if isinstance(entry, Mapping) and entry.get("date") == key:
                return entry

    raise RuntimeError(f"No offline race-card data for {key}")


def _parse_racecards(payload: Any) -> Iterator[RaceCard]:
    """Yield :class:`RaceCard` objects from a raw API payload."""

    data = _unwrap_payload(payload)
    stadiums = _extract_iterable(
        data,
        [
            "stadiums",
            "venues",
            "place_list",
            "series",
            "items",
            "stadium_list",
            "stadiumInfos",
            "placeInfos",
            "meetingList",
        ],
        description="stadium list",
    )

    for stadium in stadiums:
        stadium_map = _ensure_mapping(stadium, "stadium entry")
        venue_name = str(
            _lookup_value(
                stadium_map,
                preferred_keys=["stadium_name", "place_name", "name", "title"],
                fallback_tokens=["場", "会場", "stadium", "venue"],
                default="Unknown Venue",
            )
        )
        race_list = _extract_iterable(
            stadium_map,
            [
                "races",
                "race_list",
                "items",
                "racecard",
                "raceInfos",
                "raceCards",
                "raceList",
            ],
            description="race list",
            default=[],
        )

        for race in race_list:
            race_map = _ensure_mapping(race, "race entry")
            race_id = str(
                _lookup_value(
                    race_map,
                    preferred_keys=["race_id", "id"],
                    fallback_tokens=["race", "id"],
                    default="",
                )
            )
            race_number_value = _lookup_value(
                race_map,
                preferred_keys=["race_no", "race_number", "number", "no"],
                fallback_tokens=["race", "番", "no"],
                default="0",
            )
            race_number = _to_int(race_number_value, default=0)
            race_title = str(
                _lookup_value(
                    race_map,
                    preferred_keys=["race_name", "name", "title"],
                    fallback_tokens=["race", "名", "title"],
                    default=f"{race_number}R",
                )
            )
            entries = _extract_iterable(
                race_map,
                [
                    "entries",
                    "entry_list",
                    "racers",
                    "players",
                    "competitors",
                    "racerList",
                    "row",
                ],
                description="entry list",
                default=[],
            )

            riders: List[Rider] = []
            for entry in entries:
                entry_map = _ensure_mapping(entry, "entry")
                try:
                    name = str(
                        _lookup_value(
                            entry_map,
                            preferred_keys=[
                                "name",
                                "racer_name",
                                "player_name",
                                "選手名",
                                "racerName",
                                "playerName",
                            ],
                            fallback_tokens=["name", "選手", "racer"],
                        )
                    ).strip()
                    score = _to_float(
                        _lookup_value(
                            entry_map,
                            preferred_keys=[
                                "score",
                                "point",
                                "racer_score",
                                "競走得点",
                                "playerPoint",
                                "racerPoint",
                                "recentPoint",
                                "evaluationPoint",
                            ],
                            fallback_tokens=["score", "point", "得点", "ポイント"],
                        )
                    )
                    upset = _to_float(
                        _lookup_value(
                            entry_map,
                            preferred_keys=[
                                "upset",
                                "variance",
                                "disorder",
                                "波乱度",
                                "roughness",
                                "disturbance",
                                "instability",
                            ],
                            fallback_tokens=["upset", "波乱", "変動", "乱"],
                        )
                    )
                    leg_type = str(
                        _lookup_value(
                            entry_map,
                            preferred_keys=[
                                "leg_type",
                                "style",
                                "脚質",
                                "legType",
                                "runningStyle",
                            ],
                            fallback_tokens=["leg", "style", "脚質", "脚"],
                            default="",
                        )
                    ).strip() or "自在"
                    line_name = str(
                        _lookup_value(
                            entry_map,
                            preferred_keys=[
                                "line",
                                "line_name",
                                "ライン",
                                "lineLabel",
                                "lineName",
                                "line_code",
                            ],
                            fallback_tokens=["line", "ライン"],
                            default="",
                        )
                    ).strip()
                    line_power_value = _lookup_value(
                        entry_map,
                        preferred_keys=[
                            "line_power",
                            "linePower",
                            "ラインパワー",
                            "line_point",
                            "linePoint",
                            "lineStrength",
                        ],
                        fallback_tokens=["power", "パワー", "ライン"],
                        default="",
                    )
                    line_power = None
                    if line_power_value not in (None, ""):
                        try:
                            line_power = _to_float(line_power_value)
                        except ValueError:
                            line_power = None
                except KeyError as exc:
                    LOGGER.debug("Skipping entry due to missing data: %s", exc)
                    continue
                except ValueError as exc:
                    LOGGER.debug("Skipping entry due to invalid numeric value: %s", exc)
                    continue

                if leg_type not in DEFAULT_LEG_TYPES:
                    leg_type = leg_type or "自在"

                riders.append(
                    Rider(
                        name=name,
                        score=score,
                        upset=upset,
                        leg_type=leg_type,
                        line=line_name or None,
                        line_power=line_power,
                    )
                )

            if riders:
                yield RaceCard(
                    race_id=race_id or f"{venue_name}-{race_number}",
                    venue_name=venue_name,
                    race_number=race_number,
                    race_title=race_title,
                    riders=riders,
                )


def _unwrap_payload(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        if "data" in payload and isinstance(payload["data"], Mapping):
            return payload["data"]
        if "body" in payload and isinstance(payload["body"], Mapping):
            body = payload["body"]
            if "data" in body and isinstance(body["data"], Mapping):
                return body["data"]
            return body
        if "response" in payload and isinstance(payload["response"], Mapping):
            return payload["response"]
        return payload
    raise ValueError("API payload must be a mapping with race data")


def _ensure_mapping(obj: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(obj, Mapping):
        raise ValueError(f"Expected mapping for {description}, got {type(obj)!r}")
    return obj


def _extract_iterable(
    mapping: Mapping[str, Any],
    keys: Sequence[str],
    *,
    description: str,
    default: Iterable[Any] | None = None,
) -> Iterable[Any]:
    for key in keys:
        if key in mapping and isinstance(mapping[key], Iterable) and not isinstance(
            mapping[key], (str, bytes)
        ):
            return mapping[key]
    if default is not None:
        return default
    raise ValueError(f"Could not locate {description} in payload")


def _lookup_value(
    mapping: Mapping[str, Any],
    *,
    preferred_keys: Sequence[str],
    fallback_tokens: Sequence[str],
    default: Any | None = None,
) -> Any:
    for key in preferred_keys:
        if key in mapping:
            value = mapping[key]
            if value not in (None, ""):
                return value
    for key, value in mapping.items():
        key_text = str(key)
        if value in (None, ""):
            continue
        for token in fallback_tokens:
            if token and (token in key_text or token in key_text.lower()):
                return value
    if default is not None:
        return default
    raise KeyError(f"Required value missing ({', '.join(preferred_keys)})")


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise ValueError("Empty value")
    normalized = re.sub(r"[^0-9+\-.,]", "", text)
    normalized = normalized.replace(",", "")
    if not normalized:
        raise ValueError(f"Cannot parse float from {text!r}")
    return float(normalized)


def _to_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return default
    return int(digits)


__all__ = [
    "RaceCard",
    "FetchOutcome",
    "fetch_racecards",
    "DataSource",
    "ProxyMode",
]
__all__ = ["RaceCard", "fetch_racecards", "DataSource"]

