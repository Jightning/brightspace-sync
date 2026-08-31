from brightspace_sync.enrollments import Course
from brightspace_sync.gui.model import CourseChoice, MergeGroup, SetupModel


def _course(cid, code, name="A course"):
    return Course(id=cid, name=name, code=code, is_active=True, can_access=True)


COURSES = [_course(1, "ECE 20001", "Linear Circuits"),
           _course(2, "ECE 20007", "Linear Circuits Lab"),
           _course(3, "ECE 27000", "Digital System Design"),
           _course(4, "CS 18000", "Problem Solving")]


def _model_with_courses(**kw):
    m = SetupModel(**kw)
    m.courses = [CourseChoice.from_course(c) for c in COURSES]
    return m


def test_sync_all_excludes_unchecked_via_ignore():
    m = _model_with_courses(sync_all=True)
    m.courses[3].selected = False           # drop CS 18000
    cfg = m.to_config()
    assert cfg.courses_mode == "all"
    assert 4 in {r.id for r in cfg.ignore}
    assert cfg.wants_course(ou_id=3, name="x", code="y")
    assert not cfg.wants_course(ou_id=4, name="x", code="y")


def test_allowlist_lists_checked_courses():
    m = _model_with_courses(sync_all=False)
    for c in m.courses:
        c.selected = c.id in (1, 3)
    cfg = m.to_config()
    assert cfg.courses_mode == "allowlist"
    assert {r.id for r in cfg.allow} == {1, 3}


def test_rename_becomes_map_entry():
    m = _model_with_courses(sync_all=True)
    m.courses[2].folder = "ECE 270 - DSD"
    cfg = m.to_config()
    hit = [e for e in cfg.mapping if e.claims(ou_id=3, name="x", code="y")]
    assert hit and hit[0].name == "ECE 270 - DSD"


def test_unchanged_folder_makes_no_map_entry():
    m = _model_with_courses(sync_all=True)
    m.courses[2].folder = m.courses[2].default_folder
    assert m.to_config().mapping == []


def test_merge_group_builds_subfolders():
    m = _model_with_courses(sync_all=False)
    for c in m.courses:
        c.selected = c.id in (1, 2)
    m.merges = [MergeGroup(key="g0", folder="ECE 200")]
    m.courses[0].merge_key = m.courses[1].merge_key = "g0"
    m.courses[0].subfolder = "Lecture"
    m.courses[1].subfolder = "Lab"

    cfg = m.to_config()
    merge = [e for e in cfg.mapping if e.name == "ECE 200"][0]
    assert {r.id for r in merge.selectors} == {1, 2}
    assert merge.subfolder_for(ou_id=1, name="Linear Circuits", code="ECE 20001") == "Lecture"
    assert merge.subfolder_for(ou_id=2, name="Linear Circuits Lab", code="ECE 20007") == "Lab"


def test_lonely_merge_group_is_ignored():
    m = _model_with_courses(sync_all=False)
    m.courses[0].selected = True
    m.courses[0].merge_key = "g0"
    m.merges = [MergeGroup(key="g0", folder="Solo")]
    assert m.to_config().mapping == []          # <2 members -> dropped
    assert any("only has one course" in p for p in m.problems())


def test_from_config_recovers_selection_and_merges():
    src = _model_with_courses(sync_all=False)
    for c in src.courses:
        c.selected = c.id in (1, 2, 3)
    src.merges = [MergeGroup(key="g0", folder="ECE 200")]
    src.courses[0].merge_key = src.courses[1].merge_key = "g0"
    src.courses[0].subfolder = "Lecture"
    src.courses[1].subfolder = "Lab"
    cfg = src.to_config()

    back = SetupModel.from_config(cfg, COURSES)
    picked = {c.id for c in back.courses if c.selected}
    assert picked == {1, 2, 3}
    assert len(back.merges) == 1
    merged_ids = {c.id for c in back.courses if c.merge_key}
    assert merged_ids == {1, 2}
    sub = {c.id: c.subfolder for c in back.courses if c.merge_key}
    assert sub == {1: "Lecture", 2: "Lab"}


def test_problems_flags_bad_url_and_empty_selection():
    m = _model_with_courses(sync_all=False)
    m.base_url = "http://insecure"
    for c in m.courses:
        c.selected = False
    probs = m.problems()
    assert any("https://" in p for p in probs)
    assert any("at least one course" in p for p in probs)
