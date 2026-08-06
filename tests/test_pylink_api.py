"""Every pylink call this package makes must exist on pylink.JLink.

A missing method is an AttributeError at runtime, on whichever path happens to
use it -- `jlink.go()` sat in the --reset path through a release before anyone
hit it. The fake J-Link in the other tests cannot catch this: it only
implements what it is asked for.
"""
import re
from pathlib import Path
import pylink

SOURCE_DIR = Path(__file__).resolve().parent.parent / 'rttt'


def jlink_calls():
    """Attribute names called on a `jlink` object anywhere in the package."""
    # Public names only: privates live on the instance, not the class, so
    # hasattr() cannot see them here. They are all inside try/except anyway.
    pattern = re.compile(r'\bjlink\.([a-zA-Z][a-zA-Z0-9_]*)')
    found = {}
    for path in SOURCE_DIR.rglob('*.py'):
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in pattern.findall(line):
                found.setdefault(name, f'{path.name}:{lineno}')
    return found


def test_every_jlink_attribute_used_exists():
    missing = [f'jlink.{name} ({where})'
               for name, where in sorted(jlink_calls().items())
               if not hasattr(pylink.JLink, name)]
    assert not missing, 'calls pylink API that does not exist: ' + ', '.join(missing)


def test_the_pylink_names_imported_by_name_exist():
    # Referenced as pylink.<path> rather than through a jlink instance.
    for path in ('errors.JLinkException', 'errors.JLinkRTTException',
                 'enums.JLinkInterfaces'):
        obj = pylink
        for part in path.split('.'):
            obj = getattr(obj, part, None)
            assert obj is not None, f'pylink.{path} does not exist'

    assert hasattr(pylink.enums.JLinkInterfaces, 'SWD')
