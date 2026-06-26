from modules.platform.services.base_adapter import BaseAdapter


class AdapterRegistry:
    _adapters: dict[str, BaseAdapter] = {}

    @classmethod
    def register_adapter(cls, platform_name: str, adapter: BaseAdapter):
        cls._adapters[cls._normalize(platform_name)] = adapter
        return adapter

    @classmethod
    def get_adapter(cls, platform_name: str):
        return cls._adapters.get(cls._normalize(platform_name))

    @classmethod
    def list_adapters(cls):
        return sorted(cls._adapters.keys())

    @staticmethod
    def _normalize(platform_name: str):
        return platform_name.strip().lower()
