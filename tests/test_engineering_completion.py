"""Focused contract checks for conceptual engineering and report handoffs."""
import pytest
from pydantic import ValidationError
from app.agents.onesimus.crops import crop_profile
from app.agents.onesimus.service import ClassifiedInput as C, evaluate_scenario
from app.workflow_db import WorkflowError


def site(**values):
    return {k:C(name=k,value=v,unit='m2' if k=='usable_area_m2' else 'm',
                classification='assumed',provenance='test assumption') for k,v in values.items()}


@pytest.mark.parametrize('area',[-1,0])
def test_invalid_area_refused(area):
    with pytest.raises(WorkflowError):
        evaluate_scenario('bench',crop_profile(),site(usable_area_m2=area))


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True])
def test_nonfinite_and_boolean_quantities_refused(value):
    with pytest.raises(ValidationError):
        C(name='usable_area_m2',value=value,unit='m2',classification='assumed')


def test_unit_conversion_and_integer_sites():
    inputs=site(usable_area_m2=100)
    inputs['usable_area_m2'].unit='ft2'
    r=evaluate_scenario('bench',crop_profile(),inputs)
    assert r['outputs']['sites']==232
    assert r['layout_status']=='area_only_estimate'
    assert not r['engineering_complete']
    inputs['usable_area_m2'].unit='kg'
    with pytest.raises(WorkflowError):
        evaluate_scenario('bench',crop_profile(),inputs)


def test_height_and_aisles_control_real_layout():
    inputs=site(usable_area_m2=100,room_length_m=10,room_width_m=10,clear_height_m=4)
    r=evaluate_scenario('rack3',crop_profile(),inputs)
    assert r['layout_status']=='plausible_under_assumptions'
    assert r['outputs']['sites']==2250
    assert r['outputs']['growing_m2']==90
    inputs['clear_height_m'].value=2
    r=evaluate_scenario('rack3',crop_profile(),inputs)
    assert not r['complete'] and r['layout_status']=='impossible'


def test_cleaning_water_not_dosed_as_nutrient_solution():
    inputs=site(usable_area_m2=100)
    inputs['cleaning_l_per_site_cycle']=C(name='cleaning_l_per_site_cycle',value=1,
        unit='L/site/cycle',classification='assumed')
    r=evaluate_scenario('bench',crop_profile(),inputs)
    assert r['outputs']['water_l_per_year']==137500
    assert r['outputs']['nutrient_solution_l_per_year']==112500


def test_extreme_exponents_refuse_before_layout_arithmetic():
    crop=crop_profile();crop['spacing_m'].value=1e-200
    with pytest.raises(WorkflowError,match='numeric range'):
        evaluate_scenario('bench',crop,site(usable_area_m2=100))
    with pytest.raises(WorkflowError,match='numeric range'):
        evaluate_scenario('rack3',crop_profile(),site(usable_area_m2=1e308))
