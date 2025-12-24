#!/bin/bash

echo "Fixing BonusClaim to BonusRecord in all files..."

# Fix employee_bp.py
echo "Fixing employee_bp.py..."
sed -i '' 's/BonusClaim/BonusRecord/g' employee_bp.py

# Fix player_bp.py  
echo "Fixing player_bp.py..."
sed -i '' 's/BonusClaim/BonusRecord/g' player_bp.py

# Fix app.py
echo "Fixing app.py..."
sed -i '' 's/BonusClaim/BonusRecord/g' app.py

echo "✅ Done! Now try running: python app.py"
