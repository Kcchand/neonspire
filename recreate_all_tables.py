# recreate_all_tables.py
from app import app, db

with app.app_context():
    print("Dropping all tables...")
    db.drop_all()
    
    print("Creating all tables with correct schema...")
    db.create_all()
    
    print("✅ Tables recreated with correct schema")