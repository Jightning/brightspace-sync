import queue
import threading
from types import SimpleNamespace

import pytest
import requests

from brightspace_sync.download import download_to
from brightspace_sync.limits import DownloadBudget, DownloadSkipped, DownloadStopped, LimitRequest, progress_hook
from brightspace_sync.manifest import Manifest
from brightspace_sync.config import Config


def test_each_limit_requires_explicit_approval_and_approval_is_scoped(tmp_path):
    requests_seen = []
    budget = DownloadBudget(tmp_path, 2, 3, 4, lambda r: requests_seen.append(r.kind) or True)
    first = budget.transfer('one')
    first.consume(5)
    first.consume(1)
    budget.transfer('two').consume(5)
    assert requests_seen == ['file', 'run', 'storage', 'file']
    assert budget.used == 11
    with pytest.raises(DownloadSkipped):
        DownloadBudget(tmp_path, 2, 3, 4).transfer('fresh run').consume(5)


@pytest.mark.parametrize('limits,kind', [((1, 99, 99), 'file'), ((99, 1, 99), 'run'),
                                       ((99, 99, 1), 'storage')])
def test_rejected_limit_skips_file_or_stops_run_before_accepting_bytes(tmp_path, limits, kind):
    seen = []
    budget = DownloadBudget(tmp_path, *limits, lambda r: seen.append(r.kind) or False)
    with pytest.raises(DownloadSkipped if kind == 'file' else DownloadStopped):
        budget.transfer('file').consume(2)
    assert seen == [kind]
    assert budget.used == 0


def test_existing_storage_counts_and_stop_flag_cancels(tmp_path):
    (tmp_path / 'existing').write_bytes(b'12345')
    seen = []
    budget = DownloadBudget(tmp_path, 99, 99, 5, lambda r: seen.append(r.kind) or False)
    with pytest.raises(DownloadStopped):
        budget.transfer('new').consume(1)
    assert seen == ['storage']
    budget.should_stop = lambda: True
    with pytest.raises(DownloadStopped, match='user'):
        budget.transfer('new').consume(0)


class Stream:
    status_code = 200
    def __init__(self, known):
        self.headers = {'Content-Length': '4'} if known else {}
        self.closed = False
    def raise_for_status(self):
        pass
    def iter_content(self, **kwargs):
        yield b'12'
        yield b'34'
    def close(self):
        self.closed = True


@pytest.mark.parametrize('known', [True, False])
def test_current_file_downloads_honor_limits_close_stream_and_keep_previous_file(tmp_path, known):
    existing = tmp_path / 'file.pdf'; existing.write_bytes(b'old')
    manifest = Manifest.load(tmp_path)
    manifest.record('topic:1', last_modified='old', local=existing)
    stream = Stream(known)
    budget = DownloadBudget(tmp_path, 3, 99, 99)
    bs = SimpleNamespace(download_budget=budget, get_stream=lambda _: stream)
    result = download_to(bs, '/file', dest_dir=tmp_path, fallback_name='file.pdf', manifest=manifest,
                         key='topic:1', last_modified='new', force=True, dry_run=False)
    assert result.action == 'declined'
    assert 'topic:1' in manifest.touched
    assert existing.read_bytes() == b'old'
    assert not list(tmp_path.glob('*.part'))
    assert stream.closed
    assert manifest.entries['topic:1']['last_modified'] == 'old'


def test_media_hook_counts_deltas_and_pauses_on_estimated_size(tmp_path):
    budget = DownloadBudget(tmp_path, 9, 9, 99)
    hook = progress_hook(budget)
    hook({'status': 'downloading', 'filename': 'video', 'downloaded_bytes': 2})
    hook({'status': 'downloading', 'filename': 'video', 'downloaded_bytes': 5})
    assert budget.used == 5
    with pytest.raises(DownloadSkipped):
        hook({'status': 'downloading', 'filename': 'video', 'total_bytes': 10, 'downloaded_bytes': 5})


def test_cli_prompt_defaults_no_and_accepts_yes(monkeypatch):
    from brightspace_sync.cli import _confirm_download_limit
    request = LimitRequest('run', 'notes.pdf', 1, 2)
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert _confirm_download_limit(request) is False
    monkeypatch.setattr('builtins.input', lambda _: 'yes')
    assert _confirm_download_limit(request) is True


