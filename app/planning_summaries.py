"""Deterministic resource summaries and illustrative finance resilience; no new facts."""
from decimal import Decimal,ROUND_CEILING,ROUND_HALF_UP
from app.workflow_db import WorkflowError


def dec(value):
    if isinstance(value,bool): raise WorkflowError('finite planning quantity required')
    try: result=Decimal(str(value))
    except Exception: raise WorkflowError('finite planning quantity required') from None
    if not result.is_finite() or abs(result)>Decimal('1e25'): raise WorkflowError('bounded finite planning quantity required')
    return result


def formatted(value,places='0.01'):
    return str(value.quantize(Decimal(places),rounding=ROUND_HALF_UP))


def engineering_budget(scenario):
    outputs=scenario.get('outputs') or {};missing=[];values={}
    def ratio(out,name,denominator,unit,basis):
        raw=outputs.get(name)
        if raw is None or denominator is None or denominator<=0: missing.append(out);return
        values[out]=dict(value=formatted(dec(raw)/denominator,'0.001'),unit=unit,classification='estimated',formula=basis)
    ratio('average_daily_water','water_l_per_year',Decimal(365),'L/day','annual water / 365; average, not peak-day supply')
    ratio('average_daily_labor','labor_hours_per_year',Decimal(365),'h/day','annual labor / 365; not a staffing rota')
    ratio('average_daily_energy','lighting_kwh_per_year',Decimal(365),'kWh/day','annual lighting energy / 365; HVAC remains separate')
    area=dec(outputs['rack_footprint_m2']) if outputs.get('rack_footprint_m2') is not None else None
    ratio('average_rack_footprint_mass','water_plus_structure_kg',area,'kg/m2','estimated water + catalog structure mass / rack footprint; not a structural load rating')
    if outputs.get('lighting_connected_kw') is not None:
        values['lighting_connected_load']=dict(value=str(outputs['lighting_connected_kw']),unit='kW',classification='estimated',formula='retained calculated connected lighting load; excludes starting current and other circuits')
    else: missing.append('lighting_connected_load')
    return dict(quantities=values,unquantified=missing,building_capacity_verified=False,
        required_reviews=['Structural distributed/point loads and dynamic loads','Measured electrical capacity, circuit design and startup loads','Peak water supply, sanitation, drainage and backup','HVAC/dehumidification peak capacity and worker-access plan'],
        scope='Preliminary average resource requirements, not verified capacity, staffing, code compliance or construction approval.')


def financial_resilience(*,capex,revenue,opex):
    capex,revenue,opex=map(dec,(capex,revenue,opex))
    if min(capex,revenue,opex)<0: raise WorkflowError('nonnegative financial totals required')
    net=revenue-opex;monthly=net/Decimal(12)
    break_even=capex/monthly if monthly>0 else None
    horizon=60
    month=int(break_even.to_integral_value(rounding=ROUND_CEILING)) if break_even is not None and break_even<=horizon else None
    return dict(scope='Illustrative deterministic planning only; no debt, tax, depreciation, inflation, seasonality or verified performance.',
        annual_operating_margin_pct=formatted(100*net/revenue) if revenue>0 else None,
        revenue_headroom_to_operating_break_even_pct=formatted(100*net/revenue) if revenue>0 else None,
        already_below_operating_break_even=net<=0,
        simple_capex_recovery_month_within_60=month,
        sixty_month_cumulative_after_capex_usd=formatted(monthly*horizon-capex),
        illustrative_three_month_opex_reserve_usd=formatted(opex/Decimal(4)),
        illustrative_startup_funding_usd=formatted(capex+opex/Decimal(4)),
        annual_revenue_down_20pct_and_opex_up_20pct_net_usd=formatted(revenue*Decimal('.8')-opex*Decimal('1.2')),
        annual_milestones=[dict(year=y,cumulative_after_capex_usd=formatted(net*y-capex)) for y in range(1,6)],
        formulas=dict(cumulative='annual operating net * years - initial capex',reserve='annual opex / 4',headroom='100 * (revenue - opex) / revenue'),
        reserve_is_assumed=True,not_added_to_existing_capex_or_opex=True,profitability_verified=False)
