"""STANDARD static-replication engine for analytic Accumulator structures."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import math

from ..basis import convert_points_100
from ..enums import PricingMethod
from ..instruments import StaticAccumulatorOption
from ..models import MarketState, ValuationConfig, ValuationState
from ..results import PricingResult
from .risk import ThetaRollValue, calculate_standard_greeks, raw_price_to_points_100


_VERSION = "standard-static-accumulator-3"


def _normal_cdf(value: float) -> float:
    """Hart 1968 normal CDF."""
    y = abs(value)
    if y > 37:
        result = 0.0
    else:
        exponential = math.exp(-y ** 2 / 2)
        if y < 7.07106781186547:
            sum_a = 3.52624965998911e-02 * y + 0.700383064443688
            sum_a = sum_a * y + 6.37396220353165
            sum_a = sum_a * y + 33.912866078383
            sum_a = sum_a * y + 112.079291497871
            sum_a = sum_a * y + 221.213596169931
            sum_a = sum_a * y + 220.206867912376
            sum_b = 8.83883476483184e-02 * y + 1.75566716318264
            sum_b = sum_b * y + 16.064177579207
            sum_b = sum_b * y + 86.7807322029461
            sum_b = sum_b * y + 296.564248779674
            sum_b = sum_b * y + 637.333633378831
            sum_b = sum_b * y + 793.826512519948
            sum_b = sum_b * y + 440.413735824752
            result = exponential * sum_a / sum_b
        else:
            sum_a = y + 0.65
            sum_a = y + 4 / sum_a
            sum_a = y + 3 / sum_a
            sum_a = y + 2 / sum_a
            sum_a = y + 1 / sum_a
            result = exponential / (sum_a * 2.506628274631)
    if value > 0:
        result = 1 - result
    return result


def _generalized_bs_price(
    call_put: str, s: float, x: float, t: float, r: float, b: float, v: float,
) -> float:
    d1 = (math.log(s / x) + (b + v ** 2 / 2) * t) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    if call_put == "c":
        return s * math.exp((b - r) * t) * _normal_cdf(d1) - x * math.exp(-r * t) * _normal_cdf(d2)
    return x * math.exp(-r * t) * _normal_cdf(-d2) - s * math.exp((b - r) * t) * _normal_cdf(-d1)


def _generalized_bs(
    output: str, call_put: str, s: float, x: float, t: float, r: float, b: float,
    v: float, *, analytic_delta: bool = False,
) -> float:
    if output == "p":
        return _generalized_bs_price(call_put, s, x, t, r, b, v)
    if output == "d":
        if analytic_delta:
            d1 = (math.log(s / x) + (b + v ** 2 / 2) * t) / (v * math.sqrt(t))
            if call_put == "c":
                return math.exp((b - r) * t) * _normal_cdf(d1)
            return -math.exp((b - r) * t) * _normal_cdf(-d1)
        ds = 0.01
        return (
            _generalized_bs_price(call_put, s + ds, x, t, r, b, v)
            - _generalized_bs_price(call_put, s - ds, x, t, r, b, v)
        ) / (2 * ds)
    return 0.0


def _cash_or_nothing_price(
    call_put: str, s: float, x: float, k: float, t: float, r: float, b: float, v: float,
) -> float:
    d = (math.log(s / x) + (b - v ** 2 / 2) * t) / (v * math.sqrt(t))
    if call_put == "c":
        return k * math.exp(-r * t) * _normal_cdf(d)
    return k * math.exp(-r * t) * _normal_cdf(-d)


def _cash_or_nothing(
    output: str, call_put: str, s: float, x: float, k: float, t: float, r: float,
    b: float, v: float,
) -> float:
    if output == "p":
        return _cash_or_nothing_price(call_put, s, x, k, t, r, b, v)
    if output == "d":
        ds = 0.01
        return (
            _cash_or_nothing_price(call_put, s + ds, x, k, t, r, b, v)
            - _cash_or_nothing_price(call_put, s - ds, x, k, t, r, b, v)
        ) / (2 * ds)
    return 0.0


def _binary_barrier_price(
    type_flag: int, s: float, x: float, h: float, k: float, t: float, r: float,
    b: float, v: float, eta: int, phi: int,
) -> float:
    mu = (b - v ** 2 / 2) / v ** 2
    lambda_ = math.sqrt(mu ** 2 + 2 * r / v ** 2)
    sq = v * math.sqrt(t)
    x1 = math.log(s / x) / sq + (mu + 1) * sq
    x2 = math.log(s / h) / sq + (mu + 1) * sq
    y1 = math.log(h ** 2 / (s * x)) / sq + (mu + 1) * sq
    y2 = math.log(h / s) / sq + (mu + 1) * sq
    z = math.log(h / s) / sq + lambda_ * sq
    a1 = s * math.exp((b - r) * t) * _normal_cdf(phi * x1)
    b1 = k * math.exp(-r * t) * _normal_cdf(phi * x1 - phi * sq)
    a2 = s * math.exp((b - r) * t) * _normal_cdf(phi * x2)
    b2 = k * math.exp(-r * t) * _normal_cdf(phi * x2 - phi * sq)
    a3 = s * math.exp((b - r) * t) * (h / s) ** (2 * (mu + 1)) * _normal_cdf(eta * y1)
    b3 = k * math.exp(-r * t) * (h / s) ** (2 * mu) * _normal_cdf(eta * y1 - eta * sq)
    a4 = s * math.exp((b - r) * t) * (h / s) ** (2 * (mu + 1)) * _normal_cdf(eta * y2)
    b4 = k * math.exp(-r * t) * (h / s) ** (2 * mu) * _normal_cdf(eta * y2 - eta * sq)
    a5 = k * ((h / s) ** (mu + lambda_) * _normal_cdf(eta * z)
              + (h / s) ** (mu - lambda_) * _normal_cdf(eta * z - 2 * eta * lambda_ * sq))

    if x > h:
        values = {
            1: a5, 2: a5, 3: a5, 4: a5, 5: b2 + b4, 6: b2 + b4,
            7: a2 + a4, 8: a2 + a4, 9: b2 - b4, 10: b2 - b4,
            11: a2 - a4, 12: a2 - a4, 13: b3, 14: b3, 15: a3,
            16: a1, 17: b2 - b3 + b4, 18: b1 - b2 + b4,
            19: a2 - a3 + a4, 20: a1 - a2 + a3, 21: b1 - b3,
            22: 0.0, 23: a1 - a3, 24: 0.0, 25: b1 - b2 + b3 - b4,
            26: b2 - b4, 27: a1 - a2 + a3 - a4, 28: a2 - a4,
        }
        return values.get(type_flag, 0.0)
    if x < h:
        values = {
            1: a5, 2: a5, 3: a5, 4: a5, 5: b2 + b4, 6: b2 + b4,
            7: a2 + a4, 8: a2 + a4, 9: b2 - b4, 10: b2 - b4,
            11: a2 - a4, 12: a2 - a4, 13: b1 - b2 + b4,
            14: b2 - b3 + b4, 15: a1 - a2 + a4, 16: a2 - a3 + a4,
            17: b1, 18: b3, 19: a1, 20: a3, 21: b2 - b4,
            22: b1 - b2 + b3 - b4, 23: a2 - a4,
            24: a1 - a2 + a3 - a4, 25: 0.0, 26: b1 - b3, 27: 0.0,
            28: a1 - a3,
        }
        return values.get(type_flag, 0.0)
    return 0.0


def _binary_barrier_boundary(
    output: str, type_flag: int, s: float, x: float, k: float, t: float, r: float,
    b: float,
) -> float:
    disc = math.exp(-r * t)
    pairs = {
        2: "K", 4: "S", 6: "dK", 8: "dS", 10: "0", 12: "0",
        14: "dK_if_ge", 16: "dS_if_ge", 18: "dK_if_le", 20: "dS_if_le",
        22: "0", 24: "0", 26: "0", 28: "0",
    }
    key = type_flag if type_flag % 2 == 0 else type_flag + 1
    rule = pairs.get(key, "0")
    if output == "p":
        if rule == "K": return k
        if rule == "S": return s
        if rule == "dK": return disc * k
        if rule == "dS": return disc * s
        if rule == "dK_if_ge": return disc * k if s >= x else 0.0
        if rule == "dS_if_ge": return disc * s if s >= x else 0.0
        if rule == "dK_if_le": return disc * k if s <= x else 0.0
        if rule == "dS_if_le": return disc * s if s <= x else 0.0
        return 0.0
    if output == "d":
        if rule == "S": return 1.0
        if rule in ("dS", "dS_if_ge", "dS_if_le"):
            return math.exp((b - r) * t)
    return 0.0


def _binary_barrier(
    output: str, type_flag: int, s: float, x: float, h: float, k: float, t: float,
    r: float, b: float, v: float, eta: int, phi: int,
) -> float:
    if s >= h and type_flag % 2 == 0:
        return _binary_barrier_boundary(output, type_flag, s, x, k, t, r, b)
    if s <= h and type_flag % 2 != 0:
        return _binary_barrier_boundary(output, type_flag, s, x, k, t, r, b)
    if output == "p":
        return _binary_barrier_price(type_flag, s, x, h, k, t, r, b, v, eta, phi)
    if output == "d":
        ds = 0.01
        return (
            _binary_barrier_price(type_flag, s + ds, x, h, k, t, r, b, v, eta, phi)
            - _binary_barrier_price(type_flag, s - ds, x, h, k, t, r, b, v, eta, phi)
        ) / (2 * ds)
    return 0.0


def _standard_barrier_price(
    type_flag: str, s: float, x: float, h: float, k: float, t: float, r: float,
    b: float, v: float,
) -> float:
    mu = (b - v ** 2 / 2) / v ** 2
    lambda_ = math.sqrt(mu ** 2 + 2 * r / v ** 2)
    sq = v * math.sqrt(t)
    x1 = math.log(s / x) / sq + (1 + mu) * sq
    x2 = math.log(s / h) / sq + (1 + mu) * sq
    y1 = math.log(h ** 2 / (s * x)) / sq + (1 + mu) * sq
    y2 = math.log(h / s) / sq + (1 + mu) * sq
    z = math.log(h / s) / sq + lambda_ * sq
    if type_flag in ("cdi", "cdo"):
        eta, phi = 1, 1
    elif type_flag in ("cui", "cuo"):
        eta, phi = -1, 1
    elif type_flag in ("pdi", "pdo"):
        eta, phi = 1, -1
    elif type_flag in ("pui", "puo"):
        eta, phi = -1, -1
    else:
        return 0.0
    f1 = phi * s * math.exp((b-r)*t) * _normal_cdf(phi*x1) - phi*x*math.exp(-r*t)*_normal_cdf(phi*x1-phi*sq)
    f2 = phi * s * math.exp((b-r)*t) * _normal_cdf(phi*x2) - phi*x*math.exp(-r*t)*_normal_cdf(phi*x2-phi*sq)
    f3 = (phi*s*math.exp((b-r)*t)*(h/s)**(2*(mu+1))*_normal_cdf(eta*y1)
          - phi*x*math.exp(-r*t)*(h/s)**(2*mu)*_normal_cdf(eta*y1-eta*sq))
    f4 = (phi*s*math.exp((b-r)*t)*(h/s)**(2*(mu+1))*_normal_cdf(eta*y2)
          - phi*x*math.exp(-r*t)*(h/s)**(2*mu)*_normal_cdf(eta*y2-eta*sq))
    f5 = k*math.exp(-r*t)*(_normal_cdf(eta*x2-eta*sq)-(h/s)**(2*mu)*_normal_cdf(eta*y2-eta*sq))
    f6 = k*((h/s)**(mu+lambda_)*_normal_cdf(eta*z)+(h/s)**(mu-lambda_)*_normal_cdf(eta*z-2*eta*lambda_*sq))
    if x > h:
        return {"cdi":f3+f5,"cui":f1+f5,"pdi":f2-f3+f4+f5,"pui":f1-f2+f4+f5,
                "cdo":f1-f3+f6,"cuo":f6,"pdo":f1-f2+f3-f4+f6,"puo":f2-f4+f6}.get(type_flag,0.0)
    if x < h:
        return {"cdi":f1-f2+f4+f5,"cui":f2-f3+f4+f5,"pdi":f1+f5,"pui":f3+f5,
                "cdo":f2+f6-f4,"cuo":f1-f2+f3-f4+f6,"pdo":f6,"puo":f1-f3+f6}.get(type_flag,0.0)
    return 0.0


def _standard_barrier(
    output: str, type_flag: str, s: float, x: float, h: float, k: float, t: float,
    r: float, b: float, v: float,
) -> float:
    out_in = type_flag[-2:]
    call_put = type_flag[0]
    if (out_in == "do" and s <= h) or (out_in == "uo" and s >= h):
        return k if output == "p" else 0.0
    if (out_in == "di" and s <= h) or (out_in == "ui" and s >= h):
        return _generalized_bs(output, call_put, s, x, t, r, b, v, analytic_delta=True)
    if output == "p":
        return _standard_barrier_price(type_flag, s, x, h, k, t, r, b, v)
    if output == "d":
        ds = 0.0001
        return (
            _standard_barrier_price(type_flag, s + ds, x, h, k, t, r, b, v)
            - _standard_barrier_price(type_flag, s - ds, x, h, k, t, r, b, v)
        ) / (2 * ds)
    return 0.0


def _accumulator_value(
    output: str, call_put: str, s: float, s0: float, x: float, h: float, p: float,
    p1: float, ratio: float, t0: float, observations: float, total_observations: float,
    r: float, b: float, v: float, accumulator_type: str, expiry_multiplier: float,
    adjustment: float,
) -> float:
    total = 0.0
    i = observations + t0 - 1
    tau = lambda index: (index - adjustment) / 244

    if accumulator_type == "熔断":
        f1, f2 = ("puo", "cuo") if call_put == "c" else ("cdo", "pdo")
        total -= total_observations*expiry_multiplier*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)
        while i > t0 - 1:
            total += -ratio*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)+_standard_barrier(output,f2,s,x,h,p1,tau(i),r,b,v)
            i -= 1
        return total
    if accumulator_type == "熔断增强":
        f1,f2,f3,f4 = (("puo","cuo","cui","pui") if call_put == "c" else ("cdo","pdo","pdi","cdi"))
        total -= total_observations*expiry_multiplier*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)
        while i > t0 - 1:
            total += (-ratio*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)+_standard_barrier(output,f2,s,x,h,0.0,tau(i),r,b,v)
                      +_standard_barrier(output,f3,s,s0,h,0.0,tau(i),r,b,v)-_standard_barrier(output,f4,s,s0,h,0.0,tau(i),r,b,v))
            i -= 1
        return total
    if accumulator_type == "熔断固定赔付":
        f1,f5,f6,eta,phi = (("puo",22,2,-1,1) if call_put == "c" else ("cdo",25,1,1,-1))
        total -= total_observations*expiry_multiplier*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)
        while i > t0 - 1:
            total += (-ratio*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)+_binary_barrier(output,f5,s,x,h,p,tau(i),r,b,v,eta,phi)
                      +_binary_barrier(output,f6,s,x,h,p1,tau(i),r,b,v,eta,phi))
            i -= 1
        return total
    if accumulator_type == "熔断固定赔付增强":
        f1,f5,f3,f4,eta,phi = (("puo",22,"cui","pui",-1,1) if call_put == "c" else ("cdo",25,"pdi","cdi",1,-1))
        total -= total_observations*expiry_multiplier*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)
        while i > t0 - 1:
            total += (-ratio*_standard_barrier(output,f1,s,x,h,0.0,tau(i),r,b,v)+_binary_barrier(output,f5,s,x,h,p,tau(i),r,b,v,eta,phi)
                      +_standard_barrier(output,f3,s,s0,h,0.0,tau(i),r,b,v)-_standard_barrier(output,f4,s,s0,h,0.0,tau(i),r,b,v))
            i -= 1
        return total

    opposite = "p" if call_put == "c" else "c"
    total -= total_observations*expiry_multiplier*_generalized_bs(output,opposite,s,x,tau(i),r,b,v)
    while i > t0 - 1:
        expiry = tau(i)
        loss = -ratio*_generalized_bs(output,opposite,s,x,expiry,r,b,v)
        if accumulator_type == "标准":
            total += (loss+_generalized_bs(output,call_put,s,x,expiry,r,b,v)-_generalized_bs(output,call_put,s,h,expiry,r,b,v)
                      -_cash_or_nothing(output,call_put,s,h,abs(h-x),expiry,r,b,v))
        elif accumulator_type == "增强":
            total += loss+_generalized_bs(output,call_put,s,x,expiry,r,b,v)-_cash_or_nothing(output,call_put,s,h,abs(s0-x),expiry,r,b,v)
        elif accumulator_type == "不敲出":
            total += loss+_generalized_bs(output,call_put,s,x,expiry,r,b,v)
        elif accumulator_type == "固定赔付":
            total += loss+_cash_or_nothing(output,call_put,s,x,p,expiry,r,b,v)-_cash_or_nothing(output,call_put,s,h,p,expiry,r,b,v)
        elif accumulator_type == "不敲出固定赔付":
            total += loss+_cash_or_nothing(output,call_put,s,x,p,expiry,r,b,v)
        elif accumulator_type == "固定赔付增强":
            total += (loss+_cash_or_nothing(output,call_put,s,x,p,expiry,r,b,v)-_cash_or_nothing(output,call_put,s,h,p-abs(h-s0),expiry,r,b,v)
                      +_generalized_bs(output,call_put,s,h,expiry,r,b,v))
        i -= 1
    return total


def price_static_accumulator_standard(
    instrument: StaticAccumulatorOption,
    market: MarketState,
    config: ValuationConfig,
    valuation_state: ValuationState | None = None,
) -> PricingResult:
    del valuation_state
    def arguments_for(
        changed_instrument: StaticAccumulatorOption,
        changed_market: MarketState,
    ) -> dict[str, object]:
        carry = (
            changed_market.risk_free_rate - changed_market.dividend_yield
            if changed_market.carry is None
            else changed_market.carry
        )
        return dict(
            call_put="c" if changed_instrument.call_put.name == "CALL" else "p",
            s=changed_market.spot,
            s0=changed_instrument.initial_spot,
            x=changed_instrument.strike,
            h=changed_instrument.barrier,
            p=changed_instrument.range_payout,
            p1=changed_instrument.knockout_payout,
            ratio=changed_instrument.loss_multiplier,
            t0=changed_instrument.first_observation,
            observations=changed_instrument.observation_count,
            total_observations=changed_instrument.total_observations,
            r=changed_market.risk_free_rate,
            b=carry,
            v=changed_market.volatility,
            accumulator_type=changed_instrument.accumulator_type,
            expiry_multiplier=changed_instrument.expiry_multiplier,
            adjustment=changed_instrument.day_adjustment,
        )

    arguments = arguments_for(instrument, market)
    raw = _accumulator_value("p", **arguments)
    converted = convert_points_100(float(raw), instrument.basis)
    if converted.pv_points_100 is None:
        raise ValueError("Static Accumulator Greek要求可转换为pv_points_100的basis")

    def price_market(changed_market: MarketState) -> float:
        changed_raw = _accumulator_value(
            "p", **arguments_for(instrument, changed_market)
        )
        return raw_price_to_points_100(changed_raw, instrument.basis)

    def theta_roll(convention) -> ThetaRollValue:
        calendar_shift = convention.theta_calendar_day_shift
        adjustment_shift = calendar_shift * 244.0 / 365.0
        rolled_instrument = replace(
            instrument,
            day_adjustment=instrument.day_adjustment + adjustment_shift,
        )
        rolled_raw = _accumulator_value(
            "p",
            **arguments_for(
                rolled_instrument,
                replace(market, as_of=market.as_of + timedelta(days=calendar_shift)),
            ),
        )
        return ThetaRollValue(
            price_points_100=raw_price_to_points_100(rolled_raw, instrument.basis),
            calendar_day_shift=calendar_shift,
            trading_day_shift=convention.theta_trading_day_shift,
            description="day_adjustment增加calendar_day_shift*244/365",
        )

    greeks, extended_greeks, greek_diagnostics = calculate_standard_greeks(
        base_price_points_100=converted.pv_points_100,
        market=market,
        config=config,
        basis=instrument.basis,
        price_market=price_market,
        theta_roll=theta_roll,
    )
    warnings = tuple(
        dict.fromkeys(
            (
                *converted.warnings,
                "已触障腿使用解析边界短路；观察期限tau=(index-day_adjustment)/244。",
            )
        )
    )
    diagnostics = {
        "engine": "standard.static_accumulator",
        "runtime": "STANDARD_ONLY",
        "quantity_basis": instrument.quantity_basis.name,
        "carry_convention": (
            "explicit_carry" if market.carry is not None else "risk_free_minus_dividend"
        ),
        "boundary_short_circuit": True,
        "tau": "(observation_index-day_adjustment)/244",
        "standard_greeks": greek_diagnostics,
    }
    return PricingResult(
        pv_amount=converted.pv_amount,
        pv_percent=converted.pv_percent,
        pv_points_100=converted.pv_points_100,
        currency=converted.currency,
        greeks=greeks,
        method=PricingMethod.STATIC_REPLICATION,
        version=_VERSION,
        extended_greeks=extended_greeks,
        warnings=warnings,
        diagnostics=diagnostics,
        engine_raw={"price": raw, "unit": "POINTS_100",
                    "quantity_basis": instrument.quantity_basis.name},
    )
