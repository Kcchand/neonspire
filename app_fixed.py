import os
import sys
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import logging
from dotenv import load_dotenv
import requests
import json
import uuid
from datetime import datetime, timedelta
import random

# Load environment variables
load_dotenv()
print(f"✅ Loaded .env from: {os.path.abspath('.env')}")

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if TELEGRAM_BOT_TOKEN:
    print(f"✅ TELEGRAM_BOT_TOKEN (first 15): {TELEGRAM_BOT_TOKEN[:15]}...")
else:
    print("⚠️ TELEGRAM_BOT_TOKEN not found")

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///neonspire_dev.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User model
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    mobile = db.Column(db.String(20), nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user')
    email_verified = db.Column(db.Boolean, default=False)
    email_verified_at = db.Column(db.DateTime)
    promo_seen = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    telegram_id = db.Column(db.String(50))
    telegram_username = db.Column(db.String(100))
    telegram_firstname = db.Column(db.String(100))
    telegram_lastname = db.Column(db.String(100))
    deposit_count = db.Column(db.Integer, default=0)
    signup_bonus_claimed = db.Column(db.Boolean, default=False)
    signup_bonus_amount = db.Column(db.Float, default=0.0)
    signup_bonus_claimed_at = db.Column(db.DateTime)
    regular_bonus_last_claimed = db.Column(db.DateTime)
    available_bonus = db.Column(db.Float, default=0.0)
    bonus_eligible = db.Column(db.Boolean, default=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Fixed seed_admin function
def seed_admin():
    """Fixed admin seeding function."""
    print("🔍 seed_admin() - Checking for admin user...")
    
    email = "aryan@neonspire.com"
    
    try:
        # First, ensure tables exist
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        
        if 'users' not in inspector.get_table_names():
            print("🔄 Creating database tables...")
            db.create_all()
            print("✅ Tables created")
        
        # Use a simple query that won't fail on missing columns
        from sqlalchemy import text
        
        # Check if admin exists using raw SQL
        result = db.session.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": email}
        ).fetchone()
        
        if not result:
            print(f"👤 Creating admin user: {email}")
            
            # Insert admin user
            from werkzeug.security import generate_password_hash
            
            db.session.execute(text(
                "INSERT INTO users (name, email, mobile, password_hash, role, email_verified, bonus_eligible) "
                "VALUES (:name, :email, :mobile, :password, :role, :verified, :eligible)"
            ), {
                'name': 'Admin',
                'email': email,
                'mobile': '1234567890',
                'password': generate_password_hash('admin123'),
                'role': 'admin',
                'verified': 1,
                'eligible': 0
            })
            
            db.session.commit()
            print(f"✅ Admin user created: {email}")
        else:
            print(f"✅ Admin user already exists: {email}")
            
    except Exception as e:
        print(f"⚠️ Error in seed_admin (non-fatal): {e}")
        # Don't crash the app

def create_app():
    """Application factory pattern."""
    # Your existing create_app code here...
    # Just make sure to call seed_admin() properly
    
    with app.app_context():
        print("🔄 Setting up database...")
        db.create_all()
        seed_admin()
    
    # Add your routes and other setup here...
    
    @app.route('/')
    def index():
        return "🚀 App is running! Database is set up correctly."
    
    return app

if __name__ == '__main__':
    app = create_app()
    print("✅ App created successfully!")
    print("🌐 Starting server on http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5001)
