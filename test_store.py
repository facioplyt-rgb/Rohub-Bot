import tempfile
import unittest
from pathlib import Path

from hub.store import Store, RuleError


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'hub.sqlite3'
        self.db = Store(self.path)
        self.offer = self.db.create_offer(1, 10, 'Mapa', 'mapy', {'price': '25 PLN', 'description': 'Opis'}, False)
        self.db.publish_offer(self.offer, 100, 101)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def order(self, buyer=20):
        return self.db.reserve_order(1, self.offer, buyer, 3)

    def test_no_self_purchase(self):
        with self.assertRaises(RuleError):
            self.db.reserve_order(1, self.offer, 10, 3)

    def test_duplicate_click_reuses_order(self):
        first, created = self.order()
        again, created_again = self.order()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first['id'], again['id'])

    def test_cross_server_purchase_denied(self):
        with self.assertRaises(RuleError):
            self.db.reserve_order(2, self.offer, 20, 3)

    def test_snapshot_survives_offer_change(self):
        order, _ = self.order()
        self.db.update_offer(self.offer, 10, {'price': '99 PLN'}, False)
        self.assertEqual(order['snapshot']['details']['price'], '25 PLN')

    def test_proof_required_for_review(self):
        order, _ = self.order()
        with self.assertRaises(RuleError):
            self.db.review(order['id'], 20, 5, True, True, 'OK')

    def verified(self):
        order, _ = self.order()
        self.db.set_order_channel(order['id'], 200)
        proof = self.db.submit_proof(order['id'], 20, 'opis')
        self.db.decide_proof(proof, 30, True, 'dowód zgodny')
        return order

    def test_review_identity_unique_and_rating_range(self):
        order = self.verified()
        with self.assertRaises(RuleError):
            self.db.review(order['id'], 10, 5, True, True, '')
        with self.assertRaises(RuleError):
            self.db.review(order['id'], 20, 6, True, True, '')
        self.db.review(order['id'], 20, 2, True, False, 'Problem')
        self.db.review(order['id'], 20, 4, True, True, 'Poprawiono')
        self.assertEqual(self.db.profile(1, 10)['reviews'], 1)
        self.assertEqual(self.db.profile(1, 10)['average'], 4)

    def test_old_proof_cannot_override_new_submission(self):
        order, _ = self.order()
        self.db.set_order_channel(order['id'], 200)
        p1 = self.db.submit_proof(order['id'], 20, 'pierwszy')
        self.db.decide_proof(p1, 30, False, 'uzupełnij')
        p2 = self.db.submit_proof(order['id'], 20, 'drugi')
        with self.assertRaises(RuleError):
            self.db.decide_proof(p1, 30, True, 'stary')
        self.db.decide_proof(p2, 30, True, 'OK')

    def test_party_cannot_verify_own_transaction(self):
        order, _ = self.order()
        self.db.set_order_channel(order['id'], 200)
        proof = self.db.submit_proof(order['id'], 20, 'opis')
        for actor in (10, 20):
            with self.assertRaises(RuleError):
                self.db.decide_proof(proof, actor, True, 'OK')

    def test_dispute_does_not_block_verified_negative_review(self):
        order = self.verified()
        self.db.order_action(order['id'], 20, 'dispute')
        self.db.review(order['id'], 20, 1, False, False, 'Brak dostawy')
        self.assertEqual(self.db.get('orders', order['id'])['state'], 'disputed')

    def test_official_edit_requires_new_verification(self):
        self.db.execute("UPDATE offers SET status='pending',requested=1 WHERE id=?", (self.offer,))
        self.db.decide_offer(self.offer, 30, True, 'Sprawdzono')
        self.db.update_offer(self.offer, 10, {'description': 'Nowa wersja'}, False)
        offer = self.db.get('offers', self.offer)
        self.assertEqual(offer['status'], 'pending')
        self.assertFalse(offer['official'])

    def test_cancelled_order_cannot_be_reviewed(self):
        order = self.verified()
        self.db.order_action(order['id'], 30, 'cancel', True)
        with self.assertRaises(RuleError):
            self.db.review(order['id'], 20, 5, True, True, '')

    def test_backup_and_restart_preserve_orders(self):
        order = self.verified()
        backup = Path(self.tmp.name) / 'backup.sqlite3'
        self.db.backup(backup)
        restored = Store(backup)
        self.assertEqual(restored.get('orders', order['id'])['proof_status'], 'approved')
        restored.close()

    def test_staff_seller_cannot_suppress_review_by_closing_own_order(self):
        order = self.verified()
        for action in ('cancel', 'archive'):
            with self.assertRaises(RuleError):
                self.db.order_action(order['id'], 10, action, True)

    def test_archive_requires_proof_but_not_positive_review(self):
        order = self.verified()
        self.db.review(order['id'], 20, 1, False, False, 'Brak dostawy')
        self.db.order_action(order['id'], 30, 'archive', True)
        self.db.review(order['id'], 20, 2, True, False, 'Częściowo poprawiono')
        self.assertEqual(self.db.profile(1, 10)['average'], 2)


if __name__ == '__main__':
    unittest.main()
