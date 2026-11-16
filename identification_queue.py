"""Persistent remote-identification jobs stored alongside detections."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager


class IdentificationQueue:
    def __init__(self, path):
        self.path = path
        with self.connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS identification_jobs (
                    id TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    payload TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    notification TEXT NOT NULL DEFAULT 'none'
                )
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def enqueue(self, payload):
        identity = payload['timestamp'] + '\0' + payload['photo_path']
        job_id = hashlib.sha256(identity.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO identification_jobs (id, created, payload) VALUES (?, ?, ?)",
                (job_id, time.time(), json.dumps(payload)),
            )
        return job_id

    def recover(self):
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE identification_jobs SET state='failed', error='worker_interrupted' WHERE state='running'"
            )
        return cursor.rowcount

    def claim(self):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                "SELECT * FROM identification_jobs WHERE state='pending' ORDER BY created, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE identification_jobs SET state='running' WHERE id=?", (row['id'],))
        return {'id': row['id'], **json.loads(row['payload'])}

    def complete(self, job_id, result, *, publish=False, notify=False, error=None):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                "SELECT * FROM identification_jobs WHERE id=? AND state='running'", (job_id,)
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(row['payload'])
            if publish:
                connection.execute("""
                    INSERT INTO detections
                        (timestamp, species, common_name, gpt_confidence, spike_score,
                         baseline_score, photo_path, video_path, visit_duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (payload['timestamp'], result['name'], result['common_name'], result['score'],
                      payload['spike_score'], payload['baseline_score'], payload['photo_path'],
                      payload.get('video_path'), payload['visit_duration']))
            connection.execute(
                "UPDATE identification_jobs SET state=?, result=?, error=?, notification=? WHERE id=?",
                ('failed' if error else 'complete', json.dumps(result), error,
                 'pending' if publish or notify else 'none', job_id),
            )
        return True

    def claim_notification(self):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                "SELECT * FROM identification_jobs WHERE notification='pending' ORDER BY created, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE identification_jobs SET notification='started' WHERE id=?", (row['id'],))
        return {'id': row['id'], **json.loads(row['payload']), 'species': json.loads(row['result'])}

    def finish_notification(self, job_id, success):
        with self.connect() as connection:
            connection.execute(
                "UPDATE identification_jobs SET notification=? WHERE id=? AND notification='started'",
                ('sent' if success else 'failed', job_id),
            )