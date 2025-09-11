import yaml
from pathlib import Path

def test_keywords_yaml_loadable():
    yml = Path("config/keywords.yml").read_text(encoding="utf-8")
    cfg = yaml.safe_load(yml)
    assert isinstance(cfg, dict)
    assert "domains" in cfg and isinstance(cfg["domains"], dict)
    # 1語以上あること
    total = sum(len(v or []) for v in cfg["domains"].values())
    assert total >= 1
