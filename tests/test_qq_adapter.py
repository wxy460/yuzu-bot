from maimai_bot.qq_adapter import _qq_avatar_url, _qq_openid_avatar_url


def test_reads_qq_avatar_from_author() -> None:
    assert _qq_avatar_url({"author": {"avatar": "https://q.qlogo.cn/example.png"}}) == (
        "https://q.qlogo.cn/example.png"
    )


def test_normalizes_protocol_relative_qq_avatar() -> None:
    assert _qq_avatar_url({"author": {"avatar": "//q.qlogo.cn/example.png"}}) == (
        "https://q.qlogo.cn/example.png"
    )


def test_rejects_non_https_avatar() -> None:
    assert _qq_avatar_url({"author": {"avatar": "http://example.test/avatar.png"}}) is None


def test_builds_avatar_url_from_official_bot_openid() -> None:
    assert _qq_openid_avatar_url("12345", "OPEN ID") == (
        "https://thirdqq.qlogo.cn/qqapp/12345/OPEN%20ID/640"
    )
