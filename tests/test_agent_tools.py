
import duckdb
import pytest

from src.agent.tools import (
    get_platform_overview,
    compare_segments,
    analyse_feature_usage,
    get_customer_risk,
    analyse_decline_timing,
    get_data_quality,
)

DB = "data/warehouse/fleetpulse.duckdb"


@pytest.fixture
def con():
    connection = duckdb.connect(DB, read_only=True)
    yield connection
    connection.close()


def test_platform_overview(con):
    result = get_platform_overview(con)
    assert "reporting_periods" in result


def test_segment_comparison(con):
    result = compare_segments(con)
    assert "customer_bridge" in result
    assert "stable_customer_segments" in result


def test_feature_usage(con):
    result = analyse_feature_usage(con)
    assert "feature_adoption" in result
    assert "stable_customer_feature_impact" in result


def test_customer_risk(con):
    result = get_customer_risk(con)
    assert isinstance(result, list)


def test_decline_timing(con):
    result = analyse_decline_timing(con)
    assert isinstance(result, list)


def test_data_quality(con):
    result = get_data_quality(con)
    assert result["total_checks"] == 14
    assert result["failed_count"] == 0
    assert result["latest_event_date"] is not None