def test_app_prompts_on_main_thread_and_returns_answer_to_worker(monkeypatch):
    from brightspace_sync.gui.tasks import SyncWorker
    from brightspace_sync.gui.app import App
    q = queue.Queue()
    worker = SyncWorker(Config(), q)
    result = []
    thread = threading.Thread(target=lambda: result.append(worker._confirm_download_limit(
        LimitRequest('file', 'syllabus.pdf', 1, 2))))
    thread.start()
    message = q.get(timeout=2)
    assert message[0] == 'download_limit'
    q.put(message)
    called = []
    monkeypatch.setattr('brightspace_sync.gui.app.messagebox.askyesno',
                        lambda *a, **k: called.append(threading.get_ident()) or True)
    fake_app = SimpleNamespace(q=q, after=lambda *a: None, _drain=lambda: None)
    App._drain(fake_app)
    thread.join(timeout=2)
    assert result == [True]
    assert called == [threading.get_ident()]


def test_stopping_app_while_waiting_for_approval_unblocks_worker():
    from brightspace_sync.gui.tasks import SyncWorker
    q = queue.Queue(); worker = SyncWorker(Config(), q); result = []
    thread = threading.Thread(target=lambda: result.append(worker._confirm_download_limit(
        LimitRequest('run', 'file', 1, 2))))
    thread.start(); q.get(timeout=2); worker.stop(); thread.join(timeout=2)
    assert not thread.is_alive() and result == [False]


def test_run_limit_decline_stops_sync_saves_manifest_and_prevents_pruning(monkeypatch, tmp_path):
    from brightspace_sync import sync
    from brightspace_sync.enrollments import Course
    cfg = Config(data_dir=tmp_path, courses_dir=tmp_path / 'courses',
                 include={'content': True}, courses_mode='all', prune=True,
                 download_limits={'file_mib': 99, 'run_mib': 1, 'storage_mib': 99})
    monkeypatch.setattr(sync, 'build_session', lambda *a, **k: (requests.Session(), {}))
    monkeypatch.setattr(sync, 'list_all_courses', lambda bs: [Course(42, 'Example', '', True, True)])
    def content(bs, course, directory, manifest, **kwargs):
        old = directory / 'old.pdf'; old.write_bytes(b'kept')
        manifest.record('topic:old', local=old, last_modified=None)
        manifest.touched.clear()
        bs.download_budget.transfer('huge.pdf').consume(2 * 1024 ** 2)
    monkeypatch.setattr(sync, 'sync_content', content)
    outcome = sync.run_sync(cfg, confirm_download_limit=lambda r: False)
    assert outcome.stopped and outcome.errors and not outcome.ok
    assert list(cfg.courses_dir.rglob('old.pdf'))
    assert list(cfg.courses_dir.rglob('.bs_manifest.json'))


def test_failed_prompt_stops_instead_of_silently_falling_back(tmp_path):
    def broken(request):
        raise RuntimeError('UI closed')
    with pytest.raises(DownloadStopped, match='approval'):
        DownloadBudget(tmp_path, 1, 99, 99, broken).transfer('file').consume(2)


def test_low_disk_space_stops_before_writing(monkeypatch, tmp_path):
    monkeypatch.setattr('brightspace_sync.limits.shutil.disk_usage',
                        lambda p: SimpleNamespace(free=0))
    with pytest.raises(DownloadStopped, match='disk space'):
        DownloadBudget(tmp_path, 99, 99, 99).transfer('file').consume(1)


def test_media_merge_also_checks_final_file_limit(tmp_path):
    from brightspace_sync.limits import postprocess_hook
    first = tmp_path / 'video'; second = tmp_path / 'audio'
    first.write_bytes(b'123'); second.write_bytes(b'456')
    budget = DownloadBudget(tmp_path, 5, 99, 99)
    with pytest.raises(DownloadSkipped):
        postprocess_hook(budget)({'status': 'started',
                                 'info_dict': {'__files_to_merge': [str(first), str(second)]}})


def test_unmetered_media_protocol_stops_without_starting_subprocess(tmp_path):
    import yt_dlp
    from brightspace_sync.limits import youtube_dl_class
    cls = youtube_dl_class(yt_dlp, DownloadBudget(tmp_path, 1, 2, 3))
    instance = object.__new__(cls); instance.params = {}
    with pytest.raises(DownloadStopped, match='protocol'):
        instance.dl('video', {'url': 'rtmp://example.invalid/video', 'protocol': 'rtmp'})


