"""Mobile display of the server's target-recognition outcome (Ukrainian, additive fields)."""

from datetime import datetime

from app.models.movement_models import ThreatMovement, ThreatType


def _m(**kw):
    return ThreatMovement(id="1", threat_type=ThreatType.MISSILE, channel_username="@c",
                          received_at=datetime(2026, 11, 16), text="t", **kw)


def test_defaults_keep_old_behaviour():
    assert _m().short_description == "напрямок не вказано"
    assert _m(origin_name="Умань", origin_settlement="Умань").short_description == "з Умань"
    assert _m(origin_name="Суми", origin_settlement="Суми", destination_name="Черкаси",
              destination_settlement="Черкаси").short_description == "Суми → Черкаси"


def test_destination_only_is_ukrainian_not_english():
    text = _m(destination_name="Черкаси", destination_settlement="Черкаси").short_description
    assert text == "Рух на Черкаси (звідки — невідомо)" and "Unknown" not in text


def test_ambiguous_target_is_shown_not_hidden():
    text = _m(target_status="ambiguous", target_text="Андріївку", target_candidates=["a", "b", "c"]).short_description
    assert text == "Ціль неоднозначна: Андріївку (3+ варіантів)"


def test_unrecognized_and_waypoint_states():
    assert "не розпізнано: Гоґвортс" in _m(target_status="unrecognized", target_text="Гоґвортс").short_description
    assert _m(target_status="waypoint_only", waypoints=["Черкаси"]).short_description == \
        "Проміжний пункт: Черкаси (ціль не вказано)"
