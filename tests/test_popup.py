from alarm.popup import _connected_output_geometry

XRANDR_STALE_ROOT = """\
Screen 0: minimum 320 x 200, current 2400 x 900, maximum 7680 x 7680
DSI-1 connected 800x480+1600+0 (normal left inverted right x axis y axis) 0mm x 0mm
   800x480       60.05*+
HDMI-1 disconnected (normal left inverted right x axis y axis)
"""

XRANDR_PRIMARY = """\
Screen 0: minimum 320 x 200, current 3520 x 1080, maximum 7680 x 7680
HDMI-1 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 509mm x 286mm
DSI-1 connected primary 1600x900+1920+0 (normal left inverted right x axis y axis) 0mm x 0mm
"""


def test_single_connected_output_wins_over_root():
    assert _connected_output_geometry(XRANDR_STALE_ROOT) == (800, 480, 1600, 0)


def test_primary_output_preferred():
    assert _connected_output_geometry(XRANDR_PRIMARY) == (1600, 900, 1920, 0)


def test_no_connected_outputs_returns_none():
    assert _connected_output_geometry("Screen 0: current 1024 x 768\n") is None
