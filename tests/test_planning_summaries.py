from decimal import Decimal
import pytest
from app.planning_summaries import engineering_budget,financial_resilience
from app.agents.solomon import schedule_plan,PHASES
from app.workflow_db import WorkflowError


def test_financial_downside_cash_recovery_and_zero_revenue():
    r=financial_resilience(capex='1200',revenue='2400',opex='1200')
    assert r['simple_capex_recovery_month_within_60']==12
    assert r['sixty_month_cumulative_after_capex_usd']=='4800.00'
    assert r['annual_revenue_down_20pct_and_opex_up_20pct_net_usd']=='480.00'
    assert r['illustrative_startup_funding_usd']=='1500.00'
    assert financial_resilience(capex='100',revenue='0',opex='50')['annual_operating_margin_pct'] is None
    assert financial_resilience(capex='100',revenue='50',opex='100')['simple_capex_recovery_month_within_60'] is None
    with pytest.raises(WorkflowError): financial_resilience(capex='NaN',revenue='50',opex='100')


def test_engineering_budget_keeps_average_requirements_distinct_from_capacity():
    r=engineering_budget({'outputs':{'water_l_per_year':3650,'labor_hours_per_year':730,'lighting_kwh_per_year':36500,'rack_footprint_m2':10,'water_plus_structure_kg':500,'lighting_connected_kw':5}})
    assert r['quantities']['average_daily_water']['value']=='10.000'
    assert r['quantities']['average_rack_footprint_mass']['value']=='50.000'
    assert not r['building_capacity_verified']
    assert engineering_budget({'outputs':{}})['unquantified']


def test_schedule_requires_all_durations_and_handles_leap_day():
    assert schedule_plan({})['total_days'] is None
    r=schedule_plan(dict(planning_start='2028-02-28',phase_duration_days={p[0]:1 for p in PHASES}))
    assert r['total_days']==6 and r['phases'][1]['planned_start']=='2028-02-29'
    assert not any(p['execution_authorized'] for p in r['phases'])
    with pytest.raises(WorkflowError): schedule_plan(dict(planning_start='2026-11-03',phase_duration_days={'survey':True}))
