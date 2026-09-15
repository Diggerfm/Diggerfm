from . import bandcamp, mixcloud

REGISTRY = {
    "bandcamp": bandcamp.collect,
    "mixcloud": mixcloud.collect,
}
