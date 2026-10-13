import json
import unittest
from unittest.mock import patch

from agents.agent_output import AgentOutputSchema
from pydantic import ValidationError

from arbitrage.contracts import (
    ResaleEvidenceItem,
    ResaleEvidenceSourceKind,
    ResaleResult,
)
from arbitrage.specialists.resale import resale_agent
from tests import test_candidate_persistence as fixtures


def historical_item():
    return {
        "marketplace": "eBay",
        "listing_title": "Retail identity reference",
        "evidence_type": "RETAIL_REFERENCE",
        "sale_completion_basis": None,
        "realized_price_basis": None,
        "quantity_sold": None,
        "price": 145.0,
        "currency": "USD",
        "shipping_cost": None,
        "condition": "NEW",
        "sale_or_listing_date": None,
        "url": "https://www.shoepalace.com/products/example",
        "match_quality": "EXACT",
        "limitations": ["Retail reference, not an eBay sale."],
    }


class ResaleEvidenceSourceContractTests(unittest.TestCase):
    def test_historical_item_has_null_source_fields_without_inference(self):
        item = ResaleEvidenceItem.model_validate_json(json.dumps(historical_item()))
        self.assertIsNone(item.source_kind)
        self.assertIsNone(item.source_name)
        self.assertEqual(item.marketplace, "eBay")
        self.assertEqual(item.url, historical_item()["url"])

    def test_historical_result_with_old_evidence_remains_readable(self):
        payload = fixtures.CandidatePersistenceTests().resale_result().model_dump(mode="json")
        payload["evidence_items"] = [historical_item()]
        result = ResaleResult.model_validate_json(json.dumps(payload))
        self.assertIsNone(result.evidence_items[0].source_kind)
        self.assertIsNone(result.evidence_items[0].source_name)

    def test_explicit_null_fields(self):
        item = ResaleEvidenceItem.model_validate(
            dict(historical_item(), source_kind=None, source_name=None)
        )
        restored = ResaleEvidenceItem.model_validate_json(item.model_dump_json())
        self.assertIsNone(restored.source_kind)
        self.assertIsNone(restored.source_name)

    def test_all_source_kinds_validate_and_serialize(self):
        self.assertEqual([kind.value for kind in ResaleEvidenceSourceKind], [
            "DESTINATION_MARKETPLACE", "OTHER_RESALE_MARKETPLACE", "RETAILER",
            "MANUFACTURER", "PRICE_AGGREGATOR", "OTHER", "UNKNOWN",
        ])
        for kind in ResaleEvidenceSourceKind:
            with self.subTest(kind=kind):
                item = ResaleEvidenceItem.model_validate(
                    dict(historical_item(), source_kind=kind.value)
                )
                self.assertEqual(item.source_kind, kind)
                self.assertEqual(item.model_dump(mode="json")["source_kind"], kind.value)

    def test_invalid_source_kind_is_rejected(self):
        for invalid in ("EBAY", "retailer", 123, {}):
            with self.subTest(value=invalid), self.assertRaises(ValidationError):
                ResaleEvidenceItem.model_validate(dict(historical_item(), source_kind=invalid))

    def test_source_name_is_string_or_null(self):
        for name in ("Shoe Palace", "eBay", "Nike", "Google Shopping", ""):
            item = ResaleEvidenceItem.model_validate(dict(historical_item(), source_name=name))
            self.assertEqual(item.source_name, name)
            self.assertIsNone(item.source_kind)
        for invalid in (123, True, [], {}):
            with self.subTest(value=invalid), self.assertRaises(ValidationError):
                ResaleEvidenceItem.model_validate(dict(historical_item(), source_name=invalid))

    def test_independent_optional_fields_and_existing_fields_preserved(self):
        for fields in (
            {"source_kind": "RETAILER", "source_name": None},
            {"source_kind": None, "source_name": "Shoe Palace"},
            {"source_kind": "RETAILER", "source_name": "Shoe Palace"},
        ):
            item = ResaleEvidenceItem.model_validate(dict(historical_item(), **fields))
            serialized = item.model_dump(mode="json")
            for key, value in historical_item().items():
                self.assertEqual(serialized[key], value)
            for key, value in fields.items():
                self.assertEqual(serialized[key], value)

    def test_full_result_round_trip_preserves_source_fields(self):
        result = fixtures.CandidatePersistenceTests().resale_result()
        result.evidence_items = [ResaleEvidenceItem.model_validate(dict(
            historical_item(), source_kind="RETAILER", source_name="Shoe Palace"
        ))]
        restored = ResaleResult.model_validate_json(result.model_dump_json())
        self.assertEqual(restored, result)
        self.assertEqual(restored.evidence_items[0].source_name, "Shoe Palace")

    def test_contradictory_metadata_is_preserved_without_repair(self):
        item = ResaleEvidenceItem.model_validate(dict(
            historical_item(), source_kind="DESTINATION_MARKETPLACE", source_name="Nike"
        ))
        self.assertEqual(item.marketplace, "eBay")
        self.assertEqual(item.source_kind, ResaleEvidenceSourceKind.DESTINATION_MARKETPLACE)
        self.assertEqual(item.source_name, "Nike")
        self.assertEqual(item.evidence_type.value, "RETAIL_REFERENCE")
        self.assertEqual(item.url, historical_item()["url"])

    def test_actual_agent_strict_output_schema_required_nullable_fields(self):
        self.assertIs(resale_agent.output_type, ResaleResult)
        schema = AgentOutputSchema(resale_agent.output_type)
        self.assertTrue(schema.is_strict_json_schema())
        document = schema.json_schema()
        evidence = document["$defs"]["ResaleEvidenceItem"]
        self.assertFalse(evidence["additionalProperties"])
        self.assertEqual(set(evidence["properties"]), set(ResaleEvidenceItem.model_fields))
        self.assertEqual(set(evidence["required"]), set(ResaleEvidenceItem.model_fields))
        for field in ("source_kind", "source_name"):
            self.assertIn({"type": "null"}, evidence["properties"][field]["anyOf"])
        self.assertEqual(document["$defs"]["ResaleEvidenceSourceKind"]["enum"],
                         [kind.value for kind in ResaleEvidenceSourceKind])
        self.assertEqual(document["properties"]["evidence_items"]["items"],
                         {"$ref": "#/$defs/ResaleEvidenceItem"})
        self.assertIn("ProductIdentity", document["$defs"])

    def test_validation_and_schema_construction_have_no_external_access(self):
        with patch("socket.socket", side_effect=AssertionError("network access")), \
             patch("sqlite3.connect", side_effect=AssertionError("database access")):
            item = ResaleEvidenceItem.model_validate(historical_item())
            ResaleEvidenceItem.model_validate_json(item.model_dump_json())
            AgentOutputSchema(resale_agent.output_type).json_schema()


if __name__ == "__main__":
    unittest.main()
