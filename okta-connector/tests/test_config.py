import pytest

from connector.config import ConfigError, load_config

GOOD = {"OKTA_ORG_URL": "https://dev-1.okta.com/", "OKTA_API_TOKEN": "secret-token"}


def test_loads_and_normalises_values():
    cfg = load_config(GOOD)
    assert cfg.okta_org_url == "https://dev-1.okta.com"
    assert cfg.target_url == "http://127.0.0.1:8000"


def test_missing_values_are_named_in_the_error():
    with pytest.raises(ConfigError) as info:
        load_config({})
    assert "OKTA_ORG_URL" in str(info.value)
    assert "OKTA_API_TOKEN" in str(info.value)


def test_org_url_must_be_https():
    with pytest.raises(ConfigError):
        load_config({**GOOD, "OKTA_ORG_URL": "http://dev-1.okta.com"})


def test_token_never_appears_in_repr():
    assert "secret-token" not in repr(load_config(GOOD))


def test_page_size_is_capped_at_oktas_maximum():
    assert load_config({**GOOD, "PAGE_SIZE": "1000"}).page_size == 200


def test_bad_number_gives_config_error():
    with pytest.raises(ConfigError):
        load_config({**GOOD, "MAX_RETRIES": "lots"})
