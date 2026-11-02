import logging
import sys
from .config import Config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
config = Config().prepare()
role = sys.argv[1] if len(sys.argv)>1 else 'web'
if role == 'recorder':
    from .recorder import main
elif role == 'worker':
    from .worker import main
elif role == 'web':
    from .web import main
else:
    raise SystemExit('Usage: python -m radio {recorder|worker|web}')
main(config)
