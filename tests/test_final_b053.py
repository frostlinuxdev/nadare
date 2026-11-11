from nadare.core.config import Config


def test_save_does_not_replace_an_unparseable_config_without_keeping_it(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.add_profile("g", backend="wine", title="G")
    cfg.save()

    # A hand edit that is not valid JSON, while this process still holds the config.
    broken = '{"profiles": {"g": {"title": "hand edit"'
    path.write_text(broken, encoding="utf-8")
    cfg.data["language"] = "fr"

    try:
        cfg.save()
    except OSError:
        pass

    kept = path.read_text(encoding="utf-8") == broken or any(
        "hand edit" in p.read_text(encoding="utf-8")
        for p in tmp_path.glob("config.json.corrupt-*")
    )
    assert kept, "the unparseable config was replaced without keeping a copy"
