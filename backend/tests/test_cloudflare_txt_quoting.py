from app.services.cloudflare import quote_txt_content
from app.services.cloudflare_sync import _quote_txt_content


def test_txt_content_is_explicitly_quoted():
    assert quote_txt_content("MS=ms123") == '"MS=ms123"'
    assert _quote_txt_content("v=spf1 include:spf.protection.outlook.com ~all") == (
        '"v=spf1 include:spf.protection.outlook.com ~all"'
    )


def test_txt_content_is_not_double_quoted():
    assert quote_txt_content('"v=DMARC1; p=none;"') == '"v=DMARC1; p=none;"'
    assert _quote_txt_content('"MS=ms123"') == '"MS=ms123"'
