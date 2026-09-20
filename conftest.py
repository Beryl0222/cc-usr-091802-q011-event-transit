"""保证 tests/ 下的用例既能被 pytest 发现，也能被
`python3 -m unittest discover -s tests` 发现并导入项目根模块。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
