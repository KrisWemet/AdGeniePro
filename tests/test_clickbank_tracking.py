"""What AdGenie puts on an outgoing ClickBank HopLink.

The id on the link is the only thing joining a sale back to the creative that
paid for it. If ClickBank drops or truncates it, the sale still arrives and
still pays, but no creative is credited, so the optimizer reads the winning
ad as a loser and stops funding it.
"""

import string
from urllib.parse import parse_qs, urlparse

from adgenie.api.routes_tracking import _network_click_id_params
from adgenie.core.tracking import CLICK_ID_LENGTH, build_final_url, new_click_id


def test_clickbank_carries_the_click_id_as_both_tid_and_extclid():
    """INS reports `tid` in trackingCodes and `extclid` separately.

    Sending both means a sale still attributes when a seller's order form
    passes only one of them through. Missing both is revenue no creative earns.
    """
    params = _network_click_id_params("clickbank")
    click_id = new_click_id()
    url = build_final_url(
        "https://hop.clickbank.net/?affiliate=a&vendor=v",
        click_id,
        subid_param=params[0],
        extra={p: click_id for p in params[1:]},
    )
    query = parse_qs(urlparse(url).query)

    assert params == ("tid", "extclid")
    assert query["tid"] == [click_id]
    assert query["extclid"] == [click_id]
    assert "subid" not in query
    # The HopLink's own parameters survive.
    assert query["affiliate"] == ["a"] and query["vendor"] == ["v"]


def test_an_existing_hoplink_tid_is_replaced_not_duplicated():
    """Two `tid` values would leave ClickBank to pick one, maybe the stale one."""
    url = build_final_url(
        "https://hop.clickbank.net/?affiliate=a&vendor=v&tid=old", "abc123", "tid"
    )
    query = parse_qs(urlparse(url).query)

    assert query["tid"] == ["abc123"]


ALLOWED = set(string.digits + string.ascii_lowercase)


def test_a_click_id_fits_the_clickbank_tid_limit():
    """Over 24 characters ClickBank truncates; a truncated id matches no click.

    ClickBank also drops an id containing a hyphen or an uppercase letter, so
    the alphabet is checked character by character rather than by length alone.
    """
    assert CLICK_ID_LENGTH <= 24
    for _ in range(200):
        click_id = new_click_id()
        assert len(click_id) == CLICK_ID_LENGTH <= 24
        assert set(click_id) <= ALLOWED


def test_click_ids_do_not_repeat():
    """Shortening the id must not make it guessable or collision-prone."""
    assert len({new_click_id() for _ in range(2000)}) == 2000


def test_clickbank_network_name_is_case_insensitive():
    assert _network_click_id_params("ClickBank") == ("tid", "extclid")
    assert _network_click_id_params("  clickbank  ") == ("tid", "extclid")


def test_other_networks_keep_generic_subid():
    assert _network_click_id_params("manual") == ("subid",)
    assert _network_click_id_params("other-network") == ("subid",)
    assert _network_click_id_params(None) == ("subid",)
