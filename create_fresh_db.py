import os
os.environ['FLASK_APP'] = 'app.py'

from app import create_app
from models import db

app = create_app()

with app.app_context():
    # Drop all tables and recreate
    db.drop_all()
    db.create_all()
    print("✅ Created fresh database with ALL models including bonus fields")
    
    # Verify tables were created
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print(f"📊 Tables created: {tables}")
    
    # Check users table columns
    columns = inspector.get_columns('users')
    print(f"\n📋 Users table columns:")
    for col in columns:
        print(f"  - {col['name']} ({col['type']})")
