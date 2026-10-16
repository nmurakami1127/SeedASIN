from asin_collector.browser_session import is_captcha_page, is_server_error_page


class _FakePage:
    def __init__(self, content: str) -> None:
        self._content = content

    def content(self) -> str:
        return self._content


def test_is_captcha_page_detects_english_marker():
    page = _FakePage("<html><body>Enter the characters you see below</body></html>")
    assert is_captcha_page(page) is True


def test_is_captcha_page_detects_japanese_marker():
    page = _FakePage("<html><body>画像に表示されている文字を入力してください</body></html>")
    assert is_captcha_page(page) is True


def test_is_captcha_page_false_for_normal_page():
    page = _FakePage("<html><body><div data-asin='B0TESTASIN1'>商品</div></body></html>")
    assert is_captcha_page(page) is False


def test_is_server_error_page_detects_real_amazon_500_marker():
    """実際にamazon.comへのアクセスで遭遇した500/503エラーページのマーカーに基づく
    （docs/DEVIATIONS.md #19）。"""
    page = _FakePage('<a href="/ref=cs_503_logo"><img alt="Amazon.com"></a>')
    assert is_server_error_page(page) is True


def test_is_server_error_page_false_for_normal_page():
    page = _FakePage("<html><body><div data-asin='B0TESTASIN1'>商品</div></body></html>")
    assert is_server_error_page(page) is False


def test_is_server_error_page_false_for_captcha_page():
    """CAPTCHAページとサーバーエラーページは別種として区別されること。"""
    page = _FakePage("<html><body>Enter the characters you see below</body></html>")
    assert is_server_error_page(page) is False
