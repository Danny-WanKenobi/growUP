"""Deterministic planning cases and classified physical input integration."""
from decimal import Decimal
import pytest
from app.michael import FinancialInput, financial_inputs_from_raw, planning_cases, MichaelError
from app.agents.onesimus.service import ClassifiedInput, evaluate_scenario
from app.agents.onesimus.crops import crop_profile
from app.workflow_db import WorkflowError
from tests.test_michael import _engineering, _finance


def financial():
    inputs = _finance()
    inputs['occupancy_usd_per_m2_year'] = FinancialInput(name='occupancy_usd_per_m2_year', value=10, classification='assumed')
    inputs['maintenance_usd_per_year'] = FinancialInput(name='maintenance_usd_per_year', value=1200, classification='assumed')
    return inputs


def test_planning_cases_keep_costs_and_show_monthly_arithmetic():
    eng = _engineering(); eng['outputs']['site_floor_area_m2'] = 40
    bundle = financial()
    raw = {name:item.model_dump(mode='json') for name,item in bundle.items()}
    retained = financial_inputs_from_raw(raw)
    assert retained['occupancy_usd_per_m2_year'].value == 10
    cases = planning_cases('bench', eng, retained)
    assert cases['complete'] and set(cases['cases']) == {'conservative','base','optimistic'}
    base = cases['cases']['base']['outputs']
    assert base['opex_breakdown_usd']['occupancy'] == '400.00'
    assert base['opex_breakdown_usd']['maintenance'] == '1200.00'
    expected = (Decimal(base['operating_net_usd_per_year']) / 12).quantize(Decimal('.01'))
    assert abs(Decimal(base['monthly_operating_cashflow_usd']) - expected) <= Decimal('.01')
    nets = [Decimal(cases['cases'][name]['outputs']['operating_net_usd_per_year']) for name in ('conservative','base','optimistic')]
    assert nets[0] < nets[1] < nets[2]
    assert bundle['price_per_kg'].value == 8  # Stress cases never mutate baseline.


def test_missing_planning_cost_is_not_zero():
    result = planning_cases('bench', _engineering(), _finance())
    assert not result['complete'] and not result['cases']
    assert set(result['missing']) == {'occupancy_usd_per_m2_year','maintenance_usd_per_year'}


@pytest.mark.parametrize('name', ['occupancy_usd_per_m2_year','maintenance_usd_per_year'])
def test_negative_optional_cost_refuses(name):
    bundle = financial(); bundle[name].value = Decimal('-1')
    with pytest.raises(MichaelError):
        planning_cases('bench', _engineering(), bundle)


def test_physical_labor_nutrient_and_hvac_units():
    crop = crop_profile()
    for name, value, unit in [('labor_min_per_site_cycle',.1,'h/site/cycle'),('nutrient_ml_per_l_solution',2,'mL/L')]:
        crop[name] = ClassifiedInput(name=name,value=value,unit=unit,classification='assumed',provenance='test')
    site = {name:ClassifiedInput(name=name,value=value,unit=unit,classification='assumed',provenance='test')
        for name,value,unit in [('usable_area_m2',100,'m2'),('hvac_w_per_m2',10,'W/m2'),
            ('hvac_hours_per_day',12,'h/day'),('fixed_labor_hours_per_year',100,'h/year')]}
    out = evaluate_scenario('rack3',crop,site)['outputs']
    assert out['site_floor_area_m2'] == 100 and out['growing_m2'] == 300
    assert out['hvac_kwh_per_year'] == 300*10*12*365/1000
    assert out['labor_hours_per_year'] == pytest.approx(out['heads_per_year']*.1+100)
    assert out['nutrient_l_per_year'] == pytest.approx(out['nutrient_solution_l_per_year']*.002)
    site['hvac_hours_per_day'].value = 25
    with pytest.raises(WorkflowError,match='24 hours'):
        evaluate_scenario('rack3',crop,site)
