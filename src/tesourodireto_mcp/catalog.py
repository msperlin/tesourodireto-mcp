"""Catalog of Brazilian government bond types traded at Tesouro Direto.

The asset codes, aliases and title groups mirror the R package ``GetTDData``
(function ``td_get2``), so users migrating from R get the same identifiers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass


class InvalidAssetCode(ValueError):
    """Raised when an asset code is not part of the catalog."""


@dataclass(frozen=True, slots=True)
class BondType:
    asset_code: str
    name: str
    indexer: str
    coupon: str
    description: str
    group: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


BOND_TYPES: tuple[BondType, ...] = (
    BondType(
        "LTN",
        "Tesouro Prefixado",
        "Prefixado",
        "Zero Coupon",
        "Fixed-rate bond paying R$ 1,000 at maturity.",
        "prefixado",
    ),
    BondType(
        "LFT",
        "Tesouro Selic",
        "SELIC",
        "Zero Coupon",
        "Floating-rate bond indexed to the SELIC overnight interest rate.",
        "selic",
    ),
    BondType(
        "NTN-B",
        "Tesouro IPCA+ com Juros Semestrais",
        "IPCA",
        "Semi-annual Coupons",
        "Inflation-linked bond paying semi-annual coupon interest.",
        "ipca_coupon",
    ),
    BondType(
        "NTN-B Principal",
        "Tesouro IPCA+",
        "IPCA",
        "Zero Coupon",
        "Inflation-linked bond with principal and inflation adjustment paid at maturity.",
        "ipca_principal",
    ),
    BondType(
        "NTN-F",
        "Tesouro Prefixado com Juros Semestrais",
        "Prefixado",
        "Semi-annual Coupons",
        "Fixed-rate bond paying semi-annual coupon interest.",
        "prefixado_coupon",
    ),
    BondType(
        "NTN-C",
        "Tesouro IGP-M com Juros Semestrais",
        "IGP-M",
        "Semi-annual Coupons",
        "Inflation-linked bond indexed to IGP-M (legacy).",
        "igpm_coupon",
    ),
    BondType(
        "NTN-B1",
        "Tesouro RendA+ / Educa+",
        "IPCA",
        "Monthly Amortization",
        "Inflation-linked retirement or education bond paying monthly income.",
        "renda_educa",
    ),
    BondType(
        "RendA+",
        "Tesouro RendA+",
        "IPCA",
        "Monthly Amortization",
        "Inflation-linked retirement bond with monthly payout.",
        "renda",
    ),
    BondType(
        "Educa+",
        "Tesouro Educa+",
        "IPCA",
        "Monthly Amortization",
        "Inflation-linked education bond for tuition funding.",
        "educa",
    ),
)

# Every accepted short code or official name -> internal group.
# Kept in sync with ``td_code_to_group()`` in GetTDData.
ALIASES: dict[str, str] = {
    "LTN": "prefixado",
    "Tesouro Prefixado": "prefixado",
    "LFT": "selic",
    "Tesouro Selic": "selic",
    "NTN-B": "ipca_coupon",
    "Tesouro IPCA+ com Juros Semestrais": "ipca_coupon",
    "NTN-B Principal": "ipca_principal",
    "NTN-B Princip": "ipca_principal",
    "NTNBP": "ipca_principal",
    "Tesouro IPCA+": "ipca_principal",
    "NTN-F": "prefixado_coupon",
    "NTNF": "prefixado_coupon",
    "Tesouro Prefixado com Juros Semestrais": "prefixado_coupon",
    "NTN-C": "igpm_coupon",
    "NTNC": "igpm_coupon",
    "Tesouro IGPM+ com Juros Semestrais": "igpm_coupon",
    "Tesouro IGP-M com Juros Semestrais": "igpm_coupon",
    "NTN-B1": "renda_educa",
    "Tesouro RendA+ / Educa+": "renda_educa",
    "Educa+": "educa",
    "Tesouro Educa+": "educa",
    "RendA+": "renda",
    "Renda+": "renda",
    "Tesouro Renda+ Aposentadoria Extra": "renda",
    "Tesouro RendA+": "renda",
}

# Internal group -> ``tipo_titulo`` values found in the raw Tesouro Direto CSV.
# Kept in sync with ``td_title_groups()`` in GetTDData.
TITLE_GROUPS: dict[str, tuple[str, ...]] = {
    "prefixado": ("Tesouro Prefixado",),
    "selic": ("Tesouro Selic",),
    "ipca_coupon": ("Tesouro IPCA+ com Juros Semestrais",),
    "ipca_principal": ("Tesouro IPCA+",),
    "prefixado_coupon": ("Tesouro Prefixado com Juros Semestrais",),
    "igpm_coupon": (
        "Tesouro IGPM+ com Juros Semestrais",
        "Tesouro IGP-M com Juros Semestrais",
    ),
    "renda_educa": (
        "Tesouro Educa+",
        "Tesouro Renda+ Aposentadoria Extra",
        "Tesouro RendA+ Aposentadoria Extra",
    ),
    "educa": ("Tesouro Educa+",),
    "renda": (
        "Tesouro Renda+ Aposentadoria Extra",
        "Tesouro RendA+ Aposentadoria Extra",
    ),
}

_ALIASES_LOWER = {alias.casefold(): group for alias, group in ALIASES.items()}


def canonical_code(value: str) -> str:
    """Return a normalized asset code/name (whitespace collapsed)."""
    return " ".join(str(value).strip().split())


def resolve_group(value: str) -> str:
    """Map an accepted asset code or official name to its internal group."""
    code = canonical_code(value)
    group = _ALIASES_LOWER.get(code.casefold())
    if group is None:
        valid = sorted(ALIASES)
        raise InvalidAssetCode(
            f"Invalid asset code {value!r}. Valid codes are: {', '.join(valid)}."
        )
    return group


def normalize_asset_codes(codes: Iterable[str] | None) -> list[str] | None:
    """Resolve a list of codes to a deduplicated list of groups.

    ``None`` means "all assets" and is preserved.
    """
    if codes is None:
        return None
    if isinstance(codes, str):
        codes = [codes]
    codes = list(codes)
    if not codes:
        raise InvalidAssetCode(
            "asset_codes is empty. Omit it to include all bonds, or pass codes such as ['LTN']."
        )

    groups: list[str] = []
    for code in codes:
        group = resolve_group(code)
        if group not in groups:
            groups.append(group)
    return groups


def titles_for_groups(groups: Iterable[str] | None) -> frozenset[str] | None:
    """Return the raw ``tipo_titulo`` values that belong to the given groups."""
    if groups is None:
        return None
    titles: set[str] = set()
    for group in groups:
        titles.update(TITLE_GROUPS[group])
    return frozenset(titles)


def bond_types(asset_codes: Iterable[str] | None = None) -> list[BondType]:
    """Return catalog entries, optionally filtered by code or official name."""
    if asset_codes is None:
        return list(BOND_TYPES)

    if isinstance(asset_codes, str):
        asset_codes = [asset_codes]

    wanted = {resolve_group(code) for code in asset_codes}
    return [item for item in BOND_TYPES if item.group in wanted]
