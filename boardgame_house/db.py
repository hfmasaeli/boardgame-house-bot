import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, fee TEXT NOT NULL,
          currency TEXT NOT NULL, instructions TEXT NOT NULL, open INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS drafts (
          user_id INTEGER PRIMARY KEY, step TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS registrations (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
          event_id INTEGER NOT NULL REFERENCES events(id),
          name TEXT NOT NULL, surname TEXT NOT NULL, phone TEXT NOT NULL,
          nickname TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unpaid'
            CHECK(status IN ('unpaid','pending','paid','rejected')),
          receipt TEXT, receipt_kind TEXT,
          UNIQUE(user_id,event_id));
        CREATE TABLE IF NOT EXISTS scores (
          registration_id INTEGER NOT NULL REFERENCES registrations(id),
          round TEXT NOT NULL, score REAL NOT NULL,
          PRIMARY KEY(registration_id, round));
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS champions (
          registration_id INTEGER PRIMARY KEY REFERENCES registrations(id),
          event_id INTEGER NOT NULL UNIQUE REFERENCES events(id));
        ''')
        columns = {r['name'] for r in self.rows('PRAGMA table_info(events)')}
        if 'series' not in columns:
            self.run("ALTER TABLE events ADD COLUMN series TEXT NOT NULL DEFAULT 'raiders'")
        champion_columns = {r['name'] for r in self.rows('PRAGMA table_info(champions)')}
        if 'event_id' not in champion_columns:
            with self.db:
                self.db.execute('ALTER TABLE champions ADD COLUMN event_id INTEGER REFERENCES events(id)')
                self.db.execute('''UPDATE champions SET event_id=(
                  SELECT event_id FROM registrations WHERE id=champions.registration_id)''')
                self.db.execute('''DELETE FROM champions WHERE registration_id NOT IN (
                  SELECT MAX(registration_id) FROM champions GROUP BY event_id)''')
        self.run('CREATE UNIQUE INDEX IF NOT EXISTS one_champion_per_event ON champions(event_id)')
        self.run("UPDATE events SET name='ریدرز' WHERE name='Raiders'")
        # Upgrade the original unconfigured Raiders seed without changing paid events.
        self.run("UPDATE events SET fee='400000',currency='IRT' WHERE series='raiders' AND fee=''")

    def run(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def rows(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def draft(self, uid, step, data):
        self.run('INSERT OR REPLACE INTO drafts VALUES (?,?,?)', (uid, step, json.dumps(data)))

    def get_draft(self, uid):
        row = self.one('SELECT * FROM drafts WHERE user_id=?', (uid,))
        return (row['step'], json.loads(row['data'])) if row else (None, {})

    def clear(self, uid):
        self.run('DELETE FROM drafts WHERE user_id=?', (uid,))

    def decide(self, rid, admin, approve):
        with self.db:
            result = self.db.execute("UPDATE registrations SET status=? WHERE id=? AND status='pending'",
                                     ('paid' if approve else 'rejected', rid))
            if result.rowcount:
                self.db.execute('INSERT INTO audit(admin_id,action) VALUES (?,?)',
                                (admin, f'{"approve" if approve else "reject"} registration {rid}'))
            return bool(result.rowcount)

    def score(self, rid, round_name, value, admin):
        with self.db:
            row = self.one("SELECT id FROM registrations WHERE id=? AND status='paid'", (rid,))
            if not row:
                raise ValueError('ابتدا باید پرداخت این ثبت‌نام تأیید شود.')
            self.db.execute('INSERT INTO scores VALUES (?,?,?) ON CONFLICT(registration_id,round) DO UPDATE SET score=excluded.score',
                            (rid, round_name, value))
            self.db.execute('INSERT INTO audit(admin_id,action) VALUES (?,?)',
                            (admin, f'score registration {rid}, round {round_name}: {value}'))

    def leaderboard(self, eid):
        return self.rows("""SELECT r.user_id,
          (SELECT latest.nickname FROM registrations latest
           JOIN events le ON le.id=latest.event_id
           WHERE latest.user_id=r.user_id AND le.series=e.series AND latest.status='paid'
           ORDER BY latest.id DESC LIMIT 1) nickname,
          MAX(s.score) AS best
          FROM registrations r JOIN scores s ON s.registration_id=r.id
          JOIN events e ON e.id=r.event_id
          WHERE e.series=(SELECT series FROM events WHERE id=?) AND r.status='paid'
          GROUP BY r.user_id ORDER BY best DESC, r.user_id LIMIT 50""", (eid,))

    def champion(self, rid, admin, remove=False):
        with self.db:
            if not remove and not self.one("""SELECT r.id,r.event_id FROM registrations r JOIN scores s
                    ON s.registration_id=r.id WHERE r.id=? AND r.status='paid'""", (rid,)):
                raise ValueError('برای ثبت قهرمانی، ثبت‌نام تأییدشده و امتیاز ثبت‌شده لازم است.')
            if remove:
                self.db.execute('DELETE FROM champions WHERE registration_id=?', (rid,))
            else:
                registration = self.one('SELECT event_id FROM registrations WHERE id=?', (rid,))
                self.db.execute('''INSERT INTO champions(registration_id,event_id) VALUES (?,?)
                  ON CONFLICT(event_id) DO UPDATE SET registration_id=excluded.registration_id''',
                                (rid, registration['event_id']))
            self.db.execute('INSERT INTO audit(admin_id,action) VALUES (?,?)',
                            (admin, f'{"remove champion" if remove else "champion"} registration {rid}'))

    def titles(self, uid):
        return self.rows("""SELECT e.name FROM champions c
          JOIN registrations r ON r.id=c.registration_id
          JOIN events e ON e.id=r.event_id WHERE r.user_id=? ORDER BY e.id""", (uid,))
