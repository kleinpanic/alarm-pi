from alarm import config
from alarm.audio import AudioPlayer


def test_resolve_relative_against_project_root():
    rel = AudioPlayer._resolve("sounds/x.mp3")
    assert rel == str(config.PROJECT_ROOT / "sounds/x.mp3")


def test_resolve_absolute_unchanged():
    assert AudioPlayer._resolve("/abs/x.mp3") == "/abs/x.mp3"
