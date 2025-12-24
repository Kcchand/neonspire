with open('app.py', 'r') as f:
    lines = f.readlines()

# Find where to add the import (usually near other flask imports)
for i, line in enumerate(lines):
    if 'from flask import' in line or 'import Flask' in line:
        # Add after Flask imports
        lines.insert(i+1, 'from flask_migrate import Migrate\n')
        print(f"✅ Added Migrate import at line {i+2}")
        break

with open('app.py', 'w') as f:
    f.writelines(lines)
