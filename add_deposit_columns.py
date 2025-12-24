# add_deposit_columns.py
from app import create_app, db
from sqlalchemy import text, inspect

app = create_app()

with app.app_context():
    print("🔄 Adding missing columns to deposit_requests table...")
    
    inspector = inspect(db.engine)
    columns = [c['name'] for c in inspector.get_columns('deposit_requests')]
    
    missing_columns = [
        ('bonus_percentage', 'ALTER TABLE deposit_requests ADD COLUMN bonus_percentage INTEGER DEFAULT 0'),
        ('bonus_amount', 'ALTER TABLE deposit_requests ADD COLUMN bonus_amount INTEGER DEFAULT 0'),
        ('total_credited', 'ALTER TABLE deposit_requests ADD COLUMN total_credited INTEGER DEFAULT 0'),
        ('credited_amount', 'ALTER TABLE deposit_requests ADD COLUMN credited_amount INTEGER DEFAULT 0'),
    ]
    
    for col_name, sql in missing_columns:
        if col_name not in columns:
            try:
                db.session.execute(text(sql))
                db.session.commit()
                print(f"✅ Added '{col_name}' column to deposit_requests")
            except Exception as e:
                print(f"⚠️ Could not add '{col_name}': {e}")
                db.session.rollback()
        else:
            print(f"✓ '{col_name}' already exists")
    
    print("\n✅ Database update complete. Restart your app.")