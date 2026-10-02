"""The grounding check: the model's wording may only contain numbers/ids that came from the API or the SOPs."""
import pytest

from conftest import make_forecast
from weather_bot.reply import check_prose, facts, fmt_num
from weather_bot.sops import evaluate, rank


@pytest.fixture
def matches(policy):
    fc = make_forecast(policy.metrics(), {"wind_gusts_10m": 52.4})
    return rank(evaluate(policy, [policy.by_id()["EXR-003"]], fc))


def test_facts_report_the_api_value_with_unit(matches):
    assert any(f.startswith("highest wind gusts 10m: 52.4 km/h") for f in facts(matches))


@pytest.mark.parametrize("prose,ok", [
    ("Gusts of 52.4 km/h make riding risky.", True),
    ("Gusts near 52 km/h make riding risky.", False),   # rounded: not the API's number
    ("Gusts of 70 km/h are expected.", False),          # invented
    ("The limit is 45 km/h.", False),                   # a threshold that is not in the SOP's advice text
    ("Follow EXR-003 closely.", True),                  # id of a matched SOP is fine
    ("Policy HAZ-001 says stay in.", False),            # cites a policy that did not match
    ("   ", False),
])
def test_check_prose(matches, prose, ok):
    assert (check_prose(prose, matches, facts(matches)) == []) is ok


def test_fmt_num():
    assert (fmt_num(52.0), fmt_num(52.4), fmt_num(0.0)) == ("52", "52.4", "0")
