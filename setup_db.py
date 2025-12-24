import os
import sqlite3

print("🔧 Setting up database...")

# Set the environment variable to use neonspire_dev.db
os.environ['DATABASE_URL'] = 'sqlite:///neonspire_dev.db'

# Import after setting env var
from app_fixed import app, db
from models import *

with app.app_context():
    # Create all tables
    db.create_all()
    print("✅ Created all tables in neonspire_dev.db")
    
    # Add source column to dm_messages
    try:
        db.engine.execute('ALTER TABLE dm_messages ADD COLUMN source VARCHAR(20) DEFAULT "website"')
        print("✅ Added 'source' column to dm_messages")
    except Exception as e:
        print(f"Note: {e}")
    
    # Verify
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    
    print(f"\n📊 Database: neonspire_dev.db")
    print(f"📦 Tables created: {len(tables)}")
    
    # Check dm_messages structure
    if 'dm_messages' in tables:
        result = db.engine.execute("PRAGMA table_info(dm_messages)")
        columns = [row[1] for row in result]
        print(f"\n📝 dm_messages columns: {', '.join(columns)}")
        
        if 'source' in columns:
            print("🎉 SUCCESS: 'source' column is ready to use!")
