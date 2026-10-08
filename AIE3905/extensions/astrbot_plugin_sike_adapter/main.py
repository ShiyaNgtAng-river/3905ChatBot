from astrbot.api.star import Context, Star

# Importing the module registers the "sike_http" platform type; plugins load before
# platforms, so a platform entry of that type in cmd_config.json can use it.
from .adapter import SikeAdapter  # noqa: F401


class SikeAdapterPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
