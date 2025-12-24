import sqlite3

conn = sqlite3.connect('neonspire_dev.db')
cursor = conn.cursor()

# Read your models.py to understand table structure
# Create essential tables for chat system

tables_sql = [
    # Users table
    '''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        mobile VARCHAR(24),
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(20) DEFAULT 'PLAYER',
        telegram_id BIGINT,
        telegram_username VARCHAR(64)
    )''',
    
    # dm_threads
    '''CREATE TABLE IF NOT EXISTS dm_threads (
        id INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL,
        employee_id INTEGER,
        status VARCHAR(16) DEFAULT 'OPEN',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_msg_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES users(id),
        FOREIGN KEY (employee_id) REFERENCES users(id)
    )''',
    
    # dm_messages WITH source column
    '''CREATE TABLE IF NOT EXISTS dm_messages (
        id INTEGER PRIMARY KEY,
        thread_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        sender_role VARCHAR(20),
        body TEXT NOT NULL,
        source VARCHAR(20) DEFAULT 'website',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (thread_id) REFERENCES dm_threads(id),
        FOREIGN KEY (sender_id) REFERENCES users(id)
    )'''
]

for sql in tables_sql:
    cursor.execute(sql)

print("✅ Created essential tables for chat system")
print("✅ 'source' column is included in dm_messages")

# Show structure
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
print("\n📊 Tables created:")
for table in cursor.fetchall():
    cursor.execute(f"PRAGMA table_info({table[0]})")
    cols = [col[1] for col in cursor.fetchall()]
    print(f"  • {table[0]}: {', '.join(cols)}")

conn.commit()
conn.close()
