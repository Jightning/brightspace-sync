from brightspace_sync.util import html_to_text

SCHEDULE = """
<p>Read this.</p>
<table>
  <tr><th>Week</th><th>Date</th><th>Topic</th></tr>
  <tr><td>1</td><td>8/24</td><td>Intro, charge &amp; current</td></tr>
  <tr><td>2</td><td>8/31</td><td>Kirchhoff<br>laws</td></tr>
</table>
"""


def test_a_table_becomes_a_markdown_table():
    out = html_to_text(SCHEDULE)
    assert "Read this." in out
    assert "| Week | Date | Topic |" in out
    assert "| --- | --- | --- |" in out
    assert "| 1 | 8/24 | Intro, charge & current |" in out
    assert "| 2 | 8/31 | Kirchhoff laws |" in out   # <br> inside a cell stays one row


def test_short_rows_are_padded_so_the_table_stays_valid():
    out = html_to_text("<table><tr><th>a</th><th>b</th></tr><tr><td>x</td></tr></table>")
    assert "| x |  |" in out


def test_a_pipe_in_a_cell_is_escaped():
    assert r"| a \| b |" in html_to_text("<table><tr><td>a | b</td></tr></table>")


def test_plain_html_is_unaffected():
    assert html_to_text("<p>one</p><ul><li>two</li></ul>") == "one\n\n- two"


def test_a_rowspan_is_repeated_so_later_rows_stay_aligned():
    out = html_to_text(
        "<table>"
        "<tr><th>Week</th><th>Day</th></tr>"
        '<tr><td rowspan="2">1</td><td>M</td></tr>'
        "<tr><td>W</td></tr>"
        "</table>")
    assert "| 1 | M |" in out
    assert "| 1 | W |" in out


def test_a_colspan_fills_every_column_it_covers():
    out = html_to_text(
        '<table><tr><td colspan="3">Exam week</td></tr>'
        "<tr><td>a</td><td>b</td><td>c</td></tr></table>")
    assert "| Exam week | Exam week | Exam week |" in out
