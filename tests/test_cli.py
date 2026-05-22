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


def test_next_lists_upcoming(capsys):
    cli.main(["add", "--label", "Morning", "--time", "06:00", "--days", "daily"])
    assert cli.main(["next", "-n", "2"]) == 0
    assert "Morning" in capsys.readouterr().out
