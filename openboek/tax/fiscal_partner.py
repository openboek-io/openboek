"""Fiscal partnership optimizer — find optimal deduction allocation between partners.

Calculates combined IB for all possible allocation scenarios and returns the
split that minimizes total tax.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# ---------------------------------------------------------------------------
# 2026 Dutch IB tax brackets (Box 1)
# ---------------------------------------------------------------------------

IB_BRACKETS_2026 = [
    (Decimal("38441"), Decimal("0.3597")),   # 35.97% up to €38,441
    (Decimal("75518"), Decimal("0.3697")),   # 36.97% from €38,441 to €75,518
    (Decimal("999999999"), Decimal("0.4950")),  # 49.50% above €75,518
]

# General tax credits (approximated)
ALGEMENE_HEFFINGSKORTING_MAX = Decimal("3362")
ARBEIDSKORTING_MAX = Decimal("5532")

# Box 3 (savings & investments)
BOX3_VRIJSTELLING_2026 = Decimal("57000")  # per person
BOX3_RATE_TIER1 = Decimal("0.36")  # forfaitair rendement varies by asset class
BOX3_TAX_RATE = Decimal("0.36")     # 36% over forfaitair rendement

# Eigenwoningforfait
EIGENWONINGFORFAIT_RATE = Decimal("0.0035")  # 0.35% of WOZ value


@dataclass
class PartnerInput:
    """Input data for one partner."""
    name: str
    box1_income: Decimal = Decimal("0")
    dga_salary: Decimal = Decimal("0")  # included in box1
    box2_income: Decimal = Decimal("0")
    box3_vermogen: Decimal = Decimal("0")


@dataclass
class SharedDeductions:
    """Jointly allocatable deductions."""
    hypotheekrenteaftrek: Decimal = Decimal("0")
    eigenwoningforfait: Decimal = Decimal("0")
    woz_waarde: Decimal = Decimal("0")
    giften: Decimal = Decimal("0")
    zorgkosten: Decimal = Decimal("0")
    studiekosten: Decimal = Decimal("0")


@dataclass
class Scenario:
    """Tax calculation result for one allocation scenario."""
    name: str
    partner_a_tax: Decimal = Decimal("0")
    partner_b_tax: Decimal = Decimal("0")
    total_tax: Decimal = Decimal("0")
    allocation: dict = field(default_factory=dict)


@dataclass
class OptimizationResult:
    """Full optimization result with all scenarios."""
    scenario_a: Scenario  # All to partner A
    scenario_b: Scenario  # All to partner B
    optimal: Scenario     # Optimal split
    saving_vs_equal: Decimal = Decimal("0")


def calculate_ib_box1(income: Decimal) -> Decimal:
    """Calculate Box 1 income tax (before credits)."""
    tax = Decimal("0")
    prev_limit = Decimal("0")

    for bracket_limit, rate in IB_BRACKETS_2026:
        if income <= prev_limit:
            break
        taxable_in_bracket = min(income, bracket_limit) - prev_limit
        if taxable_in_bracket > 0:
            tax += taxable_in_bracket * rate
        prev_limit = bracket_limit

    return tax.quantize(Decimal("0.01"))


def calculate_marginal_rate(income: Decimal) -> Decimal:
    """Get the marginal tax rate for a given income level."""
    prev_limit = Decimal("0")
    for bracket_limit, rate in IB_BRACKETS_2026:
        if income <= bracket_limit:
            return rate
        prev_limit = bracket_limit
    return IB_BRACKETS_2026[-1][1]


def calculate_box3_tax(vermogen: Decimal) -> Decimal:
    """Calculate Box 3 tax (simplified)."""
    taxable = max(Decimal("0"), vermogen - BOX3_VRIJSTELLING_2026)
    if taxable <= 0:
        return Decimal("0")
    forfaitair = taxable * Decimal("0.0672")  # weighted average forfaitair rendement
    return (forfaitair * Decimal("0.36")).quantize(Decimal("0.01"))


def _calculate_total_tax(
    partner: PartnerInput,
    woning_aftrek: Decimal,
    woning_forfait: Decimal,
    giften: Decimal,
    zorgkosten: Decimal,
    studiekosten: Decimal,
    box3_extra: Decimal = Decimal("0"),
) -> Decimal:
    """Calculate total IB for a partner with given deduction allocations."""
    # Box 1
    box1 = partner.box1_income + woning_forfait - woning_aftrek - giften - zorgkosten - studiekosten
    box1 = max(Decimal("0"), box1)
    box1_tax = calculate_ib_box1(box1)

    # Approximate heffingskortingen
    box1_tax = max(Decimal("0"), box1_tax - ALGEMENE_HEFFINGSKORTING_MAX)
    if partner.box1_income > 0:
        box1_tax = max(Decimal("0"), box1_tax - ARBEIDSKORTING_MAX)

    # Box 2
    box2_tax = (partner.box2_income * Decimal("0.245")).quantize(Decimal("0.01"))  # 24.5% for first €67k

    # Box 3
    box3_tax = calculate_box3_tax(partner.box3_vermogen + box3_extra)

    return box1_tax + box2_tax + box3_tax


def optimize(
    partner_a: PartnerInput,
    partner_b: PartnerInput,
    shared: SharedDeductions,
) -> OptimizationResult:
    """Find the optimal allocation of shared deductions between fiscal partners.

    Returns three scenarios: all to A, all to B, and optimal split.
    """
    woning_net = shared.hypotheekrenteaftrek - shared.eigenwoningforfait

    # Scenario A: everything to partner A
    tax_a_scen_a = _calculate_total_tax(
        partner_a, shared.hypotheekrenteaftrek, shared.eigenwoningforfait,
        shared.giften, shared.zorgkosten, shared.studiekosten,
    )
    tax_b_scen_a = _calculate_total_tax(
        partner_b, Decimal("0"), Decimal("0"),
        Decimal("0"), Decimal("0"), Decimal("0"),
    )
    scenario_a = Scenario(
        name="all_to_a",
        partner_a_tax=tax_a_scen_a,
        partner_b_tax=tax_b_scen_a,
        total_tax=tax_a_scen_a + tax_b_scen_a,
        allocation={"woning": "A", "giften": "A", "zorgkosten": "A", "studiekosten": "A"},
    )

    # Scenario B: everything to partner B
    tax_a_scen_b = _calculate_total_tax(
        partner_a, Decimal("0"), Decimal("0"),
        Decimal("0"), Decimal("0"), Decimal("0"),
    )
    tax_b_scen_b = _calculate_total_tax(
        partner_b, shared.hypotheekrenteaftrek, shared.eigenwoningforfait,
        shared.giften, shared.zorgkosten, shared.studiekosten,
    )
    scenario_b = Scenario(
        name="all_to_b",
        partner_a_tax=tax_a_scen_b,
        partner_b_tax=tax_b_scen_b,
        total_tax=tax_a_scen_b + tax_b_scen_b,
        allocation={"woning": "B", "giften": "B", "zorgkosten": "B", "studiekosten": "B"},
    )

    # Optimal: assign each deduction to the partner with higher marginal rate
    # Woning (hypotheek + forfait must go together)
    best_total = Decimal("999999999")
    best_allocation = {}
    best_a_tax = Decimal("0")
    best_b_tax = Decimal("0")

    for woning_to in ["A", "B"]:
        for giften_to in ["A", "B"]:
            for zorg_to in ["A", "B"]:
                for studie_to in ["A", "B"]:
                    a_woning = shared.hypotheekrenteaftrek if woning_to == "A" else Decimal("0")
                    a_forfait = shared.eigenwoningforfait if woning_to == "A" else Decimal("0")
                    a_giften = shared.giften if giften_to == "A" else Decimal("0")
                    a_zorg = shared.zorgkosten if zorg_to == "A" else Decimal("0")
                    a_studie = shared.studiekosten if studie_to == "A" else Decimal("0")

                    b_woning = shared.hypotheekrenteaftrek if woning_to == "B" else Decimal("0")
                    b_forfait = shared.eigenwoningforfait if woning_to == "B" else Decimal("0")
                    b_giften = shared.giften if giften_to == "B" else Decimal("0")
                    b_zorg = shared.zorgkosten if zorg_to == "B" else Decimal("0")
                    b_studie = shared.studiekosten if studie_to == "B" else Decimal("0")

                    ta = _calculate_total_tax(partner_a, a_woning, a_forfait, a_giften, a_zorg, a_studie)
                    tb = _calculate_total_tax(partner_b, b_woning, b_forfait, b_giften, b_zorg, b_studie)
                    total = ta + tb

                    if total < best_total:
                        best_total = total
                        best_a_tax = ta
                        best_b_tax = tb
                        best_allocation = {
                            "woning": woning_to,
                            "giften": giften_to,
                            "zorgkosten": zorg_to,
                            "studiekosten": studie_to,
                        }

    optimal = Scenario(
        name="optimal",
        partner_a_tax=best_a_tax,
        partner_b_tax=best_b_tax,
        total_tax=best_total,
        allocation=best_allocation,
    )

    # Saving vs 50/50 (equal split scenario)
    half = shared.hypotheekrenteaftrek / 2
    half_forfait = shared.eigenwoningforfait / 2
    equal_a = _calculate_total_tax(
        partner_a, half, half_forfait,
        shared.giften / 2, shared.zorgkosten / 2, shared.studiekosten / 2,
    )
    equal_b = _calculate_total_tax(
        partner_b, half, half_forfait,
        shared.giften / 2, shared.zorgkosten / 2, shared.studiekosten / 2,
    )
    equal_total = equal_a + equal_b
    saving = equal_total - best_total

    return OptimizationResult(
        scenario_a=scenario_a,
        scenario_b=scenario_b,
        optimal=optimal,
        saving_vs_equal=saving,
    )
