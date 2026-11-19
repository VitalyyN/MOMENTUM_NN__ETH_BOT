"""Транзакционное состояние робота и финансовый журнал."""

import json
import sqlite3
from contextlib import closing
from decimal import Decimal


class Journal:
    def __init__(self, path, account):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (day TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, intent TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS finance (id TEXT PRIMARY KEY, entry TEXT NOT NULL);
        """)
        other = self.get("account")
        if other is not None and other != account:
            self.db.close()
            raise ValueError("Журнал привязан к другому API-ключу. Требуется сверка счёта перед заменой ключа.")
        self.update(account=account)

    def close(self):
        self.db.close()

    def get(self, name, default=None):
        row = self.db.execute("SELECT value FROM state WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def _write(self, states):
        self.db.executemany(
            "INSERT OR REPLACE INTO state VALUES (?, ?)",
            ((key, json.dumps(value, ensure_ascii=False)) for key, value in states.items()),
        )

    def update(self, **states):
        with self.db:
            self._write(states)

    def decision(self, day, data, **states):
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?)", (day, json.dumps(data)))
            self._write(dict(states, last_day=day, last_decision=data))

    def begin_order(self, op, **states):
        with self.db:
            self.db.execute("INSERT INTO orders VALUES (?, ?, NULL)", (op["id"], json.dumps(op)))
            self._write(dict(states, pending=op))

    def finish_order(self, op, result, **states):
        with self.db:
            self.db.execute("UPDATE orders SET result=? WHERE id=?", (json.dumps(result), op["id"]))
            self._write(dict(states, pending=None))

    def record_finance(self, entries, through):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO finance VALUES (?, ?)",
                                ((row["id"], json.dumps(row)) for row in entries))
            self._write({"finance_through": through})

    def finance(self):
        cash = commission = received = paid = Decimal(0)
        with closing(self.db.execute("SELECT entry FROM finance")) as cursor:
            for (value,) in cursor:
                row = json.loads(value)
                cash += Decimal(row["cashFlow"] or "0")
                commission += Decimal(row["fee"] or "0")
                funding = Decimal(row["funding"] or "0")
                received += max(funding, 0)
                paid -= min(funding, 0)
        return cash, commission, received, paid