def test_media_limit_cleanup_preserves_completed_and_existing_files(monkeypatch, tmp_path):
    from brightspace_sync import media
    monkeypatch.setattr(media, 'find_ytdlp', lambda: '/unexpected-binary')
    existing = tmp_path / 'video.old.part'; existing.write_bytes(b'keep')
    def via_api(*args):
        (tmp_path / 'video.mp4.part').write_bytes(b'incomplete')
        (tmp_path / 'video.m4a').write_bytes(b'completed audio')
        raise DownloadStopped('declined')
    monkeypatch.setattr(media, '_via_api', via_api)
    monkeypatch.setattr(media, '_via_binary', lambda *a: pytest.fail('unmetered binary'))
    with pytest.raises(DownloadStopped):
        media.download_video(media.MediaInfo(provider='youtube', embed_url='https://example.invalid'),
                             dest_dir=tmp_path, base_name='video', base_url='https://example.invalid',
                             opts=media.VideoOpts(budget=DownloadBudget(tmp_path, 1, 2, 3)))
    assert not (tmp_path / 'video.mp4.part').exists()
    assert existing.exists() and (tmp_path / 'video.m4a').exists()


def test_capped_transcription_cannot_download_unmetered_model(tmp_path):
    from brightspace_sync.transcribe import _run_whisper, TranscribeOpts
    def model(name, **kwargs):
        assert kwargs['local_files_only'] is True
        raise FileNotFoundError('not cached')
    with pytest.raises(DownloadStopped, match='speech model'):
        _run_whisper(model, tmp_path / 'audio', TranscribeOpts(budget=object()))


def test_media_retry_bytes_still_count_against_run_limit(tmp_path):
    budget = DownloadBudget(tmp_path, 99, 6, 99)
    hook = progress_hook(budget)
    hook({'status': 'downloading', 'filename': 'video', 'downloaded_bytes': 5})
    with pytest.raises(DownloadStopped):
        hook({'status': 'downloading', 'filename': 'video', 'downloaded_bytes': 2})


def test_free_space_is_checked_on_temporary_download_destination(monkeypatch, tmp_path):
    root = tmp_path / 'courses'; root.mkdir()
    temporary = tmp_path / 'audio'; temporary.mkdir()
    checked = []
    def disk_usage(path):
        checked.append(path)
        return SimpleNamespace(free=0)
    monkeypatch.setattr('brightspace_sync.limits.shutil.disk_usage', disk_usage)
    budget = DownloadBudget(root, 99, 99, 99)
    with pytest.raises(DownloadStopped):
        progress_hook(budget)({'status': 'downloading', 'filename': str(temporary / 'audio.mp3'),
                               'downloaded_bytes': 1})
    assert checked == [temporary]


@pytest.mark.parametrize('known', [True, False])
@pytest.mark.parametrize('classic', [True, False])
def test_file_rejection_continues_courses_and_prompts_again_next_sync(monkeypatch, tmp_path, known, classic):
    from brightspace_sync import sync
    from brightspace_sync.enrollments import Course
    cfg = Config(data_dir=tmp_path, courses_dir=tmp_path / 'courses',
                 include={'content': True}, courses_mode='all', prune=True,
                 download_limits={'file_mib': 1, 'run_mib': 99, 'storage_mib': 99})
    courses = [Course(42, 'First', 'FIRST', True, True), Course(43, 'Second', 'SECOND', True, True)]
    monkeypatch.setattr(sync, 'build_session', lambda *a, **k: (requests.Session(), {}))
    monkeypatch.setattr(sync, 'list_all_courses', lambda bs: courses)
    topics = [{'Id': i, 'TopicId': i, 'Title': str(i), 'TopicType': 1,
               'Url': f'/files/{i}.pdf', 'LastModifiedDate': 'same'} for i in (1, 2)]
    def get_json(bs, path, **kwargs):
        if path.endswith('/toc'):
            return None if classic else {'Modules': [{'ModuleId': 1, 'Title': 'Files', 'Topics': topics}]}
        if path.endswith('/root/'):
            return [{'Id': 1, 'Title': 'Files', 'Type': 'Module'}]
        if path.endswith('/structure/'):
            return topics
        return next(t for t in topics if path.endswith('/' + str(t['Id'])))
    monkeypatch.setattr(sync.Brightspace, 'get_json', get_json)
    def raw_get(*args, **kwargs):
        response = requests.Response()
        response.status_code = 404
        response._content = b''
        response._content_consumed = True
        return response
    monkeypatch.setattr(sync.Brightspace, '_raw_get', raw_get)
    streams = []
    def get_stream(bs, path):
        assert '/files/' not in path, 'A rejection must not try the fallback URL'
        stream = Stream(False)
        huge = '/topics/1/' in path
        data = b'x' * (2 * 1024 ** 2) if huge else b'good'
        stream.headers = {'Content-Length': str(len(data))} if known else {}
        stream.iter_content = lambda **kwargs: iter((data[:2], data[2:]))
        streams.append(stream)
        return stream
    monkeypatch.setattr(sync.Brightspace, 'get_stream', get_stream)
    prompts = []
    for run in range(2):
        outcome = sync.run_sync(cfg, confirm_download_limit=lambda r: prompts.append(r.kind) or False)
        assert outcome.ok and not outcome.stopped and len(outcome.courses) == 2
        assert prompts == ['file'] * (2 * (run + 1))
        assert len(list(cfg.courses_dir.rglob('2.pdf'))) == 2
        assert not list(cfg.courses_dir.rglob('1.pdf'))
        assert not list(cfg.courses_dir.rglob('*.part'))
        for path in cfg.courses_dir.rglob('.bs_manifest.json'):
            manifest = Manifest.load(path.parent)
            assert not manifest.unchanged('topic:1', last_modified='same')
            assert manifest.unchanged('topic:2', last_modified='same')
    assert all(stream.closed for stream in streams)


