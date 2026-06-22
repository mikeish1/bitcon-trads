"""
Computation tests: the dashboard's derived numbers match RiskManager's own math on
identical inputs (architecture §12). If these drift, the dashboard would lie.
"""
from __future__ import annotations

from tests.conftest_web import seed_sample_db, spot_cfg
from web.db import ReadOnlyDB
from web import queries as q


def _open_db(tmp_path):
    db_path = str(tmp_path / "trading_state.db")
    cfg = seed_sample_db(db_path)
    return ReadOnlyDB(db_path), cfg


def test_win_rate_matches_risk_manager_rule(tmp_path):
    db, cfg = _open_db(tmp_path)
    with db.conn() as c:
        s = q.build_summary(c, cfg, prices={"SOL": 160.0}, price_age_seconds=1.0)
    # 1 win + 1 loss = 2 closed (< 10) -> RiskManager returns 0.5 (50%).
    assert s.wins == 1 and s.losses == 1
    assert s.win_rate_pct == 50.0


def test_equity_is_cash_plus_mtm(tmp_path):
    db, cfg = _open_db(tmp_path)
    with db.conn() as c:
        equity, cash, ov, basis = q.compute_equity(c, cfg, {"SOL": 160.0})
    # One open SOL position: qty 1.0 @ 160 = 160 MTM. Equity = paper_cash + 160.
    assert basis == "paper_ledger"
    assert round(ov, 2) == 160.0
    assert round(equity - cash, 2) == 160.0


def test_open_position_computed_fields(tmp_path):
    db, cfg = _open_db(tmp_path)
    with db.conn() as c:
        equity, _, _, _ = q.compute_equity(c, cfg, {"SOL": 160.0})
        pos = q.build_open_positions(c, cfg, {"SOL": 160.0}, {"SOL": False}, equity)
    assert len(pos) == 1
    sol = pos[0]
    assert sol.symbol == "SOL/USDT"
    # uPnL = 1.0*160 - 150 cost = 10.
    assert round(sol.unrealized_pnl_usd, 2) == 10.0
    # distance to stop = (160-140)/160 = 12.5%.
    assert round(sol.distance_to_stop_pct, 1) == 12.5
    # R-multiple = (160-150)/(150-140) = 1.0.
    assert round(sol.r_multiple, 2) == 1.0
    # drawdown from peak (155) = (155-160)/155 -> negative? peak<last so 0-ish negative.
    assert sol.drawdown_from_peak_pct <= 0.0


def test_risk_gauges_thresholds(tmp_path):
    from src.capital_policy import DeployableCapitalPolicy
    db, cfg = _open_db(tmp_path)
    policy = DeployableCapitalPolicy.from_mapping({"max_pct": 0.90}, label="spot")
    with db.conn() as c:
        g = q.build_risk_gauges(c, cfg, {"SOL": 160.0}, policy, regime_on=True)
    assert g.concurrent_positions.limit == 3
    assert g.concurrent_positions.current == 1
    assert g.trades_today.limit == cfg["safety"]["max_trades_per_day"]
    assert not g.circuit_breaker_tripped
    # exposure gauge: open_value 160 vs deployable 0.9*equity.
    assert g.total_exposure.current == round(160.0, 4)


def test_trade_history_pagination_and_aggregates(tmp_path):
    db, cfg = _open_db(tmp_path)
    with db.conn() as c:
        page = q.query_trades(c, limit=1)
        assert len(page.items) == 1
        assert page.has_more is True
        assert page.next_cursor is not None
        agg = q.trade_aggregates(c)
    # 2 closed: BTC win (+~), ETH loss (-~). Net depends on fees; just check counts.
    assert agg.count == 2
    assert agg.wins == 1 and agg.losses == 1


def test_performance_stats(tmp_path):
    db, _ = _open_db(tmp_path)
    with db.conn() as c:
        stats = q.build_performance_stats(c, has_snapshots=True)
    assert stats.closed_trades == 2
    assert stats.wins == 1 and stats.losses == 1
    assert stats.profit_factor is not None  # has both a win and a loss
    assert stats.max_drawdown_pct <= 0.0


def test_equity_series_drawdown(tmp_path):
    db, _ = _open_db(tmp_path)
    with db.conn() as c:
        series = q.build_equity_series(c, has_table=True, since_iso=None)
    assert series.available and len(series.points) == 3
    # Peak 262 then 258 -> drawdown ~ (258-262)/262 = -1.53%.
    assert round(series.max_drawdown_pct, 2) == round((258 - 262) / 262 * 100, 2)


def test_regime_split_buckets(tmp_path):
    db, _ = _open_db(tmp_path)
    with db.conn() as c:
        split = q.build_regime_split(c, has_snapshots=True)
    assert split.available
    assert sum(b.closed_trades for b in split.buckets) == 2
