from brightspace_sync.manifest import Manifest


def _manifest(tmp_path, **files):
    m = Manifest.load(tmp_path)
    for key, name in files.items():
        p = tmp_path / name
        p.write_text("x", "utf-8")
        m.record(key, last_modified="2026-01-01", local=p)
    m.touched.clear()
    return m


def test_prune_removes_only_untouched_entries_of_a_synced_section(tmp_path):
    m = _manifest(tmp_path, **{"topic:1": "kept.pdf", "topic:2": "gone.pdf"})
    m.touch("topic:1")

    removed = m.prune({"content"}, dry_run=False)

    assert [p.name for p in removed] == ["gone.pdf"]
    assert (tmp_path / "kept.pdf").exists()
    assert "topic:2" not in m.entries


def test_prune_leaves_sections_that_did_not_run_alone(tmp_path):
    m = _manifest(tmp_path, **{"video:1": "lecture.mp4"})

    assert m.prune({"content"}, dry_run=False) == []
    assert (tmp_path / "lecture.mp4").exists()


def test_touch_prefix_keeps_the_images_of_an_unchanged_description(tmp_path):
    m = _manifest(tmp_path, **{"moddesc:7": "_module.html",
                               "moddesc:7:asset:0": "diagram.png"})
    m.touch("moddesc:7")
    m.touch_prefix("moddesc:7:asset:")

    assert m.prune({"content"}, dry_run=False) == []
    assert (tmp_path / "diagram.png").exists()