def test_stop_during_file_prompt_still_cancels_sync(tmp_path):
    stopped = []
    budget = DownloadBudget(tmp_path, 1, 99, 99,
                            lambda r: stopped.append(True) or False, lambda: bool(stopped))
    with pytest.raises(DownloadStopped, match='user'):
        budget.transfer('file').consume(2)


@pytest.mark.parametrize('exception', [DownloadStopped, DownloadSkipped])
def test_media_rejection_cleans_partial_without_recording_success(monkeypatch, tmp_path, exception):
    from brightspace_sync import media
    def download(*args):
        (tmp_path / 'video.mp4.part').write_bytes(b'partial')
        raise exception('declined')
    monkeypatch.setattr(media, '_via_api', download)
    def run():
        return media.download_video(media.MediaInfo('youtube', 'https://example.invalid'),
                                    dest_dir=tmp_path, base_name='video', base_url='https://example.invalid',
                                    opts=media.VideoOpts(budget=DownloadBudget(tmp_path, 1, 99, 99)))
    if exception is DownloadStopped:
        with pytest.raises(DownloadStopped):
            run()
    else:
        assert run() == ('declined', None)
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('stage', ['captions', 'audio'])
def test_transcript_file_rejection_skips_without_fallback(monkeypatch, tmp_path, stage):
    import importlib
    module = importlib.import_module('brightspace_sync.transcribe')
    def reject(*args, **kwargs):
        raise DownloadSkipped('declined')
    monkeypatch.setattr(module, '_fetch_captions', reject if stage == 'captions' else lambda *a: ([], None))
    monkeypatch.setattr(module, '_speech_to_text', reject if stage == 'audio' else
                        lambda *a: pytest.fail('Rejected captions must not trigger audio download'))
    assert module.transcribe(SimpleNamespace(), dest_dir=tmp_path, base_name='video', title='Video',
                             base_url='https://example.invalid', video_path=None,
                             opts=module.TranscribeOpts(budget=object())) == ('declined', None)
    assert not list(tmp_path.iterdir())


def test_rejected_forced_refresh_prompts_again_then_clears_retry_on_success(tmp_path):
    path = tmp_path / 'file.pdf'
    path.write_bytes(b'old')
    manifest = Manifest.load(tmp_path)
    manifest.record('topic:1', local=path, last_modified='same')
    answers = iter((False, False, True))
    prompts = []
    def confirm(request):
        prompts.append(request.kind)
        return next(answers)
    for force, expected in [(True, 'declined'), (False, 'declined'), (False, 'updated')]:
        budget = DownloadBudget(tmp_path, 3, 9999, 9999, confirm)
        bs = SimpleNamespace(download_budget=budget, get_stream=lambda _: Stream(True))
        result = download_to(bs, '/file', dest_dir=tmp_path, fallback_name='file.pdf',
                             prefer_name='file.pdf', manifest=manifest, key='topic:1',
                             last_modified='same', force=force, dry_run=False)
        assert result.action == expected
        manifest.save()
        manifest = Manifest.load(tmp_path)
        assert manifest.unchanged('topic:1', last_modified='same') == (expected == 'updated')
    assert prompts == ['file'] * 3
    assert path.read_bytes() == b'1234'
