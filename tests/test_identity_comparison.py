import unittest
from unittest.mock import patch

from arbitrage.contracts import MaterialVariantAttributes, ProductIdentity
from arbitrage.identity_comparison import IdentityComparisonOutcome as Outcome
from arbitrage.identity_comparison import compare_product_identity


def identity(**changes):
    values = dict(
        brand="Nike", product_name="Pegasus 41", model_number="FD2722-001",
        upc_gtin="123456789012", variant="Original descriptive wording",
        condition="NEW", package_quantity=1,
        material_variant=MaterialVariantAttributes(
            colorway="Black/Anthracite/Black", audience="MENS", size_value="10",
            size_system="US_MENS", width="Men's standard", packaging="COMPLETE_ORIGINAL",
        ),
    )
    values.update(changes)
    return ProductIdentity(**values)


def variant_identity(**changes):
    original = identity()
    variant = original.material_variant.model_dump()
    variant.update(changes)
    return identity(material_variant=MaterialVariantAttributes(**variant))


class IdentityComparisonTests(unittest.TestCase):
    def test_identical(self):
        result = compare_product_identity(identity(), identity())
        self.assertEqual(result.outcome, Outcome.MATCH)
        self.assertEqual(len(result.matched_fields), 11)

    def test_case_and_whitespace(self):
        self.assertEqual(compare_product_identity(
            identity(), identity(brand="  NIKE  ", model_number=" fd2722-001 ")
        ).outcome, Outcome.MATCH)

    def test_unicode_and_apostrophes(self):
        self.assertEqual(compare_product_identity(
            variant_identity(width="Men's café"),
            variant_identity(width=" men’s  cafe\u0301 "),
        ).outcome, Outcome.MATCH)

    def test_color_separators(self):
        self.assertEqual(compare_product_identity(
            identity(), variant_identity(colorway=" black , Anthracite / Black ")
        ).outcome, Outcome.MATCH)

    def test_numeric_size(self):
        self.assertEqual(compare_product_identity(
            identity(), variant_identity(size_value="10.0")
        ).outcome, Outcome.MATCH)

    def test_material_conflicts(self):
        for name, value in (
            ("size_value", "10.5"), ("size_system", "EU"),
            ("audience", "WOMENS"), ("colorway", "White"),
            ("width", "Wide"), ("packaging", "ORIGINAL_INCOMPLETE"),
        ):
            with self.subTest(field=name):
                result = compare_product_identity(identity(), variant_identity(**{name: value}))
                self.assertEqual(result.outcome, Outcome.CONFLICT)
                self.assertIn(f"material_variant.{name}", result.conflicting_fields)

    def test_identifier_condition_quantity_conflicts(self):
        for name, value in (
            ("model_number", "FD2722-002"), ("upc_gtin", "999"),
            ("condition", "USED"), ("package_quantity", 2), ("brand", "Other"),
        ):
            with self.subTest(field=name):
                result = compare_product_identity(identity(), identity(**{name: value}))
                self.assertEqual(result.outcome, Outcome.CONFLICT)
                self.assertIn(name, result.conflicting_fields)

    def test_all_conflicts_reported_in_stable_order(self):
        result = compare_product_identity(identity(), identity(
            brand="Other", model_number="Other", upc_gtin="999", condition="USED",
            package_quantity=2, material_variant=variant_identity(colorway="White").material_variant,
        ))
        self.assertEqual(result.conflicting_fields, (
            "brand", "model_number", "upc_gtin", "condition", "package_quantity",
            "material_variant.colorway",
        ))

    def test_one_sided_attributes(self):
        for name in MaterialVariantAttributes.model_fields:
            for left, right in ((identity(), variant_identity(**{name: None})),
                                (variant_identity(**{name: None}), identity())):
                with self.subTest(field=name):
                    result = compare_product_identity(left, right)
                    self.assertEqual(result.outcome, Outcome.INSUFFICIENT_IDENTITY_EVIDENCE)
                    self.assertIn(f"material_variant.{name}", result.unresolved_fields)

    def test_both_absent_optional_attributes_are_reported_but_nonblocking(self):
        left = variant_identity(width=None, packaging=None, colorway=None)
        result = compare_product_identity(left, left)
        self.assertEqual(result.outcome, Outcome.MATCH)
        self.assertEqual(result.unresolved_fields, (
            "material_variant.colorway", "material_variant.width", "material_variant.packaging",
        ))

    def test_missing_identifiers(self):
        left = identity(model_number=None, upc_gtin=None)
        result = compare_product_identity(left, left)
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_IDENTITY_EVIDENCE)
        self.assertEqual(result.unresolved_fields[:2], ("model_number", "upc_gtin"))

    def test_upc_alone_can_establish_strong_match(self):
        left = identity(model_number=None)
        self.assertEqual(compare_product_identity(left, left).outcome, Outcome.MATCH)

    def test_unknown_condition_and_quantity_block(self):
        for changes in ({"condition": "UNKNOWN"}, {"package_quantity": None}):
            left = identity(**changes)
            self.assertEqual(compare_product_identity(left, left).outcome,
                             Outcome.INSUFFICIENT_IDENTITY_EVIDENCE)

    def test_size_without_system_is_not_compared(self):
        result = compare_product_identity(
            variant_identity(size_system=None), variant_identity(size_system=None, size_value="11")
        )
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_IDENTITY_EVIDENCE)
        self.assertIn("material_variant.size_value", result.unresolved_fields)

    def test_live_legacy_nike_example(self):
        left = identity(upc_gtin=None, material_variant=None,
                        variant="Men's Black/Anthracite/Black, size 10")
        right = identity(upc_gtin=None, material_variant=None,
                         variant="Black/Anthracite/Black, men's US size 10, original box")
        result = compare_product_identity(left, right)
        self.assertEqual(result.outcome, Outcome.INSUFFICIENT_IDENTITY_EVIDENCE)
        self.assertEqual(result.conflicting_fields, ())
        self.assertEqual(len(result.unresolved_fields), 7)

    def test_raw_variant_and_product_name_are_not_parsed(self):
        self.assertEqual(compare_product_identity(identity(), identity(
            variant="Different prose", product_name="Different descriptive name"
        )).outcome, Outcome.MATCH)

    def test_identifier_punctuation_and_color_order_remain_meaningful(self):
        self.assertEqual(compare_product_identity(identity(), identity(
            model_number="FD2722001"
        )).outcome, Outcome.CONFLICT)
        self.assertEqual(compare_product_identity(identity(), variant_identity(
            colorway="Anthracite/Black/Black"
        )).outcome, Outcome.CONFLICT)

    def test_no_mutation_or_side_effects_and_repeatability(self):
        left, right = identity(), identity()
        before = (left.model_dump_json(), right.model_dump_json())
        with patch("builtins.open", side_effect=AssertionError("file access")), \
             patch("socket.socket", side_effect=AssertionError("network access")), \
             patch("sqlite3.connect", side_effect=AssertionError("database access")):
            first = compare_product_identity(left, right)
            second = compare_product_identity(left, right)
        self.assertEqual(first, second)
        self.assertEqual(before, (left.model_dump_json(), right.model_dump_json()))


if __name__ == "__main__":
    unittest.main()
