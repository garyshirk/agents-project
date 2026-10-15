import json
import tempfile
import unittest
from pathlib import Path

from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError

from arbitrage.contracts import (
    CandidateDestination,
    CandidateDestinationKind,
    CandidateIntakeSource,
    CandidateSource,
    CandidateSourceKind,
    MaterialVariantAttributes,
    MatchQuality,
    ProductAudience,
    ProductCondition,
    ProductIdentity,
    ProductPackaging,
    ProductSizeSystem,
    ResaleRequest,
    ResaleResult,
)
from arbitrage.lead_routing import route_candidate_ready
from arbitrage.orchestration import resale_agent_tool
from arbitrage.persistence import CandidateRepository
from arbitrage.profitability_request_assembly import evaluate_identity_acceptance


class StructuredProductIdentityTests(unittest.TestCase):
    @staticmethod
    def historical_identity_json() -> str:
        return json.dumps(
            {
                "brand": "Nike",
                "product_name": "Pegasus 41",
                "model_number": "FD2722-001",
                "upc_gtin": None,
                "variant": "Men's Black/Anthracite/Black, size 10",
                "condition": "NEW",
                "package_quantity": 1,
            }
        )

    @staticmethod
    def structured_identity(**changes) -> ProductIdentity:
        values = {
            "brand": "Nike",
            "product_name": "Pegasus 41",
            "model_number": "FD2722-001",
            "upc_gtin": None,
            "variant": "Men's Black/Anthracite/Black, size 10",
            "condition": ProductCondition.NEW,
            "package_quantity": 1,
            "material_variant": MaterialVariantAttributes(
                colorway="Black/Anthracite/Black",
                audience=ProductAudience.MENS,
                size_value="10",
                size_system=ProductSizeSystem.US_MENS,
                width=None,
                packaging=ProductPackaging.COMPLETE_ORIGINAL,
            ),
        }
        values.update(changes)
        return ProductIdentity(**values)

    @staticmethod
    def resale(identity: ProductIdentity) -> ResaleResult:
        return ResaleResult(
            researched_identity=identity,
            identity_match=MatchQuality.EXACT,
            identity_match_notes="Exact product identity for contract testing.",
            evidence_items=[],
            verified_realized_price_low=None,
            verified_realized_price_high=None,
            estimated_achievable_price_low=80.0,
            estimated_achievable_price_high=100.0,
            currency="USD",
            evidence_quality="MODERATE",
            evidence_quality_reason="Deterministic contract fixture.",
            demand_assessment="UNKNOWN",
            market_observations=[],
            unresolved_issues=[],
            research_timestamp="2026-10-11T00:00:00Z",
            notes=None,
        )

    def test_historical_json_without_material_variant_remains_valid(self):
        identity = ProductIdentity.model_validate_json(self.historical_identity_json())

        self.assertIsNone(identity.material_variant)
        self.assertEqual(identity.variant, "Men's Black/Anthracite/Black, size 10")

    def test_explicit_null_material_variant_remains_null(self):
        payload = json.loads(self.historical_identity_json())
        payload["material_variant"] = None

        identity = ProductIdentity.model_validate(payload)

        self.assertIsNone(identity.material_variant)
        self.assertIsNone(identity.model_dump(mode="json")["material_variant"])

    def test_complete_structured_material_variant_round_trips(self):
        identity = self.structured_identity()

        restored = ProductIdentity.model_validate_json(identity.model_dump_json())

        self.assertEqual(restored, identity)
        self.assertEqual(restored.variant, identity.variant)
        self.assertEqual(restored.material_variant.colorway, "Black/Anthracite/Black")
        self.assertEqual(restored.material_variant.audience, ProductAudience.MENS)
        self.assertEqual(restored.material_variant.size_value, "10")
        self.assertEqual(
            restored.material_variant.size_system, ProductSizeSystem.US_MENS
        )
        self.assertIsNone(restored.material_variant.width)
        self.assertEqual(
            restored.material_variant.packaging,
            ProductPackaging.COMPLETE_ORIGINAL,
        )

    def test_partially_populated_variant_preserves_null_attributes(self):
        attributes = MaterialVariantAttributes(
            colorway="Black",
            audience=None,
            size_value="10",
            size_system=None,
            width=None,
            packaging=None,
        )

        restored = MaterialVariantAttributes.model_validate_json(
            attributes.model_dump_json()
        )

        self.assertEqual(restored, attributes)
        self.assertIsNone(restored.audience)
        self.assertIsNone(restored.size_system)
        self.assertIsNone(restored.width)
        self.assertIsNone(restored.packaging)

    def test_enum_values_serialize_and_invalid_values_are_rejected(self):
        serialized = self.structured_identity().model_dump(mode="json")
        material_variant = serialized["material_variant"]

        self.assertEqual(material_variant["audience"], "MENS")
        self.assertEqual(material_variant["size_system"], "US_MENS")
        self.assertEqual(material_variant["packaging"], "COMPLETE_ORIGINAL")
        with self.assertRaises(ValidationError):
            MaterialVariantAttributes(audience="ADULTS")
        with self.assertRaises(ValidationError):
            MaterialVariantAttributes(size_system="US")
        with self.assertRaises(ValidationError):
            MaterialVariantAttributes(packaging="BOX")

    def test_candidate_persistence_round_trip_preserves_structured_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CandidateRepository(Path(directory) / "candidate.db")
            identity = self.structured_identity()
            candidate = repository.create_candidate(
                product_identity=identity,
                acquisition_source=CandidateSource(
                    source_kind=CandidateSourceKind.PHYSICAL_STORE,
                    name="Ross",
                    physical_location="Algonquin, IL",
                    url=None,
                    seller_identity="Ross",
                ),
                resale_destination=CandidateDestination(
                    destination_kind=CandidateDestinationKind.MARKETPLACE,
                    name="eBay",
                    url="https://www.ebay.com",
                    seller_account=None,
                ),
                intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
            )

            restored = repository.get_candidate(candidate.candidate_id)

        self.assertEqual(restored.product_identity, identity)
        self.assertEqual(restored.product_identity.variant, identity.variant)

    def test_resale_result_round_trip_preserves_structured_identity(self):
        result = self.resale(self.structured_identity())

        restored = ResaleResult.model_validate_json(result.model_dump_json())

        self.assertEqual(restored, result)
        self.assertEqual(
            restored.researched_identity.material_variant,
            result.researched_identity.material_variant,
        )

    def test_strict_sdk_schemas_construct_for_all_embedded_paths(self):
        identity_schema = AgentOutputSchema(ProductIdentity).json_schema()
        resale_output_schema = AgentOutputSchema(ResaleResult).json_schema()

        self.assertIn("material_variant", identity_schema["properties"])
        self.assertIn("material_variant", identity_schema["required"])
        self.assertIn("MaterialVariantAttributes", identity_schema["$defs"])
        material_schema = identity_schema["$defs"]["MaterialVariantAttributes"]
        self.assertEqual(
            set(material_schema["required"]),
            set(MaterialVariantAttributes.model_fields),
        )
        self.assertIn("ProductIdentity", resale_output_schema["$defs"])
        self.assertIn(
            "material_variant",
            resale_output_schema["$defs"]["ProductIdentity"]["required"],
        )
        self.assertEqual(
            set(resale_agent_tool.params_json_schema["properties"]),
            set(ResaleRequest.model_fields) - {"product_identity"},
        )
        strict_schema_text = json.dumps(resale_agent_tool.params_json_schema)
        self.assertNotIn('"product_identity"', strict_schema_text)
        lead_schema_text = json.dumps(route_candidate_ready.params_json_schema)
        self.assertIn('"material_variant"', lead_schema_text)

    def test_structured_identity_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CandidateRepository(Path(directory) / "identity.db")
            candidate = repository.create_candidate(
                product_identity=self.structured_identity(),
                acquisition_source=CandidateSource(
                    source_kind=CandidateSourceKind.PHYSICAL_STORE,
                    name="Ross",
                    physical_location="Algonquin, IL",
                    url=None,
                    seller_identity="Ross",
                ),
                resale_destination=CandidateDestination(
                    destination_kind=CandidateDestinationKind.MARKETPLACE,
                    name="eBay",
                    url="https://www.ebay.com",
                    seller_account=None,
                ),
                intake_origin=CandidateIntakeSource.HUMAN_PHYSICAL,
            )
            different_structured_detail = self.structured_identity(
                material_variant=MaterialVariantAttributes(
                    colorway="White",
                    audience=ProductAudience.WOMENS,
                    size_value="9",
                    size_system=ProductSizeSystem.US_WOMENS,
                    width="Wide",
                    packaging=ProductPackaging.NO_ORIGINAL,
                )
            )

            decision = evaluate_identity_acceptance(
                candidate.product_identity,
                self.resale(different_structured_detail),
            )

        self.assertFalse(decision.accepted)
        self.assertIn("material_variant.colorway", decision.conflicting_fields)


if __name__ == "__main__":
    unittest.main()
