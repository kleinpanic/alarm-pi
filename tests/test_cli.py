from alarm import cli, config


def test_add_list_edit_delete(capsys):
    assert cli.main(["add", "--label", "Wake", "--time", "7:00", "--days", "weekdays"]) == 0
    assert config.get_alarm_by_id("1").time == "07:00"

    assert cli.main(["list"]) == 0
    assert "Wake" in capsys.readouterr().out

    assert cli.main(["edit", "1", "--time", "08:30"]) == 0
    assert config.get_alarm_by_id("1").time == "08:30"

    assert cli.main(["disable", "1"]) == 0
    assert config.get_alarm_by_id("1").enabled is False

    assert cli.main(["delete", "1", "-y"]) == 0
    assert config.load_alarms() == []


def test_add_rejects_bad_time():
    assert cli.main(["add", "--label", "x", "--time", "25:99", "--days", "daily"]) == 1


def test_add_prints_every_structured_validation_issue(capsys):
    result = cli.main([
        "add", "--label", " ", "--time", "25:99", "--days", "daily",
        "--volume", "101", "--irritable-duration", "0", "--irritable-step", "0",
        "--sound", "/missing/alarm.wav",
    ])
    output = capsys.readouterr().out
    assert result == 1
    for field in ("label", "time", "sound_path", "base_volume", "irritable_duration_minutes", "irritable_volume_step"):
        assert f"{field}:" in output


def test_edit_prints_all_structured_issues(capsys):
    assert cli.main(["add", "--label", "Wake", "--time", "07:00", "--days", "daily"]) == 0
    capsys.readouterr()
    result = cli.main(["edit", "1", "--label", " ", "--time", "25:99", "--volume", "101"])
    output = capsys.readouterr().out
    assert result == 1
    assert "label:" in output and "time:" in output and "base_volume:" in output


def test_next_lists_upcoming(capsys):
    cli.main(["add", "--label", "Morning", "--time", "06:00", "--days", "daily"])
    assert cli.main(["next", "-n", "2"]) == 0
    assert "Morning" in capsys.readouterr().out
