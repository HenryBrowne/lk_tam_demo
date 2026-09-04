"""Load the illustrative scoring thresholds from ``thresholds.yaml``.

Kept separate from ``scoring.py`` so the scoring functions stay pure (they take a
plain dict) and so the dashboard can show the same numbers and the same banner.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

_YAML_PATH = Path(__file__).with_name("thresholds.yaml")

ILLUSTRATIVE_BANNER = (
    "Thresholds are ILLUSTRATIVE - the scoring framework is the point, not these "
    "numbers. With a real account they would be co-owned with the customer and "
    "revisited every QBR."
)


@functools.lru_cache(maxsize=1)
def load_thresholds() -> dict:
    """Parsed ``thresholds.yaml`` as a nested dict. Cached; call ``load_thresholds.cache_clear()`` after editing."""
    with _YAML_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


if __name__ == "__main__":
    import json

    print(ILLUSTRATIVE_BANNER)
    print(json.dumps(load_thresholds(), indent=2))
