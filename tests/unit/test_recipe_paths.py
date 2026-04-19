"""Tests for recipe path resolution (data_root anchor)."""

from pathlib import Path

from spyoncino.recipe_paths import (
    gallery_path_from_recipe,
    resolve_path_for_recipe,
    resolve_secrets_path,
    sqlite_path_from_recipe,
)


def test_default_data_root_puts_media_under_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recipe = {}
    p = resolve_path_for_recipe(recipe, "media")
    assert p == (tmp_path / "data" / "media").resolve()


def test_explicit_null_data_root_legacy_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recipe = {"data_root": None}
    p = resolve_path_for_recipe(recipe, "media/clips")
    assert p == (tmp_path / "media" / "clips").resolve()


def test_data_root_joins_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    recipe = {"data_root": "data"}
    p = resolve_path_for_recipe(recipe, "media")
    assert p == (tmp_path / "data" / "media").resolve()


def test_strip_duplicate_data_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    recipe = {"data_root": "data"}
    p = resolve_path_for_recipe(recipe, "data/face_gallery")
    assert p == (tmp_path / "data" / "face_gallery").resolve()


def test_sqlite_default_under_data_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    recipe = {"data_root": "data"}
    p = sqlite_path_from_recipe(recipe)
    assert p.name == "spyoncino.db"
    assert p.parent == (tmp_path / "data").resolve()


def test_sqlite_default_omitted_key_uses_data_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = sqlite_path_from_recipe({})
    assert p == (tmp_path / "data" / "spyoncino.db").resolve()


def test_secrets_path_cwd_not_data_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "config").mkdir(parents=True)
    (tmp_path / "data" / "config" / "secrets.yaml").write_text("x: 1\n")
    recipe = {"data_root": "data", "secrets_path": "data/config/secrets.yaml"}
    s = resolve_secrets_path(recipe)
    assert Path(s) == (tmp_path / "data" / "config" / "secrets.yaml").resolve()


def test_gallery_path_face_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    recipe = {
        "data_root": "data",
        "postproc": [
            {
                "name": "face_identification",
                "class": "face_identification",
                "params": {"gallery_path": "data/face_gallery"},
            }
        ],
    }
    p = gallery_path_from_recipe(recipe)
    assert p == (tmp_path / "data" / "face_gallery").resolve()
