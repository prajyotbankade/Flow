import sys
from pathlib import Path

# Ensure eval modules are importable when pytest runs from the project root
sys.path.insert(0, str(Path(__file__).parent))
