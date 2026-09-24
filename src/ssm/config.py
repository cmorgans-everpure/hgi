from functools import lru_cache
import yaml
from . import CONFIG

@lru_cache
def load(name: str) -> dict:
    with open(CONFIG / f"{name}.yaml") as f:
        return yaml.safe_load(f)

def assumptions() -> dict: return load("assumptions")
def trusted() -> dict: return load("trusted_sources")
def column_maps() -> dict: return load("column_maps")
