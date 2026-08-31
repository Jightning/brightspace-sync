from brightspace_sync.config import (Config, CourseRule, MapEntry, load_config,
                                     save_config)


def _write_and_reload(cfg: Config, tmp_path):
    path = save_config(cfg, tmp_path / "config.yaml")
    return load_config(str(path))


def test_scalars_and_include_survive(tmp_path):
    cfg = Config()
    cfg.base_url = "https://school.brightspace.com"
    cfg.browser = "firefox"
    cfg.data_dir = tmp_path / "data"
    cfg.include = {"content": True, "grades": False, "news": True,
                   "assignments": False, "calendar": True, "videos": True}
    cfg.max_retries = 7
    cfg.prune = True

    got = _write_and_reload(cfg, tmp_path)
    assert got.base_url == "https://school.brightspace.com"
    assert got.browser == "firefox"
    assert got.includes_section("calendar") and not got.includes_section("grades")
    assert got.max_retries == 7 and got.prune is True


def test_allowlist_and_rename_map_survive(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.courses_mode = "allowlist"
    cfg.allow = [CourseRule(id=111), CourseRule(id=222)]
    cfg.mapping = [MapEntry(name="ECE 270", selectors=[CourseRule(id=111)])]

    got = _write_and_reload(cfg, tmp_path)
    assert got.courses_mode == "allowlist"
    assert {r.id for r in got.allow} == {111, 222}
    assert got.mapping[0].name == "ECE 270"
    assert got.mapping[0].claims(ou_id=111, name="x", code="y")


def test_merge_map_survives(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.mapping = [MapEntry(
        name="ECE 200",
        selectors=[CourseRule(id=1), CourseRule(id=2)],
        subfolders=[(CourseRule(match="ECE 20001"), "Lecture"),
                    (CourseRule(match="ECE 20007"), "Lab")],
    )]

    got = _write_and_reload(cfg, tmp_path)
    entry = got.mapping[0]
    assert entry.name == "ECE 200"
    assert {r.id for r in entry.selectors} == {1, 2}
    assert entry.subfolder_for(ou_id=1, name="ECE 20001 Foo", code="ECE 20001") == "Lecture"
    assert entry.subfolder_for(ou_id=2, name="ECE 20007 Bar", code="ECE 20007") == "Lab"


def test_relative_data_dir_written_when_under_app_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("brightspace_sync.config.app_dir", lambda: tmp_path)
    cfg = Config()
    cfg.data_dir = tmp_path / "bs_data"
    path = save_config(cfg, tmp_path / "config.yaml")
    assert "data_dir: bs_data" in path.read_text()
