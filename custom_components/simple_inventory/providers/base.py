"""Abstract base class for barcode product lookup providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class ProductInfo(TypedDict):
    """Standardized product information returned by providers."""

    name: str
    description: NotRequired[str]
    category: NotRequired[str]
    brand: NotRequired[str]
    unit: NotRequired[str]
    image_url: NotRequired[str]


class BarcodeProvider(ABC):
    """Abstract base class for barcode lookup providers."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize with a Home Assistant instance."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of this provider."""

    @abstractmethod
    async def async_lookup(self, barcode: str) -> ProductInfo | None:
        """Look up a barcode and return product info, or None if not found."""

    async def async_close(self) -> None:
        """Clean up provider resources. Default no-op."""
