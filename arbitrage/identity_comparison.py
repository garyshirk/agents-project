"""Pure identity evidence comparison, independent of economic materiality.

Both-absent optional fields are reported but do not alone block MATCH.
One-sided values, absent variant objects, unknown condition/quantity, and
absence of an agreeing strong identifier block MATCH. Raw variants are never
parsed. Economic materiality remains the assembler's responsibility.
"""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from arbitrage.contracts import ProductCondition, ProductIdentity


class IdentityComparisonOutcome(str, Enum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    INSUFFICIENT_IDENTITY_EVIDENCE = "INSUFFICIENT_IDENTITY_EVIDENCE"


@dataclass(frozen=True)
class IdentityComparisonResult:
    outcome: IdentityComparisonOutcome
    matched_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    unresolved_fields: tuple[str, ...]
    notes: tuple[str, ...]


def _text(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    return " ".join(value.replace("’", "'").replace("‘", "'").split()).casefold()


def _identifier(value: str) -> str:
    # Preserve punctuation and internal whitespace in identifiers.
    return unicodedata.normalize("NFC", value).strip().casefold()


def _colorway(value: str) -> str:
    # Slash and comma delimit ordered color components; hyphens remain meaningful.
    return re.sub(r"\s*[/,]\s*", "/", _text(value))


def _size(value: str) -> str:
    normalized = _text(value)
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", normalized):
        return str(Decimal(normalized).normalize())
    return normalized


def compare_product_identity(
    candidate: ProductIdentity, researched: ProductIdentity
) -> IdentityComparisonResult:
    """Compare supplied facts without mutation, inference, or external activity."""
    matched: list[str] = []
    conflicts: list[str] = []
    unresolved: list[str] = []
    notes: list[str] = []
    blocking = False

    def compare(field, left, right, normalize=lambda value: value):
        nonlocal blocking
        if isinstance(left, str) and not left.strip():
            left = None
        if isinstance(right, str) and not right.strip():
            right = None
        if left is None or right is None:
            unresolved.append(field)
            if (left is None) != (right is None):
                blocking = True
                notes.append(f"{field}: supplied on only one side.")
            return
        if normalize(left) == normalize(right):
            matched.append(field)
        else:
            conflicts.append(field)

    compare("brand", candidate.brand, researched.brand, _text)
    compare("model_number", candidate.model_number, researched.model_number, _identifier)
    compare("upc_gtin", candidate.upc_gtin, researched.upc_gtin, _identifier)
    compare(
        "condition",
        None if candidate.condition == ProductCondition.UNKNOWN else candidate.condition,
        None if researched.condition == ProductCondition.UNKNOWN else researched.condition,
    )
    compare("package_quantity", candidate.package_quantity, researched.package_quantity)
    if "condition" in unresolved or "package_quantity" in unresolved:
        blocking = True

    left_variant = candidate.material_variant
    right_variant = researched.material_variant
    if left_variant is None or right_variant is None:
        blocking = True
        notes.append("Structured material_variant evidence is absent; raw variant is not parsed.")

    for name in ("colorway", "audience", "size_value", "size_system", "width", "packaging"):
        field = f"material_variant.{name}"
        left = getattr(left_variant, name, None)
        right = getattr(right_variant, name, None)
        if name == "size_value" and left is not None and right is not None:
            left_system = getattr(left_variant, "size_system", None)
            right_system = getattr(right_variant, "size_system", None)
            if left_system is None or right_system is None or left_system != right_system:
                unresolved.append(field)
                blocking = True
                notes.append("size_value: cannot compare without equal known sizing systems.")
                continue
        normalizer = {"colorway": _colorway, "size_value": _size, "width": _text}.get(name)
        compare(field, left, right, normalizer or (lambda value: value))

    if not any(field in matched for field in ("model_number", "upc_gtin")):
        blocking = True
        notes.append("No strong identifier agrees on both sides.")
    notes.append("Both-absent optional fields remain unresolved without alone blocking MATCH.")
    outcome = (
        IdentityComparisonOutcome.CONFLICT if conflicts else
        IdentityComparisonOutcome.INSUFFICIENT_IDENTITY_EVIDENCE if blocking else
        IdentityComparisonOutcome.MATCH
    )
    return IdentityComparisonResult(
        outcome, tuple(matched), tuple(conflicts), tuple(unresolved), tuple(notes)
    )
