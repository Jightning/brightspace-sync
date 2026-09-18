from brightspace_sync.media import MediaInfo
from brightspace_sync.transcribe import (SUFFIX, TranscribeOpts, _paragraphs,
                                         _parse_cues, _render, _stamp, transcribe)

SRT = """1
00:00:00,600 --> 00:00:05,230
Hello everyone here we're going to work
through an example

2
00:00:05,230 --> 00:00:10,050
with a capacitor

3
00:01:02,000 --> 00:01:05,000
<i>and now the second half</i>
"""

VTT = """WEBVTT

NOTE this is a comment

00:01.000 --> 00:03.000
first line

00:03.000 --> 00:05.000
first line

00:05.000 --> 00:07.000
second line
"""


def test_parse_srt_keeps_start_times_and_joins_wrapped_lines():
    cues = _parse_cues(SRT)
    assert cues[0] == (0.6, 5.23, "Hello everyone here we're going to work through an example")
    assert cues[1][:2] == (5.23, 10.05)
    assert cues[2] == (62.0, 65.0, "and now the second half")   # styling tags stripped


def test_parse_vtt_without_hours_drops_repeated_rolling_lines():
    cues = _parse_cues(VTT)
    assert [c[2] for c in cues] == ["first line", "second line"]
    assert cues[0][:2] == (1.0, 3.0)


def test_paragraphs_break_on_a_pause_not_on_back_to_back_cues():
    cues = [(0.0, 1.0, "a"), (1.0, 2.0, "b"), (30.0, 31.0, "c")]
    assert _paragraphs(cues) == [(0.0, "a b"), (30.0, "c")]


def test_stamp_adds_hours_only_past_an_hour():
    assert _stamp(62.4) == "1:02"
    assert _stamp(3723) == "1:02:03"


def test_render_names_the_source_and_timestamps_each_block():
    md = _render("Lecture 1", _parse_cues(SRT), "publisher captions", "en")
    assert md.startswith("# Lecture 1\n\n_Transcript - publisher captions, language: en_")
    assert "**[0:00]** Hello everyone" in md
    assert "**[1:02]** and now the second half" in md


def test_an_existing_transcript_is_kept_without_touching_the_network(tmp_path):
    existing = tmp_path / ("lec1" + SUFFIX)
    existing.write_text("kept", "utf-8")

    # example.invalid would fail loudly if this reached yt-dlp.
    status, path = transcribe(
        MediaInfo(provider="kaltura", embed_url="https://example.invalid/e",
                  entry_id="1_abc", partner_id="1"),
        dest_dir=tmp_path, base_name="lec1", title="Lecture 1",
        base_url="https://school.brightspace.com", video_path=None,
        opts=TranscribeOpts(enabled=True), force=False,
    )
    assert (status, path) == ("exists", existing)
    assert existing.read_text() == "kept"
