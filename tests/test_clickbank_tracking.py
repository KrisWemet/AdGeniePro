from adgenie.api.routes_tracking import _network_click_id_param
from adgenie.core.tracking import build_final_url


def test_clickbank_uses_external_click_id_parameter():
    """ClickBank INS returns trackingCodes from tid, so this is the attribution join key."""
    param = _network_click_id_param("clickbank")
    url = build_final_url(
        "https://hop.clickbank.net/?affiliate=a&vendor=v",
        "click-123",
        subid_param=param,
    )

    assert param == "tid"
    assert "tid=click-123" in url
    assert "subid=" not in url


def test_clickbank_network_name_is_case_insensitive():
    assert _network_click_id_param("ClickBank") == "tid"


def test_other_networks_keep_generic_subid():
    assert _network_click_id_param("manual") == "subid"
    assert _network_click_id_param("other-network") == "subid"
