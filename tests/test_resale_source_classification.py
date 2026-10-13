import json
import unittest
from unittest.mock import patch

from arbitrage.contracts import ResaleEvidenceItem
from arbitrage.profitability_request_assembly import (
    MarketplaceResearchReference,
    ProfitabilityRequestProvenance,
    ResaleSourceClassification as Category,
    SOURCE_CLASSIFICATION_POLICY_VERSION,
    _classify_resale_references,
    _source_hostname,
    assemble_profitability_request,
)
from arbitrage.tools.profitability import _calculate_profitability
from tests import test_profitability_request_assembly as fixtures


def evidence(**changes):
    item = fixtures.ProfitabilityRequestAssemblyTests.resale_evidence().model_dump(mode="json")
    item.update(changes)
    return ResaleEvidenceItem.model_validate(item)


class ResaleSourceClassificationTests(unittest.TestCase):
    def classify(self, *items, candidate=None):
        candidate = candidate or fixtures.ProfitabilityRequestAssemblyTests.candidate()
        resale = fixtures.ProfitabilityRequestAssemblyTests.resale(evidence_items=list(items))
        buckets = _classify_resale_references(candidate, resale)
        return {category: values for category, values in zip(
            (Category.DESTINATION, Category.COMPARATIVE, Category.UNATTRIBUTED,
             Category.RETAIL_REFERENCE, Category.CONFLICTING_SOURCE), buckets, strict=True
        )}

    def reference(self, item):
        references = [reference for bucket in self.classify(item).values()
                      for reference in bucket if reference.url == item.url]
        self.assertEqual(len(references), 1)
        return references[0]

    def test_destination_with_corroborating_signals(self):
        item = evidence(source_kind="DESTINATION_MARKETPLACE", source_name="eBay")
        reference = self.reference(item)
        self.assertEqual(reference.classification, Category.DESTINATION)
        self.assertIn("not independently verified", reference.classification_reason)

    def test_comparative_marketplace(self):
        item = evidence(marketplace="StockX", source_kind="OTHER_RESALE_MARKETPLACE",
                        source_name="StockX", url="https://stockx.com/product")
        self.assertEqual(self.reference(item).classification, Category.COMPARATIVE)

    def test_consistent_retail_and_manufacturer(self):
        for kind, name, url in (("RETAILER", "Shoe Palace", "https://www.shoepalace.com/product"),
                                ("MANUFACTURER", "Nike", "https://nike.com/product")):
            with self.subTest(kind=kind):
                item = evidence(evidence_type="RETAIL_REFERENCE", marketplace=name,
                                source_kind=kind, source_name=name, url=url)
                self.assertEqual(self.reference(item).classification, Category.RETAIL_REFERENCE)

    def test_retail_type_with_destination_label_conflicts(self):
        item = evidence(evidence_type="RETAIL_REFERENCE", url="https://www.shoepalace.com/product")
        self.assertEqual(self.reference(item).classification, Category.CONFLICTING_SOURCE)

    def test_retail_and_manufacturer_kinds_with_destination_label_conflict(self):
        for kind in ("RETAILER", "MANUFACTURER"):
            with self.subTest(kind=kind):
                self.assertEqual(self.reference(evidence(source_kind=kind)).classification,
                                 Category.CONFLICTING_SOURCE)

    def test_destination_kind_with_retail_url_conflicts(self):
        item = evidence(source_kind="DESTINATION_MARKETPLACE", url="https://shoepalace.com/item")
        self.assertEqual(self.reference(item).classification, Category.CONFLICTING_SOURCE)

    def test_other_marketplace_kind_with_destination_url_conflicts(self):
        item = evidence(source_kind="OTHER_RESALE_MARKETPLACE", marketplace="StockX")
        self.assertEqual(self.reference(item).classification, Category.CONFLICTING_SOURCE)

    def test_legacy_label_and_known_url_can_be_corroborated(self):
        reference = self.reference(evidence(source_kind=None, source_name=None))
        self.assertEqual(reference.classification, Category.DESTINATION)
        self.assertIsNone(reference.source_kind)
        self.assertIsNone(reference.source_name)

    def test_each_single_signal_is_insufficient(self):
        for changes in (
            {"marketplace": "eBay"},
            {"marketplace": "", "source_name": "eBay"},
            {"marketplace": "", "source_kind": "DESTINATION_MARKETPLACE"},
        ):
            with self.subTest(changes=changes):
                item = evidence(url="https://unknown.example/item", **changes)
                self.assertEqual(self.reference(item).classification, Category.UNATTRIBUTED)
        self.assertEqual(self.reference(evidence(marketplace="", source_name=None)).classification,
                         Category.UNATTRIBUTED)

    def test_unsupported_marketplace_stays_unattributed(self):
        item = evidence(marketplace="New Market", source_name="New Market",
                        source_kind="OTHER_RESALE_MARKETPLACE", url="https://newmarket.example/item")
        self.assertEqual(self.reference(item).classification, Category.UNATTRIBUTED)

    def test_non_http_missing_and_malformed_hosts(self):
        for url in ("ftp://ebay.com/item", "https:///item", "not a URL", "https://[bad",
                    "https://ebay.com:bad/item", "https://ebay.com:99999/item", "", "/item",
                    "https://user@ebay.com/item", "https://ebay.com/\nitem"):
            with self.subTest(url=url):
                self.assertIsNone(_source_hostname(url))
                self.assertEqual(self.reference(evidence(url=url)).classification, Category.UNATTRIBUTED)

    def test_hostname_case_www_port_path_query(self):
        url = "HTTPS://WWW.EBAY.COM:443/itm/123?q=stockx.com"
        self.assertEqual(_source_hostname(url), "ebay.com")
        self.assertEqual(self.reference(evidence(url=url)).classification, Category.DESTINATION)

    def test_lookalikes_and_unsupported_subdomains(self):
        for host in ("not-ebay.com", "ebay.com.evil.example", "myebay.example", "shop.ebay.com"):
            with self.subTest(host=host):
                self.assertEqual(self.reference(evidence(url=f"https://{host}/item")).classification,
                                 Category.UNATTRIBUTED)

    def test_original_shoe_palace_regression(self):
        item = evidence(marketplace="eBay", evidence_type="RETAIL_REFERENCE",
                        source_kind=None, source_name=None,
                        url="https://www.shoepalace.com/products/nike-fd2722-001")
        buckets = self.classify(item)
        self.assertEqual([ref.url for ref in buckets[Category.CONFLICTING_SOURCE]], [item.url])
        self.assertNotIn(item.url, [ref.url for ref in buckets[Category.DESTINATION]])

    def test_identical_duplicates_deduplicate_but_preserve_observations(self):
        item = evidence()
        references = self.classify(item, item)[Category.DESTINATION]
        ref = next(ref for ref in references if ref.url == item.url)
        self.assertEqual(len([value for value in references if value.url == item.url]), 1)
        self.assertEqual(len(ref.observations), 2)

    def test_conflicting_duplicate_overrides_destination_in_both_orders(self):
        item = evidence()
        contradictory = evidence(marketplace="StockX")
        for ordered in ((item, contradictory), (contradictory, item)):
            buckets = self.classify(*ordered)
            self.assertNotIn(item.url, [ref.url for ref in buckets[Category.DESTINATION]])
            ref = buckets[Category.CONFLICTING_SOURCE][0]
            self.assertEqual(len(ref.observations), 2)

    def test_incomplete_duplicate_downgrades_destination(self):
        item = evidence()
        incomplete = evidence(marketplace="", source_name=None, source_kind=None)
        buckets = self.classify(item, incomplete)
        self.assertIn(item.url, [ref.url for ref in buckets[Category.UNATTRIBUTED]])
        self.assertNotIn(item.url, [ref.url for ref in buckets[Category.DESTINATION]])

    def test_candidate_url_cannot_hide_conflicting_duplicate(self):
        item = evidence(url="https://www.ebay.com", marketplace="Nike",
                        evidence_type="RETAIL_REFERENCE", source_kind="MANUFACTURER")
        buckets = self.classify(item)
        self.assertEqual(buckets[Category.DESTINATION], [])
        self.assertEqual(len(buckets[Category.CONFLICTING_SOURCE][0].observations), 2)

    def test_order_repeatability_original_metadata_and_no_mutation(self):
        items = [evidence(url="https://www.ebay.com/second", marketplace=" eBaY ", source_name="eBay"),
                 evidence(url="https://www.ebay.com/first")]
        before = [item.model_dump_json() for item in items]
        buckets = self.classify(*items)
        self.assertEqual(buckets, self.classify(*items))
        self.assertEqual(before, [item.model_dump_json() for item in items])
        refs = buckets[Category.DESTINATION]
        self.assertEqual([ref.url for ref in refs], ["https://www.ebay.com", items[0].url, items[1].url])
        self.assertEqual(refs[1].marketplace, " eBaY ")
        self.assertEqual(refs[1].observations[0].source_name, "eBay")
        self.assertEqual(refs[1].evidence_type, items[0].evidence_type)

    def test_source_name_conflicts_with_other_identity(self):
        self.assertEqual(self.reference(evidence(source_name="StockX")).classification,
                         Category.CONFLICTING_SOURCE)

    def test_historical_provenance_defaults(self):
        fixture = fixtures.ProfitabilityRequestAssemblyTests()
        provenance = fixture.assemble().provenance.model_dump(mode="json")
        for key in ("source_classification_policy_version", "retail_references", "conflicting_references"):
            provenance.pop(key)
        for key in ("destination_references", "comparative_references", "unattributed_references"):
            provenance[key] = [{name: ref[name] for name in ("url", "marketplace", "origin")}
                               for ref in provenance[key]]
        restored = ProfitabilityRequestProvenance.model_validate(provenance)
        self.assertIsNone(restored.source_classification_policy_version)
        self.assertEqual(restored.retail_references, [])
        old = MarketplaceResearchReference(url="https://example.com", marketplace=None, origin="old")
        self.assertIsNone(old.classification)

    def test_provenance_version_economics_and_price_nonattribution(self):
        fixture = fixtures.ProfitabilityRequestAssemblyTests()
        baseline = fixture.assemble()
        changed = fixture.assemble(snapshot=fixture.snapshot(resale=fixture.resale(evidence_items=[
            evidence(evidence_type="RETAIL_REFERENCE", source_kind="RETAILER",
                     source_name="Shoe Palace", marketplace="Shoe Palace",
                     url="https://shoepalace.com/item"),
            evidence(marketplace="StockX", url="https://www.ebay.com/item"),
        ])))
        self.assertTrue(changed.success)
        self.assertEqual(changed.request.sale.resale_price_low, baseline.request.sale.resale_price_low)
        self.assertEqual(changed.request.sale.resale_price_high, baseline.request.sale.resale_price_high)
        self.assertEqual(changed.provenance.projection, baseline.provenance.projection)
        self.assertEqual(changed.provenance.identity_decision, baseline.provenance.identity_decision)
        self.assertEqual(_calculate_profitability(changed.request), _calculate_profitability(baseline.request))
        self.assertEqual(changed.provenance.source_classification_policy_version,
                         SOURCE_CLASSIFICATION_POLICY_VERSION)
        selection = changed.provenance.resale_price_selection.model_dump(mode="json")
        self.assertNotIn("source_urls", selection)
        self.assertIn("No evidence-item URL", selection["url_attribution"])
        self.assertEqual(changed.request.sale.marketplace_name, "eBay")
        ProfitabilityRequestProvenance.model_validate_json(changed.provenance.model_dump_json())

    def test_classifier_has_no_external_access(self):
        item = evidence()
        with patch("socket.socket", side_effect=AssertionError("network")), \
             patch("sqlite3.connect", side_effect=AssertionError("database")), \
             patch("builtins.open", side_effect=AssertionError("file")):
            self.classify(item)


if __name__ == "__main__":
    unittest.main()
