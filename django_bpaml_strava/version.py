try:
    from django_bpaml_strava._version import version
    __version__ = version
except ModuleNotFoundError:
    __version__ = version = "unbuilt"
