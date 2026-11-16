import sqlite3
import tempfile
import unittest
from pathlib import Path

from boardgame_house.db import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.store.run("INSERT INTO events(id,name,fee,currency,instructions,open) VALUES (1,'Raiders','500','IRT','Transfer instructions',1)")
        for uid, nickname in [(10, 'Alice'), (20, 'Bob'), (30, 'Unpaid')]:
            self.store.run('INSERT INTO registrations(user_id,event_id,name,surname,phone,nickname) VALUES (?,1,?,?,?,?)',
                           (uid, nickname, 'Test', '+989123456789', nickname))

    def tearDown(self):
        self.store.db.close()

    def approve(self, rid):
        self.store.run("UPDATE registrations SET status='pending' WHERE id=?", (rid,))
        self.assertTrue(self.store.decide(rid, 99, True))

    def test_approval_is_idempotent_and_audited(self):
        self.approve(1)
        self.assertFalse(self.store.decide(1, 99, False))
        self.assertEqual(self.store.one('SELECT status FROM registrations WHERE id=1')['status'], 'paid')
        self.assertEqual(len(self.store.rows('SELECT * FROM audit')), 1)

    def test_unpaid_scores_cannot_be_entered(self):
        with self.assertRaises(ValueError):
            self.store.score(3, 'final', 1000, 99)
        self.assertEqual(self.store.leaderboard(1), [])

    def test_best_scores_and_corrections(self):
        self.approve(1)
        self.approve(2)
        self.store.score(1, 'one', 15, 99)
        self.store.score(1, 'two', 20, 99)
        self.store.score(2, 'one', 25, 99)
        self.assertEqual([r['nickname'] for r in self.store.leaderboard(1)], ['Bob', 'Alice'])
        self.store.score(2, 'one', 10, 99)
        rows = self.store.leaderboard(1)
        self.assertEqual([(r['nickname'], r['best']) for r in rows], [('Alice', 20), ('Bob', 10)])
        self.assertEqual(len(self.store.rows('SELECT * FROM scores')), 3)

    def add_edition(self, score):
        self.store.run("INSERT INTO events(id,name,fee,currency,instructions) VALUES (2,'Raiders II','400000','IRT','Transfer')")
        self.store.run("INSERT INTO registrations(user_id,event_id,name,surname,phone,nickname,status) VALUES (10,2,'Alice','Test','12345678','New nickname','paid')")
        self.store.score(4, 'one', score, 99)

    def test_later_lower_score_preserves_all_time_record(self):
        self.approve(1)
        self.store.score(1, 'one', 90, 99)
        self.add_edition(50)
        self.store.run('UPDATE events SET open=0 WHERE id=1')
        for eid in (1, 2):
            rows = self.store.leaderboard(eid)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['best'], 90)
            self.assertEqual(rows[0]['nickname'], 'New nickname')

    def test_later_higher_score_updates_all_time_record(self):
        self.approve(1)
        self.store.score(1, 'one', 90, 99)
        self.add_edition(120)
        self.assertEqual(self.store.leaderboard(1)[0]['best'], 120)
        self.assertEqual(self.store.one('SELECT score FROM scores WHERE registration_id=1')['score'], 90)

    def test_different_game_series_is_excluded(self):
        self.approve(1)
        self.store.score(1, 'one', 90, 99)
        self.add_edition(500)
        self.store.run("UPDATE events SET series='other' WHERE id=2")
        self.assertEqual(self.store.leaderboard(1)[0]['best'], 90)
        self.assertEqual(self.store.leaderboard(2)[0]['best'], 500)

    def test_championship_is_explicit_and_idempotent(self):
        self.approve(1)
        self.store.score(1, 'final', 90, 99)
        self.assertEqual(self.store.titles(10), [])
        self.store.champion(1, 99)
        self.store.champion(1, 99)
        self.assertEqual([r['name'] for r in self.store.titles(10)], ['Raiders'])
        self.assertEqual(self.store.titles(20), [])
        self.store.champion(1, 99, remove=True)
        self.assertEqual(self.store.titles(10), [])

    def test_each_event_has_one_champion_and_reassignment_removes_previous_title(self):
        self.approve(1)
        self.approve(2)
        self.store.score(1, 'final', 90, 99)
        self.store.score(2, 'final', 80, 99)
        self.store.champion(1, 99)
        self.store.champion(2, 99)
        self.assertEqual(self.store.titles(10), [])
        self.assertEqual(len(self.store.titles(20)), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM champions')), 1)
        # Removing a former champion must not clear the current champion.
        self.store.champion(1, 99, remove=True)
        self.assertEqual(len(self.store.titles(20)), 1)
        self.add_edition(100)
        self.store.champion(4, 99)
        self.assertEqual(len(self.store.titles(10)), 1)
        self.assertEqual(len(self.store.titles(20)), 1)

    def test_championship_requires_approved_player_with_score(self):
        with self.assertRaises(ValueError):
            self.store.champion(3, 99)
        self.approve(1)
        with self.assertRaises(ValueError):
            self.store.champion(1, 99)

    def test_existing_event_is_upgraded_without_losing_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'old.sqlite3')
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,name TEXT,fee TEXT,currency TEXT,instructions TEXT,open INTEGER)")
            old.execute("INSERT INTO events VALUES(1,'Raiders','','IRT','',1)")
            old.commit()
            old.close()
            upgraded = Store(path)
            event = upgraded.one('SELECT * FROM events')
            self.assertEqual(event['series'], 'raiders')
            self.assertEqual(event['fee'], '400000')
            upgraded.db.close()

    def test_duplicate_registration_is_blocked(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.run("INSERT INTO registrations(user_id,event_id,name,surname,phone,nickname) VALUES (10,1,'X','Y','12345678','Z')")

    def test_rejected_payment_can_be_resubmitted(self):
        self.store.run("UPDATE registrations SET status='pending' WHERE id=1")
        self.assertTrue(self.store.decide(1, 99, False))
        self.store.run("UPDATE registrations SET status='pending' WHERE id=1 AND status='rejected'")
        self.assertTrue(self.store.decide(1, 99, True))

    def test_draft_survives_reopening_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'test.sqlite3')
            first = Store(path)
            first.draft(10, 'surname', {'name': 'Alice', 'eid': 1})
            first.db.close()
            second = Store(path)
            self.assertEqual(second.get_draft(10), ('surname', {'name': 'Alice', 'eid': 1}))
            second.clear(10)
            self.assertEqual(second.get_draft(10), (None, {}))
            second.db.close()


if __name__ == '__main__':
    unittest.main()
