import sqlite3, os
os.makedirs('data', exist_ok=True)
os.environ['ADMIN_PASS'] = '87416180'
DB_PATH = 'data/notas.db'
conn = sqlite3.connect(DB_PATH)
conn.execute('CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, admin INTEGER)')
cur = conn.execute("SELECT id FROM usuarios WHERE username='admin'").fetchone()
conn.execute("INSERT OR IGNORE INTO usuarios (username, password, admin) VALUES (?, ?, ?)", ('admin', 'admin123', 1))
env_pass = os.getenv('ADMIN_PASS')
if env_pass:
    conn.execute("UPDATE usuarios SET password=? WHERE username='admin'", (env_pass,))
conn.commit()
r = conn.execute("SELECT username, password FROM usuarios WHERE username='admin'").fetchone()
print(f'User: {r[0]}, Pass: {r[1]}')
conn.close()
