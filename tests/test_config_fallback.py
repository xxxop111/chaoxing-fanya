"""本地配置回落：``/api/start`` 在请求没带 tiku/notification/ocr 时要用已保存的配置。

背景：界面只在打开「设置」页时才读一次 ``web_config.json``，直接点「开始」时
``tiku_config`` 等字段是空的，任务就会退化成「没有答题密钥」。后端因此加了一层
回落（``app._saved_settings``），这几个用例钉住这部分。
"""

import json

import app as app_module


def _write(tmp_path, payload, *, bom=False):
    path = tmp_path / "web_config.json"
    text = json.dumps(payload, ensure_ascii=False)
    path.write_bytes((("\ufeff" if bom else "") + text).encode("utf-8"))
    return path


def test_saved_settings_returns_settings_section(tmp_path, monkeypatch):
    config = {
        "settings": {"tiku_config": {"key": "k" * 35, "model": "deepseek-flash"}, "speed": 2},
        "selectedCourses": [],
        "rememberedLogin": {"username": "u", "password": "p", "useCookies": False},
    }
    monkeypatch.setattr(app_module, "CONFIG_FILE", str(_write(tmp_path, config)))
    saved = app_module._saved_settings()
    assert saved["tiku_config"]["key"] == "k" * 35
    assert saved["tiku_config"]["model"] == "deepseek-flash"
    assert saved["speed"] == 2


def test_saved_settings_tolerates_bom(tmp_path, monkeypatch):
    # Windows 记事本/PowerShell 写出来的 json 可能带 BOM，读崩了就等于整份配置丢失。
    monkeypatch.setattr(app_module, "CONFIG_FILE", str(_write(tmp_path, {"settings": {"speed": 2}}, bom=True)))
    assert app_module._saved_settings() == {"speed": 2}


def test_saved_settings_missing_file_and_wrong_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "CONFIG_FILE", str(tmp_path / "nope.json"))
    assert app_module._saved_settings() == {}

    monkeypatch.setattr(app_module, "CONFIG_FILE", str(_write(tmp_path, {"settings": []})))
    assert app_module._saved_settings() == {}

    monkeypatch.setattr(app_module, "CONFIG_FILE", str(_write(tmp_path, {"selectedCourses": []})))
    assert app_module._saved_settings() == {}
