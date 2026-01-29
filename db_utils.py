import json
import pickle
import sqlite3


def execute_query(db_file, query, params=None):
    "helper function"
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    if type(params) == list and type(params[0]) == list:
        r = cur.executemany(query, params)
    elif params:
        if type(params) in [list or tuple]:
            r = cur.execute(query, params).fetchall()
        else:
            r = cur.execute(query, (params,)).fetchall()
    else:
        r = cur.execute(query).fetchall()
    con.commit()
    con.close()
    return r


def create_live_translation_db():
    db_file = "live_translation.db"
    query = """CREATE TABLE IF NOT EXISTS translations (
        msg_id text NOT NULL PRIMARY KEY, 
        author text, 
        target text, 
        textjp text,
        datetime datetime)
         """
    return execute_query(db_file, query)


def insert_live_translation(msg_id, author, target, textjp, datetime):
    db_file = "live_translation.db"
    query = "INSERT OR IGNORE INTO translations VALUES (?,?,?,?,?)"
    params = [msg_id, author, target, textjp, datetime]
    return execute_query(db_file, query, params)


def get_live_translation_from_db(msg_id):
    db_file = "live_translation.db"
    query = f"select * from translations where msg_id={msg_id}"
    return execute_query(db_file, query)


def insert_translated(translations, alldata):
    "translations: translations for the day"

    db_file = "live_translation.db"
    query = "INSERT OR IGNORE INTO translations VALUES (?,?,?,?,?)"

    params = []
    # texten,textjp = translations
    # for msg_id, (en,jp) in textjp.items():
    for msg_id, (en, jp) in translations.items():
        found = [msg for msg in alldata if msg["id"] == msg_id]
        if not found:
            raise (f"corresponding msg not found for {msg_id}")
        if not get_live_translation_from_db(msg_id):
            msg = found[0]
            author = msg["author_signature"]
            PostDate = datetime.fromtimestamp(msg["date"])
            params.append([msg_id, author, en, jp, PostDate])

    if params:
        return execute_query(db_file, query, params)
    else:
        return None


def last_10_translations():
    return execute_query(
        "live_translation.db",
        "select * from translations order by datetime desc limit 10",
    )


def create_update_db():
    db_file = "updates.db"
    query = "CREATE TABLE IF NOT EXISTS updates (id text NOT NULL PRIMARY KEY, date text, data json)"
    execute_query(db_file, query)
    query = "CREATE TABLE IF NOT EXISTS albums (album_id text NOT NULL PRIMARY KEY)"
    execute_query(db_file, query)
    query = "CREATE TABLE IF NOT EXISTS summaries (date text NOT NULL PRIMARY KEY, summary text)"
    execute_query(db_file, query)


def list_tables(db_file="updates.db"):
    query = "select name from sqlite_master where type='table';"
    return execute_query(db_file, query)


def clean_update():
    "clean up anything other than WTM updates"
    r = execute_query("updates.db", "select * from updates")
    for msg_id, _, rr in r:
        jo = json.loads(rr)
        chat_id = jo["message"]["chat_id"]
        print(chat_id)
        if chat_id != WTM:
            rrr = execute_query("updates.db", f"delete from updates where id={msg_id}")


def insert_update(update):
    db_file = "updates.db"
    query = "INSERT OR IGNORE INTO updates VALUES (?,?,?) ON CONFLICT DO NOTHING"
    params = [update["message"]["id"], update["message"]["date"], json.dumps(update)]
    execute_query(db_file, query, params)


def select_update(msg_id=None, last=5):
    db_file = "updates.db"
    if not msg_id:
        query = f"select * from updates order by date desc, id desc limit {last}"
        return execute_query(db_file, query)
    elif msg_id:
        query = f"select * from updates where id = {msg_id}"
        return execute_query(db_file, query)


def insert_album_id(album_id):
    db_file = "updates.db"
    query = f"INSERT OR IGNORE INTO albums VALUES ({album_id})"
    return execute_query(db_file, query)


def get_album_id(album_id):
    db_file = "updates.db"
    if type(album_id) == int:
        query = f"SELECT album_id from albums where album_id={album_id}"
        r = execute_query(db_file, query)
        if r:
            return r[0][0]
        else:
            return None
    else:
        raise ("album_id should be an integer")


def last_10_album_ids():
    return execute_query("updates.db", "select * from albums ORDER BY rowid limit 10")
