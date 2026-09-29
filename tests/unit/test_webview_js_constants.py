"""Pins webview/static/tensix_logic.js's literal constant tables against
the real Python source they must never silently disagree with -- the same
'one check that reads what actually executes' shape
test_weights_cache_is_derived_once.py and test_run_webview_sh.py already
use in this repo, applied to a JS port instead of a shell/Python pair.

This does NOT run the JS -- it re-derives the JS file's own literal tables
by executing it under Node and reading its exported constants back, then
compares them field-for-field against ui.chipviz/protocol.events' real
values. A hand-edited JS constant that drifts from the Python source fails
here, at test time, not months later when someone notices the Tensix
panel's colors look subtly wrong.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

from protocol.events import STAGE_BANDS
from ui.chipviz import (_MODE_BY_STAGE, _MODE_CAPTION, _POWER_CEILING_W, _POWER_CURVE,
                        _POWER_FLOOR_W, _UNKNOWN_STAGE_MODE)

JS_FILE = pathlib.Path(__file__).resolve().parents[2] / "webview" / "static" / "tensix_logic.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _js_constants():
    script = f"""
const m = require({json.dumps(str(JS_FILE))});
console.log(JSON.stringify({{
  MODE_BY_STAGE: m.MODE_BY_STAGE, MODE_CAPTION: m.MODE_CAPTION,
  UNKNOWN_STAGE_MODE: m.UNKNOWN_STAGE_MODE,
  POWER_FLOOR_W: m.POWER_FLOOR_W, POWER_CEILING_W: m.POWER_CEILING_W,
  POWER_CURVE: m.POWER_CURVE, STAGE_BANDS: m.STAGE_BANDS,
}}));
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                            check=True, timeout=10)
    return json.loads(result.stdout)


def test_mode_by_stage_matches():
    assert _js_constants()["MODE_BY_STAGE"] == _MODE_BY_STAGE


def test_mode_caption_matches():
    assert _js_constants()["MODE_CAPTION"] == _MODE_CAPTION


def test_unknown_stage_mode_matches():
    assert _js_constants()["UNKNOWN_STAGE_MODE"] == _UNKNOWN_STAGE_MODE


def test_power_constants_match():
    js = _js_constants()
    assert js["POWER_FLOOR_W"] == _POWER_FLOOR_W
    assert js["POWER_CEILING_W"] == _POWER_CEILING_W
    assert js["POWER_CURVE"] == _POWER_CURVE


def test_stage_bands_match():
    js = _js_constants()["STAGE_BANDS"]
    python_bands = {stage: list(band) for stage, band in STAGE_BANDS.items()}
    assert js == python_bands
