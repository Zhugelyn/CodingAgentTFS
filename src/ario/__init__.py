"""ArioCodingAgent: харнесс из трёх агентов (analyst, developer, reviewer) на OpenHands SDK."""

import os

# Баннер SDK при импорте засоряет вывод любой команды (в том числе `ario trace`)
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
