from unittest.mock import AsyncMock

from maimai_bot import main
from maimai_bot.services.cosmetics import CosmeticSelection


async def test_release_symlinks_survive_writes_and_private_stores_land_in_state(
    tmp_path, monkeypatch
):
    release, state = tmp_path / "release", tmp_path / "state"
    data = release / "data"
    data.mkdir(parents=True)
    state.mkdir()
    for name in ("cosmetics.json", "chat_memory.json"):
        (state / name).write_text("{}", encoding="utf-8")
        (data / name).symlink_to(state / name)
    monkeypatch.setattr(main, "__file__", str(release / "src" / "maimai_bot" / "main.py"))
    monkeypatch.setenv("QQ_APP_ID", "fake")
    monkeypatch.setenv("QQ_APP_SECRET", "fake")
    monkeypatch.setenv("LXNS_CLIENT_ID", "")
    monkeypatch.setattr(main.SongRecognitionService, "start_warming", lambda self: None)
    monkeypatch.setattr(main.QQOfficialAdapter, "run", AsyncMock())
    captured = {}
    original = main.build_router

    def build(chat, scores, images, cosmetics, *args, **kwargs):
        captured.update(chat=chat, cosmetics=cosmetics)
        return original(chat, scores, images, cosmetics, *args, **kwargs)

    monkeypatch.setattr(main, "build_router", build)
    await main.run()
    await captured["cosmetics"]._save_selection("tester", CosmeticSelection(icon_id=3))
    captured["chat"]._memory.append("tester", [{"role": "user", "content": "test memory"}])
    for name in ("cosmetics.json", "chat_memory.json"):
        assert (data / name).is_symlink()
        assert (state / name).read_text(encoding="utf-8") != "{}"
    assert (state / "water-bindings.sqlite3").exists()
    assert (state / "chuni-water-bindings.sqlite3").exists()
    assert {f.name for f in data.iterdir()} == {"cosmetics.json", "chat_memory.json"}
