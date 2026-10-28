"""Read-only activity watermarks. Updates to an existing setup do not count as a new hit."""
import sqlite3

def four_h_activity(root):
    database=root/'scanner.sqlite3'
    try:
        with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2) as connection:
            first=connection.execute('SELECT seen_at FROM events ORDER BY id LIMIT 1').fetchone()
            if first is None:return None
            rows=connection.execute("SELECT s.symbol,MAX(e.id) FROM events e JOIN structures s ON s.group_id=e.group_id WHERE e.kind IN ('new','reappeared') GROUP BY s.symbol").fetchall()
            return {'generation':first[0],'by_symbol':{symbol:int(event) for symbol,event in rows}}
    except (sqlite3.Error,OSError):
        return None
