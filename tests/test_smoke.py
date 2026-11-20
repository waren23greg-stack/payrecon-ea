import sys

import payrecon


def test_version():
    assert payrecon.__version__ == "0.1.0"


def test_python_floor():
    assert sys.version_info >= (3, 11)
